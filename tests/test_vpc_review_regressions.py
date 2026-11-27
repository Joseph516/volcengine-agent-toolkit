"""Offline regressions for observed failure patterns; synthetic captures, no case IDs/cloud."""
import copy
import unittest
from test_vpc_evidence_checks import NET, CAP, base, capture, ref, WINDOW
import test_vpc_evidence_checks as fixtures


class RouteEvidence(unittest.TestCase):
    def fixture(self):
        data = base('route_evidence')
        data.update(destination='192.0.2.10', table_id='vtb-example',
                    table_ref=ref('table', '/Result/Table'), table_id_pointer='/Id',
                    subnet_id='subnet-example', subnet_ref=ref('subnet', '/Result/Subnet'),
                    subnet_id_pointer='/Id', associated_table_pointer='/RouteTableId',
                    route_pages=[{'items_ref': ref('routes', '/Result/Entries'),
                                  'total_ref': ref('routes', '/Result/TotalCount')}],
                    fields={'id': '/Id', 'table_id': '/TableId', 'cidr': '/Cidr', 'next_hop': '/NextHop'})
        bundle = {'table': capture({'Table': {'Id': 'vtb-example', 'SubnetIds': None}}),
                  'subnet': capture({'Subnet': {'Id': 'subnet-example', 'RouteTableId': 'vtb-example'}}),
                  'routes': capture({'TotalCount': 2, 'Entries': [
                      {'Id': 'r-wide', 'TableId': 'vtb-example', 'Cidr': '192.0.0.0/16', 'NextHop': 'eni-wide'},
                      {'Id': 'r-detail', 'TableId': 'vtb-example', 'Cidr': '192.0.2.0/24', 'NextHop': 'eni-detail'}]},
                      'DescribeRouteEntryList')}
        bundle['routes']['request']['RouteTableId'] = 'vtb-example'
        return data, bundle

    def test_positive_selection_binding_and_honest_claims(self):
        d, b = self.fixture()
        d['answer_claims'] = [{'field': '/effective_for_subnet', 'value': True},
                              {'field': '/correct_gateway', 'value': 'not_verified'}]
        r = NET.run(d, b)
        self.assertEqual(r['candidates'][0]['next_hop'], 'eni-detail')
        self.assertTrue(r['effective_for_subnet'])
        self.assertEqual(r['answer_check']['status'], 'consistent')
        self.assertEqual(r['runtime_reachability'], 'not_verified')

    def test_citation_action_request_and_paths_are_from_same_capture(self):
        d, b = self.fixture()
        r = NET.run(d, b)
        source = r['evidence']['routes']
        self.assertEqual(source['request_id'], b['routes']['response']['ResponseMetadata']['RequestId'])
        self.assertEqual(source['action'], 'DescribeRouteEntryList')
        self.assertEqual(source['pointers'], ['/Result/Entries', '/Result/TotalCount'])

    def test_unknown_false_true_distinct_and_caller_boolean_ignored(self):
        for actual, expected in [(None, None), ('vtb-other', False), ('vtb-example', True)]:
            d, b = self.fixture(); d['associated'] = True
            b['subnet']['response']['Result']['Subnet']['RouteTableId'] = actual
            r = NET.run(d, b)
            self.assertIs(r['effective_for_subnet'], expected)
            self.assertEqual(r['status'], 'selected')
        d, b = self.fixture(); del d['subnet_ref']; d['associated'] = False
        r = NET.run(d, b)
        self.assertIsNone(r['effective_for_subnet'])

    def test_legacy_boolean_never_proves_association(self):
        for value in (None, False, True):
            r = NET.run({'operation': 'route', 'destination': '192.0.2.1', 'associated': value,
                         'routes': [{'cidr': '192.0.2.0/24'}], 'complete': True, 'evidence': ['claimed']})
            self.assertIsNone(r['effective_for_subnet'])

    def test_null_not_false_and_unknown_not_confirmed_in_answer(self):
        d, b = self.fixture(); b['subnet']['response']['Result']['Subnet']['RouteTableId'] = None
        d['answer_claims'] = [{'field': '/effective_for_subnet', 'value': False},
                              {'field': '/correct_gateway', 'value': 'confirmed'}]
        r = NET.run(d, b)
        self.assertEqual(r['answer_check']['status'], 'mismatch')
        self.assertEqual(len(r['answer_check']['errors']), 2)

    def test_bad_pages_filters_or_time_cannot_establish_selection(self):
        for fault in ('total', 'duplicate', 'filter', 'time', 'foreign', 'response_scope'):
            d, b = self.fixture()
            if fault == 'total': b['routes']['response']['Result']['TotalCount'] = 3
            if fault == 'duplicate': b['routes']['response']['Result']['Entries'][1]['Id'] = 'r-wide'
            if fault == 'filter': b['routes']['request']['DestinationCidrBlock'] = '192.0.2.0/24'
            if fault == 'time': b['routes']['observed_at'] = '2026-02-01T10:00:00+08:00'
            if fault == 'foreign': b['routes']['request']['RouteTableId'] = 'vtb-other'
            if fault == 'response_scope': b['routes']['response']['Result']['Entries'][0]['TableId'] = 'vtb-other'
            self.assertEqual(NET.run(d, b)['status'], 'unknown', fault)

    def test_current_subnet_cannot_prove_historical_binding(self):
        d, b = self.fixture()
        b['subnet']['observed_at'] = '2026-02-01T10:00:00+08:00'
        r = NET.run(d, b)
        self.assertEqual(r['status'], 'selected')
        self.assertIsNone(r['effective_for_subnet'])


class ScopeAnswerChecks(unittest.TestCase):
    def test_multi_address_is_not_multiple_enis(self):
        d, b = fixtures.BoundNetwork().scope()
        b['eni']['response']['Result']['Enis'][0]['IPs'].append({'Address': '198.51.100.3'})
        d['answer_claims'] = [{'field': '/affected_eni_count', 'value': 3}]
        r = NET.run(d, b)
        self.assertEqual((r['affected_eni_count'], r['destination_address_count']), (2, 3))
        self.assertEqual(r['answer_check']['status'], 'mismatch')

    def test_ipv6_scope_excludes_ipv4_only_members(self):
        d, b = fixtures.BoundNetwork().scope()
        d.update(family=6, rule_sources=['2001:db8::1/128'],
                 allowed={'sources':['2001:db8::1/128'], 'destinations':['2001:db8:1::1/128']})
        b['eni']['response']['Result']['Enis'][0]['IPs'].append({'Address':'2001:db8:1::1'})
        r = NET.run(d, b)
        self.assertEqual(r['status'], 'within_scope')
        self.assertEqual(r['affected_eni_count'], 1)
        self.assertEqual(r['destination_address_count'], 1)
        self.assertEqual(r['affected_enis'][0]['addresses'], ['2001:db8:1::1'])

    def test_nested_claim_types_must_match(self):
        for checker in (NET, CAP):
            for actual in ({'free':True}, {'free':[True]}):
                expected = {'free':1} if not isinstance(actual['free'], list) else {'free':[1]}
                r = checker.verify_claims({'delta': expected}, [{'field':'/delta', 'value':actual}])
                self.assertEqual(r['status'], 'mismatch')
            self.assertEqual(checker.verify_claims({'delta':{'free':[1, None]}},
                [{'field':'/delta', 'value':{'free':[1, None]}}])['status'], 'consistent')

    def test_single_source_and_destination_positive_scope(self):
        d, b = fixtures.BoundNetwork().scope()
        b['eni']['response']['Result']['Enis'][1]['Groups'] = ['sg-other']
        r = NET.run(d, b)
        self.assertEqual(r['status'], 'within_scope')
        self.assertFalse(r['minimum_change_verified'])

    def test_bound_configuration_cannot_claim_unique_root_cause(self):
        d, b = fixtures.BoundNetwork().path()
        d['answer_claims'] = [{'field': '/unique_root_cause', 'value': 'confirmed'},
                              {'field': '/runtime_reachability', 'value': 'reachable'}]
        self.assertEqual(NET.run(d, b)['answer_check']['status'], 'mismatch')


class HistoricalInventory(unittest.TestCase):
    def fixture(self):
        d = base('compare_inventory'); del d['window']
        d.update(inventory_action='DescribeSubnets', scope={'project': 'example'},
                 fields={'id': '/Id', 'scope': {'project': '/ProjectName', 'vpc': '/VpcId'},
                         'metrics': {'free': '/Free'}})
        b = {}
        for side, day, free in [('before', '01', 10), ('after', '02', 8)]:
            d[side] = {'window': {k: v.replace('2026-01-01', '2026-01-'+day) for k, v in WINDOW.items()},
                       'pages': [{'items_ref': ref(side, '/Result/Objects'),
                                  'total_ref': ref(side, '/Result/TotalCount')}]}
            b[side] = capture({'TotalCount': 2, 'Objects': [
                {'Id': 'subnet-example', 'Name': side, 'ProjectName': 'example', 'VpcId': 'vpc-example', 'Free': free},
                {'Id': 'subnet-outside', 'ProjectName': 'other', 'VpcId': 'vpc-other', 'Free': 100}]}, 'DescribeSubnets')
            b[side]['observed_at'] = '2026-01-'+day+'T10:00:01+08:00'
        return d, b

    def test_same_id_rename_negative_delta_actual_project_filter(self):
        d, b = self.fixture(); r = CAP.run(d, b)
        self.assertEqual(r['status'], 'compared')
        self.assertEqual((r['before_count'], r['after_count']), (1, 1))
        self.assertEqual(r['items'][0]['id'], 'subnet-example')
        self.assertEqual(r['items'][0]['delta']['free'], -2)

    def test_missing_object_not_zero_or_improvement(self):
        d, b = self.fixture(); b['after']['response']['Result']['Objects'].pop(0)
        b['after']['response']['Result']['TotalCount'] = 1
        row = CAP.run(d, b)['items'][0]
        self.assertEqual(row['status'], 'no_longer_observed')
        self.assertNotIn('delta', row)

    def test_scope_transfer_null_counter_and_identity_copy(self):
        d, b = self.fixture(); b['after']['response']['Result']['Objects'][0]['VpcId'] = 'vpc-other'
        self.assertEqual(CAP.run(d, b)['items'][0]['status'], 'scope_changed')
        d, b = self.fixture(); b['after']['response']['Result']['Objects'][0]['Free'] = None
        self.assertIsNone(CAP.run(d, b)['items'][0]['delta']['free'])
        d, b = self.fixture(); d['answer_claims'] = [{'field': '/items/0/id', 'value': 'subnet-typo'}]
        self.assertEqual(CAP.run(d, b)['answer_check']['status'], 'mismatch')

    def test_project_migration_preserves_identity_but_not_outside_metrics(self):
        for side, counts in [('after', (1,0)), ('before', (0,1))]:
            d, b = self.fixture()
            b[side]['response']['Result']['Objects'][0]['ProjectName'] = 'other'
            r = CAP.run(d, b)
            self.assertEqual((r['before_count'], r['after_count']), counts)
            self.assertEqual(len(r['items']), 1)
            row = r['items'][0]
            self.assertEqual(row['id'], 'subnet-example')
            self.assertEqual(row['status'], 'scope_changed')
            self.assertIsNone(row[side]['metrics'])
            self.assertNotIn('delta', row)

    def test_incomplete_scope_time_or_quota_maximum_rejected(self):
        for fault in ('total', 'scope', 'time', 'quota'):
            d, b = self.fixture()
            if fault == 'total': b['after']['response']['Result']['TotalCount'] = 3
            if fault == 'scope': b['after']['request']['ProjectName'] = 'other'
            if fault == 'time': d['after']['window'] = copy.deepcopy(d['before']['window'])
            if fault == 'quota': d['inventory_action'] = 'ListProductQuotas'
            self.assertEqual(CAP.run(d, b)['status'], 'unknown', fault)

    def test_complete_second_page_and_positive_growth(self):
        d, b = self.fixture()
        b['after']['response']['Result']['Objects'][0]['Free'] = 12
        # Split a complete query across pages; preserve ID and total checks before project filtering.
        b['after-2'] = copy.deepcopy(b['after'])
        b['after']['response']['Result']['Objects'] = b['after']['response']['Result']['Objects'][:1]
        b['after-2']['response']['Result']['Objects'] = b['after-2']['response']['Result']['Objects'][1:]
        b['after']['request']['PageNumber'] = 1; b['after-2']['request']['PageNumber'] = 2
        d['after']['pages'].append({'items_ref': ref('after-2','/Result/Objects'), 'total_ref': ref('after-2','/Result/TotalCount')})
        self.assertEqual(CAP.run(d, b)['items'][0]['delta']['free'], 2)




class QuotaDimensionMetadata(unittest.TestCase):
    def test_localized_dimension_labels_do_not_invalidate_matching_region(self):
        d, b = fixtures.BoundCapacity().fixture()
        dimensions = b['quota']['response']['Result']['Quotas'][0]['Dimensions']
        dimensions[0].update(NameCn='地域', ValueCn='示例地域')
        d['items'][0]['quota_identity']['Dimensions'] = copy.deepcopy(dimensions)
        self.assertEqual(CAP.run(d, b)['items'][0]['status'], 'exceeds')
        dimensions[0]['Value'] = 'other-region'
        d['items'][0]['quota_identity']['Dimensions'] = copy.deepcopy(dimensions)
        self.assertEqual(CAP.run(d, b)['items'][0]['status'], 'unknown')


class ReviewBoundaryRegressions(unittest.TestCase):
    def test_null_request_returns_unknown_instead_of_traceback(self):
        for fixture, module, source in [(RouteEvidence, NET, 'routes'), (HistoricalInventory, CAP, 'before')]:
            for raw in (None, [], 'missing'):
                d, b = fixture().fixture()
                b[source]['request'] = raw
                self.assertEqual(module.run(d, b)['status'], 'unknown')

    def test_legacy_no_match_and_incomplete_keep_fixed_unknown_fields(self):
        for complete in (True, False):
            data = {'operation': 'route', 'destination': '192.0.2.1', 'complete': complete,
                    'evidence': ['snapshot'], 'routes': [{'cidr': '198.51.100.0/24'}],
                    'answer_claims': [{'field': '/effective_for_subnet', 'value': None},
                                      {'field': '/correct_gateway', 'value': 'not_verified'}]}
            result = NET.run(data)
            self.assertEqual(result['status'], 'no_match' if complete else 'unknown')
            self.assertEqual(result['answer_check']['status'], 'consistent')


if __name__ == '__main__':
    unittest.main()
