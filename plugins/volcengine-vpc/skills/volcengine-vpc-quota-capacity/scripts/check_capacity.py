#!/usr/bin/env python3
"""离线数量与地址规划校验；不推断产品保留地址，不调用云接口。"""
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
    return {'items': results, 'scope': 'provided snapshot only',
            'verification': 'unbound_arithmetic_only',
            'warning': 'basis/complete/evidence labels do not verify original data; do not present as confirmed capacity'}


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


def capacity_evidence(data, bundle):
    evidence = Evidence(bundle, data['context'], data['window'])
    rows, identities = [], set()
    for item in data['items']:
        row = {'id': item['id'], 'status': 'unknown'}
        try:
            quota = evidence.read(item['quota_ref'])
            identity = {k: quota[k] for k in ('ProviderCode', 'QuotaCode', 'Dimensions')}
            if identity != item['quota_identity']:
                raise ValueError('Quota identity/dimensions mismatch')
            region = data['context']['region']
            if not any(isinstance(d, dict) and d.get('Name') == 'region_code' and d.get('Value') == region
                       for d in identity['Dimensions']):
                raise ValueError('Quota dimensions do not cover selected region')
            if not isinstance(quota.get('Trn'), str):
                raise ValueError('Quota TRN missing')
            trn = quota['Trn'].split(':')
            if (len(trn) != 7 or trn[:3] != ['trn', 'quota', region] or
                    trn[5] != str(data['context']['account']) or
                    trn[6] != identity['ProviderCode'] + '/' + identity['QuotaCode']):
                raise ValueError('Quota TRN does not bind account, region and quota identity')
            key = json.dumps([identity, item.get('object_scope')], sort_keys=True)
            if key in identities:
                raise ValueError('Conflicting or duplicate quota/object calculation')
            identities.add(key)
            # Only the same selected quota object supplies adjustability and limit.
            limit = count(quota['TotalQuota'], 'TotalQuota')
            if item.get('usage_mode') == 'quota_total':
                if item.get('object_scope'):
                    raise ValueError('Quota total cannot be applied to an individual object')
                usage = quota['TotalUsage']
                if not isinstance(usage, dict) or usage.get('Unit') not in ('个', 'count', 'Count'):
                    raise ValueError('Unrecognized count unit')
                used = count(usage['Value'], 'TotalUsage.Value')
            elif item.get('usage_mode') == 'object_counter':
                # Accept an API-provided quota counter only, not len(arbitrarily filtered rules).
                obj = evidence.read(item['usage_object_ref'])
                if pointer(obj, item['object_id_pointer']) != item.get('object_scope'):
                    raise ValueError('Usage counter belongs to another object')
                if item.get('accounting_verified') is not True or not item.get('accounting_basis'):
                    raise ValueError('Counter semantics/default-rule inclusion unresolved')
                used = count(pointer(obj, item['counter_pointer']), 'object quota counter')
            else:
                raise ValueError('Use quota_total or a verified object_counter; ambiguous rule counting remains unknown')
            growth = count(item['growth'], 'growth')
            row.update(quota_identity=identity, limit=limit, used=used, growth=growth,
                       remaining=limit-used, remaining_after=limit-used-growth,
                       utilization_percent=round(100*used/limit, 6) if limit else None,
                       applicable_type=quota.get('ApplicableType'),
                       applicable_value_raw=quota.get('ApplicableValue'),
                       status='fits' if used+growth <= limit else 'exceeds',
                       quota_ref=item['quota_ref'])
            if item['usage_mode'] == 'object_counter':
                row['calculation_status'] = row['status']
                row['status'] = 'conditional'
                row['reason'] = ('Object ID/counter is bound, but this generic checker cannot prove the quota applies '
                                 'to that counter, direction or address family; requires a product accounting adapter')
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            row['reason'] = str(exc)
        rows.append(row)
    # Duplicate IDs make answer references ambiguous; fail instead of silently picking one.
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate item id')
    overall = ('unknown' if not rows or any(r['status'] in ('unknown','conditional') for r in rows)
               else 'exceeds' if any(r['status'] == 'exceeds' for r in rows) else 'fits')
    result = {'items': rows, 'overall': overall, 'evidence': evidence.used,
              'boundary': 'Saved quota values only; not creation feasibility or allocation approval'}
    result['answer_table'] = '\n'.join(['|对象|上限|用量|余量|增量|计划后余量|使用率%|状态|限制|',
                                        '|---|---:|---:|---:|---:|---:|---:|---|---|'] + [
        '|'+'|'.join(str(r.get(k, '未知')).replace('|', '\\|').replace('\n', ' ')
                     for k in ('id','limit','used','remaining','growth','remaining_after','utilization_percent','status','reason'))+'|'
        for r in rows])
    result['answer_check'] = verify_claims(result, data.get('answer_claims', []))
    return result


def compare_inventory(data, bundle):
    """Compare object counters in two complete captures, never assign quota maxima to objects."""
    result = {'status': 'unknown', 'items': [], 'evidence': {},
              'boundary': 'Inventory changes only; absence is not proof of deletion or risk improvement. No historical attachment or causal inference.'}
    try:
        if _proof.timestamp(data['before']['window']['end']) >= _proof.timestamp(data['after']['window']['start']):
            raise ValueError('Require ordered, non-overlapping snapshot windows')
        if data['inventory_action'] not in ('DescribeVpcs', 'DescribeSubnets', 'DescribeRouteTableList'):
            raise ValueError('Use object inventories, not quota maxima, for object-level history')
        snapshots = []
        request_scope = None
        for side in ('before', 'after'):
            evidence = Evidence(bundle, data['context'], data[side]['window'])
            objects, seen, total = {}, set(), None
            try:
                for page in data[side]['pages']:
                    if page['items_ref']['source'] != page['total_ref']['source']:
                        raise ValueError('Inventory and total must come from the same response')
                    request = bundle[page['items_ref']['source']]['request']
                    if not isinstance(request, dict):
                        raise ValueError('Original inventory request missing')
                    filters = {k: v for k, v in request.items()
                               if k not in ('PageNumber', 'PageSize', 'NextToken', 'MaxResults')}
                    if filters.get('Action') != data['inventory_action']:
                        raise ValueError('Inventory action mismatch')
                    if request_scope is not None and request_scope != filters:
                        raise ValueError('Snapshot query scopes differ')
                    request_scope = filters
                    entries = evidence.read(page['items_ref'])
                    page_total = evidence.read(page['total_ref'])
                    if type(page_total) is not int or page_total < 0 or (total is not None and total != page_total):
                        raise ValueError('Unstable inventory total')
                    total = page_total
                    if not isinstance(entries, list):
                        raise ValueError('Inventory missing')
                    for obj in entries:
                        ident = pointer(obj, data['fields']['id'])
                        if not isinstance(ident, str) or not ident or ident in seen:
                            raise ValueError('Missing or duplicate object ID')
                        seen.add(ident)
                        scope = {k: pointer(obj, v) for k, v in data['fields']['scope'].items()}
                        if not scope or any(not isinstance(v, str) or not v for v in scope.values()):
                            raise ValueError('Object scope missing')
                        if not data['scope'] or not set(data['scope']).issubset(scope):
                            raise ValueError('Requested scope fields missing')
                        in_scope = all(scope[k] == v for k, v in data['scope'].items())
                        # Retain identity/scope to distinguish a moved object from an absent one.
                        # Do not output metrics for objects outside the requested project/VPC.
                        metrics = ({k: pointer(obj, v) for k, v in data['fields']['metrics'].items()}
                                   if in_scope else None)
                        objects[ident] = {'scope': scope, 'metrics': metrics}
                if total is None or len(seen) != total:
                    raise ValueError('Incomplete inventory')
                snapshots.append(objects)
            finally:
                result['evidence'].update(evidence.used)
        before, after = snapshots
        rows = []
        relevant = lambda items: {key for key, obj in items.items() if obj['metrics'] is not None}
        for ident in sorted(relevant(before) | relevant(after)):
            old, new = before.get(ident), after.get(ident)
            row = {'id': ident, 'status': 'comparable', 'before': old, 'after': new}
            if old is None or new is None:
                row['status'] = 'newly_observed' if old is None else 'no_longer_observed'
            elif old['scope'] != new['scope']:
                row['status'] = 'scope_changed'
            else:
                row['delta'] = {}
                for key in old['metrics']:
                    a, b = old['metrics'][key], new['metrics'][key]
                    if type(a) is not int or type(b) is not int or a < 0 or b < 0:
                        row['delta'][key] = None
                        row['status'] = 'incomparable'
                    else:
                        row['delta'][key] = b - a
            rows.append(row)
        result.update(status='compared', items=rows, before_count=len(relevant(before)), after_count=len(relevant(after)))
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        result['reason'] = str(exc)
    result['answer_check'] = verify_claims(result, data.get('answer_claims', []))
    return result


def run(data, evidence=None):
    if not isinstance(data, dict):
        raise ValueError('snapshot must be an object')
    if data['operation'] == 'compare_inventory':
        return compare_inventory(data, evidence)
    if data['operation'] == 'capacity_evidence':
        return capacity_evidence(data, evidence)
    if data['operation'] == 'capacity':
        return capacity(data['items'])
    if data['operation'] == 'addresses':
        return addresses(data['parent'], data['cidrs'])
    raise ValueError('operation must be compare_inventory, capacity_evidence, capacity or addresses')


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
