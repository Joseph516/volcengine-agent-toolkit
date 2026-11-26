#!/usr/bin/env python3
"""离线集合、最长前缀与路径证据校验；不模拟产品规则优先级或真实数据平面。"""
import argparse
import ipaddress
import json
import sys


def networks(values):
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError("networks must be a list of CIDR strings")
    return [ipaddress.ip_network(v, strict=True) for v in values]


def difference(proposed, allowed):
    rest = proposed[:]
    for permit in allowed:
        updated = []
        for net in rest:
            if net.version != permit.version or not net.overlaps(permit):
                updated.append(net)
            elif net.subnet_of(permit):
                continue
            else:
                updated.extend(net.address_exclude(permit))
        rest = updated
    return [str(n) for version in (4, 6) for n in ipaddress.collapse_addresses(
        [n for n in rest if n.version == version])]


def scope_change(data):
    # sources / destinations 为有效可达集合，不是单条ACL的原始允许集合。
    extra = {k: difference(networks(data['proposed'][k]), networks(data['allowed'][k]))
             for k in ('sources', 'destinations')}
    return {'status': 'expands' if any(extra.values()) else 'within_scope',
            'extra': extra,
            'note': 'Checks one fixed protocol/port flow; ignores policy priority and runtime reachability'}


def route(data):
    if not isinstance(data['destination'], str):
        raise ValueError('destination must be an IP string')
    if not isinstance(data['routes'], list) or any(not isinstance(r, dict) or not isinstance(r.get('cidr'), str) for r in data['routes']):
        raise ValueError('routes must be a list of entries with CIDR strings')
    destination = ipaddress.ip_address(data['destination'])
    candidates = []
    for entry in data['routes']:
        net = ipaddress.ip_network(entry['cidr'], strict=True)
        if net.version == destination.version and destination in net:
            candidates.append((net.prefixlen, entry))
    if data.get('complete') is not True or not data.get('evidence'):
        return {'status': 'unknown', 'reason': 'Route list incomplete or evidence missing'}
    if not candidates:
        return {'status': 'no_match', 'note': 'No matching entry in supplied table only'}
    prefix = max(x[0] for x in candidates)
    best = [entry for size, entry in candidates if size == prefix]
    return {'status': 'selected' if len(best) == 1 else 'ambiguous', 'candidates': best,
            'effective_for_subnet': data.get('associated') is True,
            'note': 'Longest prefix only; ties require product-specific resolution. No reachability proof'}


def path(data):
    # 调用方必须先按真实产品语义、所有关联SG和ACL聚合每一关卡，不接收猜测的默认值。
    required = ['source_sg_egress', 'destination_sg_ingress',
                'source_acl_egress', 'destination_acl_ingress',
                'destination_acl_egress_return', 'source_acl_ingress_return',
                'forward_route', 'return_route']
    gates = data['gates']
    if not isinstance(gates, dict) or any(not isinstance(g, dict) for g in gates.values()):
        raise ValueError('gates must be an object of gate objects')
    unknown, blocked = [], []
    for key in required:
        gate = gates.get(key, {})
        if (gate.get('decision') not in ('allow', 'deny')
                or gate.get('applicable') is not True
                or gate.get('complete') is not True or not gate.get('evidence')
                or gate.get('observed_at') != data.get('target_time')
                or not data.get('target_time')):
            unknown.append(key)
        elif gate['decision'] == 'deny':
            blocked.append(key)
    return {'configuration': 'blocked' if blocked else ('unknown' if unknown else 'allows'),
            'blocked_gates': blocked, 'unknown_gates': unknown,
            'runtime_reachability': 'not_verified',
            'note': 'Configuration snapshot only; does not prove historical unique root cause or successful connection'}


def run(data):
    if not isinstance(data, dict):
        raise ValueError('snapshot must be an object')
    operation = data['operation']
    if operation == 'scope':
        return scope_change(data)
    if operation == 'route':
        return route(data)
    if operation == 'path':
        return path(data)
    raise ValueError('operation must be scope, route or path')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    args = parser.parse_args()
    try:
        with open(args.snapshot) as f:
            result = run(json.load(f))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
