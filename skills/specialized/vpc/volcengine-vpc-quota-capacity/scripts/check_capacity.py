#!/usr/bin/env python3
"""离线数量与地址规划校验；不推断产品保留地址，不调用云接口。"""
import argparse
import ipaddress
import json
import sys


def count(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f'{field} must be a nonnegative integer')
    return value


def capacity(items):
    if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
        raise ValueError("items must be a list of objects")
    results = []
    for item in items:
        growth = count(item['growth'], 'growth')
        scope = item['scope']
        if not isinstance(scope, dict) or not scope:
            raise ValueError('scope must identify the quota/object/direction/address family')
        row = {'scope': scope, 'growth': growth, 'status': 'unknown'}
        # 同一口径、实际配额与完整用量均必须明确；默认值不能冒充实际上限。
        if (item.get('basis') != 'actual' or item.get('usage_scope') != scope
                or item.get('complete') is not True or not item.get('evidence')):
            row['reason'] = 'Actual matched quota, complete usage and evidence are required'
        elif item.get('limit') is None or item.get('used') is None:
            row['reason'] = 'Missing limit or usage'
        else:
            limit = count(item['limit'], 'limit')
            used = count(item['used'], 'used')
            remaining = limit - used
            row.update(limit=limit, used=used, remaining=remaining,
                       remaining_after=remaining-growth,
                       status='fits' if remaining >= growth else 'exceeds',
                       evidence=item['evidence'])
        results.append(row)
    return {'items': results, 'scope': 'provided snapshot only'}


def addresses(parent, entries):
    if not isinstance(parent, str):
        raise ValueError("parent must be a CIDR string")
    if not isinstance(entries, list) or any(not isinstance(v, str) for v in entries):
        raise ValueError("cidrs must be a list of CIDR strings")
    network = ipaddress.ip_network(parent, strict=True)
    checked, errors, nets = [], [], []
    for value in entries:
        try:
            net = ipaddress.ip_network(value, strict=True)
        except ValueError:
            errors.append({'cidr': value, 'error': 'invalid CIDR or host bits set'})
            continue
        if net.version != network.version or not net.subnet_of(network):
            errors.append({'cidr': value, 'error': 'outside parent'})
            continue
        for prev in nets:
            if net.overlaps(prev):
                errors.append({'cidr': value, 'error': 'overlap', 'with': str(prev)})
        nets.append(net)
        checked.append({'cidr': str(net), 'addresses': net.num_addresses})
    return {'valid': not errors, 'parent': str(network), 'entries': checked,
            'errors': errors, 'note': 'Address counts include reserved addresses; not allocatable IP counts'}


def run(data):
    if not isinstance(data, dict):
        raise ValueError('snapshot must be an object')
    if data['operation'] == 'capacity':
        return capacity(data['items'])
    if data['operation'] == 'addresses':
        return addresses(data['parent'], data['cidrs'])
    raise ValueError('operation must be capacity or addresses')


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
