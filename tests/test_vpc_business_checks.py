"""Synthetic invariants for capacity, CIDR and effective path calculations."""
import copy
import importlib.util
from pathlib import Path
import unittest
import json
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1] / 'plugins/volcengine-vpc/skills'


def load(skill, script):
    spec = importlib.util.spec_from_file_location(script, ROOT / skill / 'scripts' / (script+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CAP = load('volcengine-vpc-quota-capacity', 'check_capacity')
NET = load('volcengine-vpc-network-path', 'check_network')


class CapacityChecks(unittest.TestCase):
    def item(self):
        scope = {'account': 'example', 'region': 'example', 'quota': 'vpc'}
        return dict(scope=scope, usage_scope=scope.copy(), basis='actual', complete=True,
                    evidence=['saved quota response'], limit=1500, used=6, growth=1495)

    def test_actual_usage_preserves_negative_remaining(self):
        row = CAP.capacity([self.item()])['items'][0]
        self.assertEqual((row['remaining'], row['remaining_after'], row['status']), (1494, -1, 'exceeds'))

    def test_exact_boundary_fits(self):
        x = self.item(); x['growth'] = 1494
        self.assertEqual(CAP.capacity([x])['items'][0]['remaining_after'], 0)

    def test_defaults_missing_usage_or_mismatched_scope_unknown(self):
        for k,v in [('basis','default'),('complete',False),('used',None),('evidence',[]),('usage_scope',{'object':'different'})]:
            x = self.item(); x[k] = v
            self.assertEqual(CAP.capacity([x])['items'][0]['status'], 'unknown')

    def test_invalid_counts_rejected(self):
        for value in [True,-1,1.5,'3']:
            x = self.item();x['growth']=value
            with self.assertRaises(ValueError):CAP.capacity([x])

    def test_malformed_address_lists_rejected(self):
        for value in [{}, [1], "10.0.0.0/24", None]:
            with self.assertRaises(ValueError):
                CAP.addresses("0.0.0.0/0", value)

    def test_cidr_boundary_not_silently_normalized(self):
        result = CAP.addresses('10.0.0.0/16',['10.0.4.0/24','10.0.6.0/22'])
        self.assertFalse(result['valid'])

    def test_overlap_and_outside(self):
        for entries in [['10.0.4.0/22','10.0.5.0/24'],['10.1.0.0/24'],['2001:db8::/64']]:
            self.assertFalse(CAP.addresses('10.0.0.0/16',entries)['valid'])

    def test_ipv6_and_total_not_allocatable(self):
        r=CAP.addresses('2001:db8::/32',['2001:db8:1::/64'])
        self.assertTrue(r['valid']);self.assertEqual(r['entries'][0]['addresses'],2**64)


class NetworkChecks(unittest.TestCase):
    def test_malformed_network_lists_rejected(self):
        for value in [{}, [1], "192.0.2.0/24", None]:
            with self.assertRaises(ValueError):NET.networks(value)

    def test_null_gate_rejected(self):
        d=self.gates();d["gates"]["source_sg_egress"]=None
        with self.assertRaises(ValueError):NET.path(d)

    def test_199_hosts_not_equivalent_to_24(self):
        allowed = [f'192.0.2.{i}/32' for i in range(199)]
        extra=NET.difference(NET.networks(['192.0.2.0/24']),NET.networks(allowed))
        import ipaddress
        self.assertEqual(sum(ipaddress.ip_network(c).num_addresses for c in extra),57)

    def test_shared_destination_expansion(self):
        d={'allowed':{'sources':['192.0.2.1/32'],'destinations':['198.51.100.1/32']},
           'proposed':{'sources':['192.0.2.1/32'],'destinations':['198.51.100.1/32','198.51.100.2/32']}}
        r=NET.scope_change(d);self.assertEqual(r['status'],'expands')
        self.assertEqual(r['extra']['destinations'],['198.51.100.2/32'])

    def test_equivalent_split_and_family(self):
        self.assertEqual(NET.difference(NET.networks(['192.0.2.0/24']),NET.networks(['192.0.2.0/25','192.0.2.128/25'])),[])
        self.assertEqual(NET.difference(NET.networks(['2001:db8::/64']),NET.networks(['0.0.0.0/0'])),['2001:db8::/64'])

    def test_longest_prefix_and_unassociated(self):
        d={'destination':'192.0.2.10','complete':True,'evidence':['saved routes'], 'associated':False,
           'routes':[{'cidr':'192.0.0.0/16','next_hop':'a'},{'cidr':'192.0.2.0/24','next_hop':'b'}]}
        r=NET.route(d);self.assertEqual(r['candidates'][0]['next_hop'],'b');self.assertFalse(r['effective_for_subnet'])
        d['routes'].append({'cidr':'192.0.2.0/24','next_hop':'c'})
        self.assertEqual(NET.route(d)['status'],'ambiguous')
        d['complete']=False;self.assertEqual(NET.route(d)['status'],'unknown')

    def gates(self):
        names=['source_sg_egress','destination_sg_ingress','source_acl_egress','destination_acl_ingress',
               'destination_acl_egress_return','source_acl_ingress_return','forward_route','return_route']
        return {'target_time':'snapshot-a','gates':{n:dict(decision='allow',applicable=True,complete=True,
                observed_at='snapshot-a',evidence=['saved response']) for n in names}}

    def test_all_allow_is_configuration_only(self):
        d=self.gates();before=copy.deepcopy(d);r=NET.path(d)
        self.assertEqual(r['configuration'],'allows');self.assertEqual(r['runtime_reachability'],'not_verified');self.assertEqual(d,before)

    def test_missing_acl_return_is_unknown(self):
        d=self.gates();del d['gates']['source_acl_ingress_return']
        self.assertEqual(NET.path(d)['configuration'],'unknown')

    def test_unbound_acl_not_treated_as_blocker(self):
        d=self.gates();d['gates']['destination_acl_ingress'].update(decision='deny',applicable=False)
        r=NET.path(d);self.assertEqual(r['configuration'],'unknown');self.assertEqual(r['blocked_gates'],[])

    def test_historical_time_mismatch_unknown(self):
        d=self.gates();d['target_time']='historical'
        self.assertEqual(NET.path(d)['configuration'],'unknown')

    def test_confirmed_deny_retains_other_gaps(self):
        d=self.gates();d['gates']['source_sg_egress']['decision']='deny';del d['gates']['return_route']
        r=NET.path(d);self.assertEqual(r['configuration'],'blocked');self.assertIn('return_route',r['unknown_gates'])


class CliErrors(unittest.TestCase):
    def test_invalid_schema_emits_json_error(self):
        cases = [
            ('volcengine-vpc-quota-capacity', 'check_capacity', {'operation':'addresses','parent':'0.0.0.0/0','cidrs':{}}),
            ('volcengine-vpc-network-path', 'check_network', {'operation':'path','gates':{'source_sg_egress':None}}),
        ]
        for skill, script, data in cases:
            with tempfile.TemporaryDirectory() as folder:
                source = Path(folder)/'input.json';source.write_text(json.dumps(data))
                r = subprocess.run([sys.executable,str(ROOT/skill/'scripts'/(script+'.py')),'--snapshot',str(source)],capture_output=True,text=True)
                self.assertEqual(r.returncode,1);self.assertIn('error',json.loads(r.stdout));self.assertNotIn('Traceback',r.stderr)


if __name__ == '__main__':
    unittest.main()
