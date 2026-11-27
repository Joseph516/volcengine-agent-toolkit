"""Evidence-bound regressions using synthetic API captures, never cloud resources."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'plugins/volcengine-vpc/skills'
def load(skill, filename):
    spec = importlib.util.spec_from_file_location(filename, ROOT/skill/'scripts'/(filename+'.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module
CAP = load('volcengine-vpc-quota-capacity', 'check_capacity')
NET = load('volcengine-vpc-network-path', 'check_network')
CONTEXT = {'account':'example-account', 'region':'example-region'}
WINDOW = {'start':'2026-01-01T10:00:00+08:00','end':'2026-01-01T10:01:00+08:00'}
def capture(result, action='ReadExample'):
    return {'kind':'current_configuration','observed_at':'2026-01-01T10:00:01+08:00',
            'context':copy.deepcopy(CONTEXT),'request':{'Action':action,'Region':'example-region'},
            'response':{'ResponseMetadata':{'RequestId':'req-example'},'Result':result}}
def ref(source, pointer):return {'source':source,'pointer':pointer}
def base(op):return {'operation':op,'context':copy.deepcopy(CONTEXT),'window':copy.deepcopy(WINDOW)}

class BoundCapacity(unittest.TestCase):
    def fixture(self):
        identity={'ProviderCode':'vpc','QuotaCode':'vpc','Dimensions':[{'Name':'region_code','Value':'example-region'}]}
        quota=dict(identity,TotalQuota=100,TotalUsage={'Unit':'count','Value':9},ApplicableType='enum',ApplicableValue=[10,50],Trn='trn:quota:example-region:::example-account:vpc/vpc')
        neighbor=dict(quota,QuotaCode='subnet',ApplicableValue=[200,250])
        bundle={'quota':capture({'Quotas':[quota,neighbor]})}
        d=base('capacity_evidence');d['items']=[{'id':'vpc','quota_ref':ref('quota','/Result/Quotas/0'),
            'quota_identity':identity,'usage_mode':'quota_total','growth':92}]
        return d,bundle
    def test_derived_numbers_and_neighbor_claim_rejected(self):
        d,b=self.fixture();d['answer_claims']=[{'field':'/items/0/applicable_value_raw','value':[200,250]},
                                             {'field':'/items/0/remaining','value':91}]
        r=CAP.run(d,b);row=r['items'][0]
        self.assertEqual((row['remaining'],row['remaining_after'],row['status']), (91,-1,'exceeds'))
        self.assertEqual(row['utilization_percent'],9.0)
        self.assertEqual(r['answer_check']['status'],'mismatch')
        self.assertEqual(len(r['answer_check']['errors']),1)
        self.assertIn('|vpc|100|9|91|92|-1|9.0|',r['answer_table'])
    def test_null_api_metadata_usage_and_trn_fail_closed(self):
        for fault in ('metadata','usage','trn'):
            d,b=self.fixture()
            if fault=='metadata':b['quota']['response']['ResponseMetadata']=None
            else:b['quota']['response']['Result']['Quotas'][0]['TotalUsage' if fault=='usage' else 'Trn']=None
            self.assertEqual(CAP.run(d,b)['items'][0]['status'],'unknown')
    def test_selected_neighbor_not_matching_quota_is_unknown(self):
        d,b=self.fixture();d['items'][0]['quota_ref']['pointer']='/Result/Quotas/1'
        self.assertEqual(CAP.run(d,b)['items'][0]['status'],'unknown')
    def test_missing_reference_and_cross_region_are_unknown(self):
        for mutation in ('missing','region','failed','time'):
            d,b=self.fixture()
            if mutation=='missing':del b['quota']
            elif mutation=='region':b['quota']['context']['region']='other'
            elif mutation=='failed':b['quota']['response']['ResponseMetadata']['Error']={'Code':'Denied'}
            else:b['quota']['observed_at']='2026-01-02T10:00:01+08:00'
            self.assertEqual(CAP.run(d,b)['items'][0]['status'],'unknown')
    def test_object_counter_requires_same_object_and_semantics(self):
        d,b=self.fixture();x=d['items'][0];x.update(usage_mode='object_counter',object_scope='acl-example',
            usage_object_ref=ref('object','/Result/ACL'),object_id_pointer='/Id',counter_pointer='/QuotaUsed',
            accounting_verified=True,accounting_basis='captured API quota counter definition')
        b['object']=capture({'ACL':{'Id':'acl-example','QuotaUsed':99}})
        r=CAP.run(d,b);self.assertEqual(r['items'][0]['used'],99)
        self.assertEqual(r['items'][0]['status'],'conditional');self.assertEqual(r['overall'],'unknown')
        self.assertIn('|conditional|',r['answer_table']);self.assertIn('generic checker',r['answer_table'])
        x['accounting_verified']=False
        self.assertEqual(CAP.run(d,b)['items'][0]['status'],'unknown')
        x['accounting_verified']=True;b['object']['response']['Result']['ACL']['Id']='acl-other'
        self.assertEqual(CAP.run(d,b)['items'][0]['status'],'unknown')
    def test_duplicate_scope_cannot_be_two_actual_answers(self):
        d,b=self.fixture();x=copy.deepcopy(d['items'][0]);x['id']='second';d['items'].append(x)
        self.assertEqual(CAP.run(d,b)['items'][1]['status'],'unknown')
    def test_zero_limit_unknown_percentage_and_exact_boundary(self):
        d,b=self.fixture();q=b['quota']['response']['Result']['Quotas'][0]
        q['TotalQuota']=0;q['TotalUsage']['Value']=0;d['items'][0]['growth']=0
        r=CAP.run(d,b)['items'][0];self.assertEqual(r['status'],'fits');self.assertIsNone(r['utilization_percent'])
    def test_claim_checks_do_not_validate_undeclared_prose_or_booleans(self):
        self.assertEqual(CAP.verify_claims({'count':1},[])['status'],'not_checked')
        self.assertEqual(CAP.verify_claims({'count':1},[{'field':'/count','value':True}])['status'],'mismatch')
        self.assertEqual(CAP.verify_claims({},[{'field':'/missing','value':0}])['status'],'mismatch')

    def test_wrong_raw_request_region_and_quota_owner_rejected(self):
        for fault in ('request','response','trn','dimensions'):
            d,b=self.fixture();q=b['quota']['response']['Result']['Quotas'][0]
            if fault=='request':b['quota']['request']['Region']='other'
            elif fault=='response':b['quota']['response']['ResponseMetadata']['Region']='other'
            elif fault=='trn':q['Trn']='trn:quota:example-region:::foreign:vpc/vpc'
            else:
                q['Dimensions']=[{'Name':'region_code','Value':'other'}]
                d['items'][0]['quota_identity']['Dimensions']=q['Dimensions']
            self.assertEqual(CAP.run(d,b)['items'][0]['status'],'unknown')

class BoundNetwork(unittest.TestCase):
    def scope(self):
        d=base('sg_scope');d.update(security_group_id='sg-example',family=4,inventory_action='DescribeNetworkInterfaces',
            eni_pages=[{'items_ref':ref('eni','/Result/Enis'),'total_ref':ref('eni','/Result/TotalCount')}],
            fields={'id':'/Id','security_groups':'/Groups','addresses':[{'pointer':'/IPs','value_pointer':'/Address'}]},
            rule_sources=['192.0.2.1/32'],allowed={'sources':['192.0.2.1/32'],'destinations':['198.51.100.1/32']},
            proposed={'destinations':['198.51.100.1/32']})
        b={'eni':capture({'TotalCount':2,'Enis':[
            {'Id':'eni-one','Groups':['sg-example'],'IPs':[{'Address':'198.51.100.1'}]},
            {'Id':'eni-unattached','Groups':['sg-example'],'IPs':[{'Address':'198.51.100.2'}],'InstanceId':''}]},
            'DescribeNetworkInterfaces')}
        return d,b
    def test_shared_group_derives_unattached_destination(self):
        d,b=self.scope();r=NET.run(d,b)
        self.assertEqual(r['status'],'expands');self.assertEqual(r['extra']['destinations'],['198.51.100.2/32'])
        self.assertFalse(r['minimum_change_verified'])
    def test_prefix_widening_is_not_single_source(self):
        d,b=self.scope();d['rule_sources']=['192.0.2.0/24']
        r=NET.run(d,b);self.assertEqual(r['status'],'expands');self.assertTrue(r['extra']['sources'])
    def test_ipv6_prefix_widening_and_secondary_addresses(self):
        d,b=self.scope();d['family']=6;d['rule_sources']=['2001:db8::/64']
        d['allowed']={'sources':['2001:db8::1/128'],'destinations':['2001:db8:1::1/128']}
        enis=b['eni']['response']['Result']['Enis'];enis[0]['IPs']=[{'Address':'2001:db8:1::1'},{'Address':'2001:db8:1::2'}]
        enis[1]['IPs']=[{'Address':'2001:db8:1::3'}]
        r=NET.run(d,b);self.assertEqual(r['status'],'expands');self.assertEqual(len(r['derived_destinations']),3)
    def test_partial_filtered_null_or_duplicate_inventory_unknown(self):
        for fault in ('partial','filter','null','duplicate'):
            d,b=self.scope();raw=b['eni']['response']['Result']
            if fault=='partial':raw['TotalCount']=3
            elif fault=='filter':b['eni']['request']['InstanceId']='ecs-example'
            elif fault=='null':raw['Enis'][1]['Groups']=None
            else:raw['Enis'][1]['Id']='eni-one'
            self.assertEqual(NET.run(d,b)['status'],'unknown')
    def path(self):
        d=base('path_evidence');d['gates']={};d['endpoints']={}
        b={'policy':capture({'Policy':{'Id':'policy-example','Rules':[], 'TotalCount':0}})}
        for side, ip in [('source','192.0.2.1'),('destination','198.51.100.1')]:
            b[side]=capture({'Object':{'Id':'eni-'+side,'SubnetId':'subnet-'+side,'IPs':[ip],
                                      'Policies':['policy-example']},
                             'Subnet':{'Id':'subnet-'+side,'Policies':['policy-example']}})
            d['endpoints'][side]={'ref':ref(side,'/Result/Object'),'id_pointer':'/Id','subnet_pointer':'/SubnetId',
                                 'addresses_pointer':'/IPs','ip':ip}
        owners={'source_sg_egress':('source','Object'),'destination_sg_ingress':('destination','Object'),
                'source_acl_egress':('source','Subnet'),'source_acl_ingress_return':('source','Subnet'),
                'destination_acl_ingress':('destination','Subnet'),'destination_acl_egress_return':('destination','Subnet'),
                'forward_route':('source','Subnet'),'return_route':('destination','Subnet')}
        for name,(side,obj) in owners.items():
            d['gates'][name]={'target_id':('eni-' if obj=='Object' else 'subnet-')+side,
                'target_ref':ref(side,'/Result/'+obj+'/Id'),
                'association_ref':ref(side,'/Result/'+obj+'/Policies'),
                'policies':[{'id':'policy-example','ref':ref('policy','/Result/Policy'),'id_pointer':'/Id',
                             'rules_pointer':'/Rules','rules_total_pointer':'/TotalCount'}],
                'decision':'allow','complete':True,'semantics_verified':True,'decision_basis':'Synthetic reviewed policy'}
        return d,b
    def test_bound_path_is_still_not_runtime_proof(self):
        d,b=self.path();r=NET.run(d,b);self.assertEqual(r['configuration'],'allows')
        self.assertEqual(r['unique_root_cause'],'not_verified');self.assertEqual(r['runtime_reachability'],'not_verified')
    def test_overriding_gate_time_cannot_turn_current_into_historical(self):
        d,b=self.path();d['window']={'start':'2025-12-01T10:00:00+08:00','end':'2025-12-01T10:01:00+08:00'}
        for g in d['gates'].values():g.update(observed_at=d['window']['start'],decision='deny')
        r=NET.run(d,b);self.assertEqual(r['configuration'],'unknown');self.assertFalse(r['blocked_gates'])
    def test_same_window_different_observation_times_allowed(self):
        d,b=self.path();b['policy']['observed_at']='2026-01-01T10:00:59+08:00'
        self.assertEqual(NET.run(d,b)['configuration'],'allows')
    def test_historical_interval_must_come_from_original_record(self):
        d,b=self.path()
        for source in b.values():
            source.update(kind='historical_configuration',valid_from_pointer='/Result/From',valid_to_pointer='/Result/To')
            source['response']['Result'].update(From=WINDOW['start'],To=WINDOW['end'])
        self.assertEqual(NET.run(d,b)['configuration'],'allows')
        b['policy']['response']['Result']['To']='2025-12-01T10:00:00+08:00'
        self.assertEqual(NET.run(d,b)['configuration'],'unknown')
    def test_null_association_not_absence_and_omitted_sg_unknown(self):
        for association in (None,['sg-example','sg-extra']):
            d,b=self.path();b['source']['response']['Result']['Object']['Policies']=association
            self.assertEqual(NET.run(d,b)['configuration'],'unknown')
    def test_foreign_object_association_cannot_be_joined(self):
        d,b=self.path();b['source']['response']['Result']['Other']={'Policies':['policy-example']}
        for g in d['gates'].values():g['association_ref']=ref('source','/Result/Other/Policies')
        self.assertEqual(NET.run(d,b)['configuration'],'unknown')
    def test_source_policy_cannot_be_reused_for_all_gates(self):
        d,b=self.path()
        gate=copy.deepcopy(d['gates']['source_sg_egress'])
        d['gates']={name:copy.deepcopy(gate) for name in d['gates']}
        r=NET.run(d,b);self.assertEqual(r['configuration'],'unknown')
        self.assertIn('destination_sg_ingress',r['unknown_gates'])
        self.assertIn('forward_route',r['unknown_gates'])
    def test_null_or_incomplete_rules_cannot_prove_allow(self):
        for rules in (None,[]):
            d,b=self.path();p=b['policy']['response']['Result']['Policy'];p['Rules']=rules;p['TotalCount']=2
            self.assertEqual(NET.run(d,b)['configuration'],'unknown')
    def test_endpoint_ip_must_belong_to_bound_eni(self):
        d,b=self.path();d['endpoints']['source']['ip']='192.0.2.99'
        self.assertEqual(NET.run(d,b)['configuration'],'unknown')

    def test_missing_evidence_cli_returns_json_error(self):
        with tempfile.TemporaryDirectory() as folder:
            d,b=self.path();p=Path(folder)/'input.json';p.write_text(json.dumps(d))
            script=ROOT/'volcengine-vpc-network-path/scripts/check_network.py'
            result=subprocess.run([sys.executable,str(script),'--snapshot',str(p)],capture_output=True,text=True)
            self.assertEqual(result.returncode,1);self.assertIn('error',json.loads(result.stdout))
            self.assertNotIn('Traceback',result.stderr)

if __name__=='__main__':unittest.main()
