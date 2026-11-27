#!/usr/bin/env python3
"""离线集合、最长前缀与路径证据校验；不模拟产品规则优先级或真实数据平面。"""
import argparse
import ipaddress
import json
import sys

import importlib.util
from pathlib import Path
_spec = importlib.util.spec_from_file_location('vpc_evidence', Path(__file__).with_name('evidence.py'))
_proof = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_proof)
Evidence, pointer, verify_claims = _proof.Evidence, _proof.pointer, _proof.verify_claims


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
            'extra': extra, 'verification': 'unbound_set_arithmetic', 'minimum_change_verified': False,
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
            'correct_gateway': 'not_verified',
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
            'runtime_reachability': 'not_verified', 'verification': 'unbound_gate_arithmetic',
            'note': 'Configuration snapshot only; does not prove historical unique root cause or successful connection'}


def sg_scope(data, bundle):
    """Conservative ingress SG impact: derive destinations from complete ENI inventory."""
    evidence = Evidence(bundle, data['context'], data['window'])
    try:
        members, seen, total, request_scope = [], set(), None, None
        for page in data['eni_pages']:
            if page['items_ref']['source'] != page['total_ref']['source']:
                raise ValueError('Inventory and total must come from the same response')
            source = bundle[page['items_ref']['source']]
            request = source['request']
            if not isinstance(request, dict):
                raise ValueError('Original inventory request missing')
            # Caller supplies only the pagination field names, never a list of allowed filters.
            pagination = {'PageNumber', 'PageSize', 'NextToken', 'MaxResults'}
            filters = {k: v for k, v in request.items() if k not in pagination}
            expected = {'Action': data['inventory_action'], 'Region': data['context']['region']}
            if filters != expected:
                raise ValueError('Inventory request must be unfiltered; restrictive filters can hide SG members')
            if request_scope is not None and filters != request_scope:
                raise ValueError('Inventory request scope changed')
            request_scope = filters
            entries = evidence.read(page['items_ref'])
            page_total = evidence.read(page['total_ref'])
            if type(page_total) is not int or page_total < 0 or (total is not None and page_total != total):
                raise ValueError('Unstable or invalid inventory total')
            total = page_total
            if not isinstance(entries, list):
                raise ValueError('ENI inventory must be an array')
            for eni in entries:
                ident = pointer(eni, data['fields']['id'])
                if not isinstance(ident, str) or not ident or ident in seen:
                    raise ValueError('Missing or duplicate ENI id')
                seen.add(ident)
                groups = pointer(eni, data['fields']['security_groups'])
                if not isinstance(groups, list) or any(not isinstance(g, str) for g in groups):
                    raise ValueError('SG membership unknown; map actual string-id array')
                if data['security_group_id'] in groups:
                    addresses = []
                    for field in data['fields']['addresses']:
                        values = pointer(eni, field['pointer'])
                        if not isinstance(values, list):
                            raise ValueError('Member ENI address list missing')
                        addresses.extend(pointer(v, field.get('value_pointer', '')) for v in values)
                    if not addresses:
                        raise ValueError('Member ENI addresses missing')
                    ips = [ipaddress.ip_address(a) for a in addresses]
                    members.append({'eni_id': ident, 'addresses': [str(a) for a in ips]})
        if total is None or len(seen) != total:
            raise ValueError('Incomplete inventory; filtered or omitted ENIs cannot prove shared-SG scope')
        # No destination supplied by the agent is trusted. Includes unattached ENIs.
        family = data['family']
        if family not in (4, 6):
            raise ValueError('family must be 4 or 6')
        dest = [str(ipaddress.ip_network(a)) for m in members for a in m['addresses']
                if ipaddress.ip_address(a).version == family]
        sources = networks(data['rule_sources'])
        if not sources or any(n.version != family for n in sources):
            raise ValueError('Rule source family mismatch or empty sources')
        if not dest:
            raise ValueError('No matching destination addresses established')
        result = scope_change({'allowed': data['allowed'],
                               'proposed': {'sources': data['rule_sources'], 'destinations': dest}})
        result.update(affected_enis=members, derived_destinations=dest,
                      verification='bound_inventory_potential_impact', minimum_change_verified=False,
                      note='Potential ingress SG rule scope for one fixed protocol/port; existing rules and other layers are not simulated. within_scope is not proof of effective reachability or overall least privilege.')
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        result = {'status': 'unknown', 'reason': str(exc), 'minimum_change_verified': False}
    result['evidence'] = evidence.used
    return result


def path_evidence(data, bundle):
    evidence = Evidence(bundle, data['context'], data['window'])
    normalized, reasons = {}, {}
    targets = {}
    try:
        for side in ('source', 'destination'):
            endpoint = data['endpoints'][side]
            raw = evidence.read(endpoint['ref'])
            eni_id = pointer(raw, endpoint['id_pointer'])
            subnet_id = pointer(raw, endpoint['subnet_pointer'])
            addresses = pointer(raw, endpoint['addresses_pointer'])
            if not isinstance(addresses, list):
                raise ValueError('Endpoint address association missing')
            addresses = [str(ipaddress.ip_address(pointer(v, endpoint.get('address_value_pointer', '')))) for v in addresses]
            if str(ipaddress.ip_address(endpoint['ip'])) not in addresses:
                raise ValueError('Requested endpoint IP is not on captured ENI')
            if not isinstance(eni_id, str) or not eni_id or not isinstance(subnet_id, str) or not subnet_id:
                raise ValueError('Endpoint ENI/subnet identity missing')
            targets[side] = {'eni': eni_id, 'subnet': subnet_id}
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        result = path({'target_time': 'bound-window', 'gates': {}})
        result.update(verification='endpoint_binding_failed', reason=str(exc), evidence=evidence.used,
                      unique_root_cause='not_verified')
        return result
    owners = {'source_sg_egress': ('source','eni'), 'destination_sg_ingress': ('destination','eni'),
              'source_acl_egress': ('source','subnet'), 'source_acl_ingress_return': ('source','subnet'),
              'destination_acl_ingress': ('destination','subnet'), 'destination_acl_egress_return': ('destination','subnet'),
              'forward_route': ('source','subnet'), 'return_route': ('destination','subnet')}
    # Keep policy decisions separate from raw association and observation provenance.
    for name, gate in data['gates'].items():
        try:
            side, kind = owners[name]
            target = evidence.read(gate['target_ref'])
            if target != targets[side][kind]:
                raise ValueError('Gate target does not match the requested endpoint/associated subnet')
            if target != gate['target_id'] or not isinstance(target, str):
                raise ValueError('Target resource mismatch')
            target_ref, association_ref = gate['target_ref'], gate['association_ref']
            if (target_ref['source'] != association_ref['source'] or
                    target_ref['pointer'].rsplit('/', 1)[0] != association_ref['pointer'].rsplit('/', 1)[0]):
                raise ValueError('Target and association must be fields of the same captured object')
            association = evidence.read(association_ref)
            if not isinstance(association, list):
                raise ValueError('Policy association unknown (null is not empty)')
            policies = gate.get('policies')
            if not isinstance(policies, list):
                raise ValueError('All associated policy references required')
            ids = [p['id'] for p in policies]
            if any(not isinstance(v, str) or not v for v in association):
                raise ValueError('Association must be an array of policy IDs')
            if len(set(ids)) != len(ids) or set(ids) != set(association):
                raise ValueError('Policy references must cover every associated policy')
            if not policies:
                if '_acl_' not in name or association != [] or gate.get('decision') != 'allow':
                    raise ValueError('Only a confirmed empty ACL association may bypass policy rules')
            for policy_ref in policies:
                policy = evidence.read(policy_ref['ref'])
                rules = pointer(policy, policy_ref['rules_pointer'])
                if not isinstance(rules, list):
                    raise ValueError('Policy rules missing/null; cannot conclude allow or deny')
                if policy_ref.get('rules_mode') == 'inline_attributes':
                    action = bundle[policy_ref['ref']['source']]['request'].get('Action')
                    expected_action = ('DescribeSecurityGroupAttributes' if '_sg_' in name else
                                       'DescribeNetworkAclAttributes' if '_acl_' in name else None)
                    request = bundle[policy_ref['ref']['source']]['request']
                    if action != expected_action or expected_action is None or any(
                            k in request for k in ('PageNumber', 'PageSize', 'NextToken', 'MaxResults')):
                        raise ValueError('Inline rule proof requires the corresponding unpaginated attributes API')
                else:
                    total = pointer(policy, policy_ref['rules_total_pointer'])
                    if type(total) is not int or total != len(rules):
                        raise ValueError('Policy rule list is incomplete or total unknown')
                if pointer(policy, policy_ref['id_pointer']) != policy_ref['id']:
                    raise ValueError('Rule evidence belongs to another policy')
            if gate.get('semantics_verified') is not True or not gate.get('decision_basis'):
                raise ValueError('Decision still needs product semantics and rule reasoning')
            normalized[name] = dict(decision=gate['decision'], applicable=True,
                                    complete=gate.get('complete'), observed_at='bound-window',
                                    evidence=[gate['association_ref']])
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            reasons[name] = str(exc)
    result = path({'target_time': 'bound-window', 'gates': normalized})
    result.update(evidence=evidence.used, rejected_evidence=reasons,
                  verification='time_and_association_bound',
                  unique_root_cause='not_verified',
                  note='Association and time verified against saved captures; rule decisions remain caller-reviewed, not a firewall simulation. No historical unique-root-cause or runtime proof.')
    return result


def run(data, evidence=None):
    if not isinstance(data, dict):
        raise ValueError('snapshot must be an object')
    operation = data['operation']
    if operation == 'sg_scope':
        return sg_scope(data, evidence)
    if operation == 'path_evidence':
        return path_evidence(data, evidence)
    if operation == 'scope':
        return scope_change(data)
    if operation == 'route':
        return route(data)
    if operation == 'path':
        return path(data)
    raise ValueError('operation must be sg_scope, path_evidence, scope, route or path')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--evidence', help='Saved evidence bundle, separate from calculation input')
    args = parser.parse_args()
    try:
        with open(args.snapshot) as f:
            bundle = json.loads(Path(args.evidence).read_text()) if args.evidence else None
            result = run(json.load(f), bundle)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
