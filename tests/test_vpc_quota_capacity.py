"""Synthetic regression evidence; no real accounts, credentials or cloud calls."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = (Path(__file__).resolve().parents[1] / "plugins/volcengine-vpc/skills"
          / "volcengine-vpc-quota-capacity/scripts/summarize_vpc_quota.py")
SPEC = importlib.util.spec_from_file_location("vpc_quota_capacity", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
_previous_bytecode = sys.dont_write_bytecode
try:
    sys.dont_write_bytecode = True
    SPEC.loader.exec_module(MODULE)
finally:
    sys.dont_write_bytecode = _previous_bytecode


def response(action, service, result, request_id="synthetic-request"):
    return {"ResponseMetadata": {"Action": action, "Service": service, "Region": "cn-beijing",
                                 "RequestId": request_id}, "Result": result}


def snapshot():
    return {
        "schema_version": 1, "collected_at": "2026-01-01T12:00:00+08:00",
        "region": "cn-beijing", "project": "demo", "provider_code": "vpc",
        "identity": response("GetCallerIdentity", "sts", {"AccountId": "10001"}),
        "vpc_pages": [{"request": {"Region": "cn-beijing", "MaxResults": 100},
                       "response": response("DescribeVpcs", "vpc", {"NextToken": "", "Vpcs": [
                           {"VpcId": "vpc-example-a", "ProjectName": "demo", "AccountId": "10001"},
                           {"VpcId": "vpc-example-b", "ProjectName": "default", "AccountId": "10001"},
                           {"VpcId": "vpc-example-c", "ProjectName": "default", "AccountId": "10002"}]})}],
        "quota_pages": [{"request": {"Region": "cn-beijing", "MaxResults": 100,
                                     "ProviderCode": "vpc", "Dimensions": [
                                         {"Name": "region_code", "Value": "cn-beijing"}]},
                         "response": response("ListProductQuotas", "quota", {
                             "NextToken": "", "TotalCount": 1, "Quotas": [{
                                 "ProviderCode": "vpc", "QuotaCode": "vpc",
                                 "Dimensions": [{"Name": "region_code", "Value": "cn-beijing"}],
                                 "Trn": "trn:quota:cn-beijing:::10001:vpc/vpc",
                                 "TotalQuota": 23, "TotalUsage": {"Value": 2, "Unit": "个"},
                                 "ApplicableType": "continuous", "ApplicableValue": "10,30"}]})}]
    }


def quota(s):
    return s["quota_pages"][0]["response"]["Result"]["Quotas"][0]


class VpcQuotaCapacityTests(unittest.TestCase):
    def test_shared_visibility_does_not_reduce_owned_quota(self):
        s = snapshot()
        original = copy.deepcopy(s)
        result = MODULE.summarize(s, planned_vpcs=1)
        self.assertEqual(result["inventory"]["visible"], 3)
        self.assertEqual(result["inventory"]["known_owned"], 2)
        self.assertEqual(result["project_inventory"]["known_owned"], 1)
        self.assertEqual(result["quota"]["remaining"], 21)
        self.assertEqual(result["plan"]["remaining_after"], 20)
        self.assertTrue(result["reconciliation"]["owned_count_matches_quota_usage"])
        self.assertEqual(s, original)

    def test_foreign_resource_with_same_project_name_is_separate(self):
        s = snapshot()
        s["vpc_pages"][0]["response"]["Result"]["Vpcs"][2]["ProjectName"] = "demo"
        project = MODULE.summarize(s)["project_inventory"]
        self.assertEqual((project["visible"], project["known_owned"], project["other_account_visible"]), (2, 1, 1))

    def test_multiple_pages_preserve_counts(self):
        s = snapshot()
        first = s["vpc_pages"][0]
        second = copy.deepcopy(first)
        second["request"]["NextToken"] = "synthetic-next"
        second["response"]["ResponseMetadata"]["RequestId"] = "synthetic-second"
        second["response"]["Result"]["Vpcs"] = first["response"]["Result"]["Vpcs"][1:]
        first["response"]["Result"]["Vpcs"] = first["response"]["Result"]["Vpcs"][:1]
        first["response"]["Result"]["NextToken"] = "synthetic-next"
        for p in (first, second):
            p["response"]["Result"]["TotalCount"] = 3
        s["vpc_pages"].append(second)
        self.assertEqual(MODULE.summarize(s)["inventory"]["visible"], 3)

    def test_missing_ownership_is_not_guessed(self):
        s = snapshot()
        del s["vpc_pages"][0]["response"]["Result"]["Vpcs"][0]["AccountId"]
        result = MODULE.summarize(s)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["inventory"]["owned_count_exact"])
        self.assertEqual(result["project_inventory"]["unknown_owner"], 1)
        self.assertEqual(result["quota"]["remaining"], 21)

    def test_unreconciled_usage_does_not_become_delay_claim(self):
        s = snapshot()
        quota(s)["TotalUsage"]["Value"] = 4
        result = MODULE.summarize(s)
        self.assertEqual(result["quota"]["remaining"], 19)
        self.assertEqual(result["reconciliation"]["reason_for_any_remaining_difference"], "unknown")
        self.assertTrue(result["warnings"])

    def test_exceeded_quota_remains_negative(self):
        s = snapshot()
        quota(s)["TotalQuota"] = 1
        result = MODULE.summarize(s, planned_vpcs=2)
        self.assertEqual(result["quota"]["remaining"], -1)
        self.assertEqual(result["plan"]["remaining_after"], -3)
        self.assertFalse(result["plan"]["within_vpc_quota"])

    def test_adjustability_is_not_borrowed_from_adjacent_quota(self):
        s = snapshot()
        del quota(s)["ApplicableValue"]
        other = copy.deepcopy(quota(s))
        other.update(QuotaCode="vpc/route_table", ApplicableValue="50,200")
        result = s["quota_pages"][0]["response"]["Result"]
        result["Quotas"].append(other)
        result["TotalCount"] = 2
        self.assertIsNone(MODULE.summarize(s)["quota"]["applicable_value_raw"])

    def test_invalid_evidence_cannot_produce_a_successful_capacity(self):
        for mutation in ("missing_usage", "wrong_unit", "missing_unit", "null_usage", "negative_usage", "bool_usage", "nonfinite",
                         "missing_page", "no_end_token", "duplicate_id", "truncated_total",
                         "project_filter", "wrong_region", "api_error", "ambiguous_quota",
                         "wrong_account", "missing_project", "unknown_dimension"):
            with self.subTest(mutation=mutation):
                s = snapshot()
                result = s["vpc_pages"][0]["response"]["Result"]
                q = quota(s)
                if mutation == "missing_usage": del q["TotalUsage"]
                elif mutation == "wrong_unit": q["TotalUsage"]["Unit"] = "%"
                elif mutation == "missing_unit": del q["TotalUsage"]["Unit"]
                elif mutation == "null_usage": q["TotalUsage"]["Value"] = None
                elif mutation == "negative_usage": q["TotalUsage"]["Value"] = -1
                elif mutation == "bool_usage": q["TotalUsage"]["Value"] = True
                elif mutation == "nonfinite": q["TotalQuota"] = float("nan")
                elif mutation == "missing_page": result["NextToken"] = "more-pages"
                elif mutation == "no_end_token": del result["NextToken"]
                elif mutation == "duplicate_id": result["Vpcs"].append(copy.deepcopy(result["Vpcs"][0]))
                elif mutation == "truncated_total": result["TotalCount"] = 5
                elif mutation == "project_filter": s["vpc_pages"][0]["request"]["ProjectName"] = "demo"
                elif mutation == "wrong_region": s["vpc_pages"][0]["response"]["ResponseMetadata"]["Region"] = "cn-shanghai"
                elif mutation == "api_error": s["vpc_pages"][0]["response"]["ResponseMetadata"]["Error"] = {"Code": "AccessDenied"}
                elif mutation == "ambiguous_quota":
                    s["quota_pages"][0]["response"]["Result"]["Quotas"].append(copy.deepcopy(q))
                    s["quota_pages"][0]["response"]["Result"]["TotalCount"] = 2
                elif mutation == "wrong_account": q["Trn"] = "trn:quota:cn-beijing:::10002:vpc/vpc"
                elif mutation == "missing_project": del result["Vpcs"][0]["ProjectName"]
                elif mutation == "unknown_dimension": q["Dimensions"].append({"Name": "object", "Value": "x"})
                with self.assertRaises(MODULE.EvidenceError): MODULE.summarize(s)

    def test_large_integer_count_does_not_overflow(self):
        s = snapshot()
        quota(s)["TotalQuota"] = 10 ** 400
        self.assertEqual(MODULE.summarize(s)["quota"]["remaining"], 10 ** 400 - 2)

    def test_command_line_runs_offline_and_reports_saved_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot()))
            result = subprocess.run([sys.executable, str(SCRIPT), "--snapshot", str(path)],
                                    capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["evidence_kind"], "saved_snapshot_not_live_query")


if __name__ == "__main__":
    unittest.main()
