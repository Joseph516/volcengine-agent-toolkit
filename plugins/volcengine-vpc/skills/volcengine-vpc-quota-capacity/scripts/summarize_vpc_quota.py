#!/usr/bin/env python3
"""Reconcile a saved VPC/quota snapshot. Offline; no credentials or cloud calls."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from typing import Any

import importlib.util
_spec = importlib.util.spec_from_file_location('vpc_evidence', Path(__file__).with_name('evidence.py'))
_proof = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_proof)


class EvidenceError(ValueError):
    """The snapshot cannot support an exact capacity calculation."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def count(value: Any, label: str) -> int:
    require(type(value) in (int, float), f"{label}: missing or nonnumeric count")
    require((type(value) is int or math.isfinite(value)) and value >= 0 and int(value) == value,
            f"{label}: expected a finite nonnegative integer count")
    return int(value)


def response(value: Any, action: str, service: str) -> tuple[dict, dict]:
    require(isinstance(value, dict), f"{action}: response must be an object")
    metadata = value.get("ResponseMetadata")
    require(isinstance(metadata, dict), f"{action}: missing ResponseMetadata")
    require(not metadata.get("Error"), f"{action}: API error; cannot treat as empty")
    require(metadata.get("Action") == action and metadata.get("Service") == service,
            f"{action}: mismatched action or service")
    require(isinstance(metadata.get("RequestId"), str) and bool(metadata["RequestId"]),
            f"{action}: missing RequestId")
    result = value.get("Result")
    require(isinstance(result, dict), f"{action}: missing Result")
    return result, metadata


def collect(pages: Any, action: str, service: str, field: str, region: str,
            provider: str | None = None) -> tuple[list[dict], list[str]]:
    require(isinstance(pages, list) and bool(pages), f"{action}: pages are required")
    items, request_ids, totals = [], [], []
    next_token = ""
    seen_tokens = set()
    first_filters = None
    for index, page in enumerate(pages):
        require(isinstance(page, dict), f"{action}: invalid page envelope")
        request = page.get("request")
        require(isinstance(request, dict), f"{action}: missing actual request parameters")
        allowed = {"Region", "MaxResults", "NextToken"}
        if provider is not None:
            allowed |= {"ProviderCode", "Dimensions", "QuotaCode"}
        require(set(request) <= allowed,
                f"{action}: unsupported filter or pagination mode; full scope required")
        require(request.get("Region") == region, f"{action}: request region mismatch")
        require(count(request.get("MaxResults"), "MaxResults") > 0,
                f"{action}: MaxResults must be positive")
        require(request.get("NextToken", "") == next_token,
                f"{action}: missing, reordered or mismatched page token")
        filters = {k: v for k, v in request.items() if k not in {"NextToken", "MaxResults"}}
        if index == 0:
            first_filters = filters
        require(filters == first_filters, f"{action}: filters changed between pages")
        if provider is not None:
            require(request.get("ProviderCode") == provider,
                    f"{action}: product code mismatch")
            require(request.get("Dimensions") == [{"Name": "region_code", "Value": region}],
                    f"{action}: only the selected account-region dimension is supported")
            require(request.get("QuotaCode", "vpc") == "vpc",
                    f"{action}: wrong quota filter")
        result, metadata = response(page.get("response"), action, service)
        if service == "vpc":
            require(metadata.get("Region") == region, f"{action}: response region mismatch")
        require(metadata["RequestId"] not in request_ids, f"{action}: duplicate page response")
        request_ids.append(metadata["RequestId"])
        batch = result.get(field)
        require(isinstance(batch, list) and all(isinstance(x, dict) for x in batch),
                f"{action}: missing or invalid {field} array")
        items.extend(batch)
        if "TotalCount" in result:
            totals.append(count(result["TotalCount"], "TotalCount"))
        require("NextToken" in result and isinstance(result["NextToken"], str),
                f"{action}: missing token pagination completion evidence")
        next_token = result["NextToken"]
        if index < len(pages) - 1:
            require(bool(next_token), f"{action}: extra page after end of list")
        if next_token:
            require(next_token not in seen_tokens, f"{action}: repeated page token")
            seen_tokens.add(next_token)
    require(not next_token, f"{action}: more pages remain")
    require(all(total == len(items) for total in totals),
            f"{action}: TotalCount changed or does not match collected rows")
    return items, request_ids


def summarize(snapshot: dict, planned_vpcs: int = 0) -> dict:
    require(isinstance(snapshot, dict) and snapshot.get("schema_version") == 1,
            "expected snapshot schema_version 1")
    region, project = snapshot.get("region"), snapshot.get("project")
    require(isinstance(region, str) and bool(region.strip()), "region is required")
    require(project is None or (isinstance(project, str) and bool(project.strip())),
            "project must be a nonempty name or null")
    stamp = snapshot.get("collected_at")
    require(isinstance(stamp, str), "collected_at is required")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        raise EvidenceError("collected_at must be an ISO timestamp") from None
    require(parsed.utcoffset() is not None, "collected_at must include a timezone")
    planned_vpcs = count(planned_vpcs, "planned_vpcs")
    identity, identity_metadata = response(snapshot.get("identity"), "GetCallerIdentity", "sts")
    account = identity.get("AccountId")
    require(type(account) in (int, str) and str(account).isdigit(), "identity AccountId is missing")
    account = str(account)
    provider = snapshot.get("provider_code")
    require(isinstance(provider, str) and provider.lower() == "vpc", "expected VPC product code")
    vpcs, vpc_requests = collect(snapshot.get("vpc_pages"), "DescribeVpcs", "vpc", "Vpcs", region)
    quotas, quota_requests = collect(snapshot.get("quota_pages"), "ListProductQuotas", "quota",
                                      "Quotas", region, provider)
    ids = [v.get("VpcId") for v in vpcs]
    require(all(isinstance(i, str) and bool(i) for i in ids), "VpcId is missing")
    require(len(set(ids)) == len(ids), "duplicate VpcId; pagination or snapshot is inconsistent")
    require(all(isinstance(v.get("ProjectName"), str) and v["ProjectName"] for v in vpcs),
            "ProjectName is missing; cannot claim exact project counts")
    candidates = []
    for quota in quotas:
        if quota.get("ProviderCode") != provider or quota.get("QuotaCode") != "vpc":
            continue
        dimensions = quota.get("Dimensions")
        require(isinstance(dimensions, list) and all(isinstance(d, dict) for d in dimensions),
                "VPC quota dimensions missing")
        if [(d.get("Name"), d.get("Value")) for d in dimensions] == [("region_code", region)]:
            candidates.append(quota)
    require(len(candidates) == 1, "expected exactly one VPC quota at the requested dimensions")
    quota = candidates[0]
    trn = quota.get("Trn", "").split(":") if isinstance(quota.get("Trn"), str) else []
    require(len(trn) == 7 and trn[:3] == ["trn", "quota", region]
            and trn[5] == account and trn[6] == f"{provider}/vpc",
            "VPC quota TRN does not prove the selected account, region and quota")
    total = count(quota.get("TotalQuota"), "TotalQuota")
    usage = quota.get("TotalUsage")
    require(isinstance(usage, dict), "TotalUsage is missing")
    unit = usage.get("Unit")
    require(unit in ("个", "count", "Count"), "TotalUsage.Unit: expected a recognized resource-count unit")
    used = count(usage.get("Value"), "TotalUsage.Value")

    def owner(item: dict) -> str:
        value = item.get("AccountId")
        return str(value) if type(value) in (str, int) and str(value).isdigit() else ""

    owned = [v for v in vpcs if owner(v) == account]
    foreign = [v for v in vpcs if owner(v) and owner(v) != account]
    unknown = [v for v in vpcs if not owner(v)]
    project_rows = [v for v in vpcs if v["ProjectName"] == project] if project else []
    warnings = []
    if foreign:
        warnings.append("Other-account VPCs are visible; do not treat visible count as owned quota usage.")
    if unknown:
        warnings.append("Some ownership fields are missing; exact owned count is unknown.")
    if len(owned) != used or unknown:
        warnings.append("Inventory and quota usage are not reconciled; do not infer eventual-consistency delay.")
    remaining = total - used
    return {
        "status": "partial" if unknown else "ok",
        "scope": {"account_id": account, "region": region, "project": project},
        "collected_at": stamp,
        "evidence_kind": "saved_snapshot_not_live_query",
        "inventory": {"visible": len(vpcs), "known_owned": len(owned),
                      "other_account_visible": len(foreign), "unknown_owner": len(unknown),
                      "owned_count_exact": not bool(unknown)},
        "project_inventory": None if project is None else {
            "visible": len(project_rows),
            "known_owned": sum(owner(v) == account for v in project_rows),
            "other_account_visible": sum(bool(owner(v)) and owner(v) != account for v in project_rows),
            "unknown_owner": sum(not owner(v) for v in project_rows),
            "vpc_ids": [v["VpcId"] for v in project_rows]},
        "quota": {"provider_code": provider, "quota_code": "vpc", "dimensions": quota["Dimensions"],
                  "total": total, "used": used, "remaining": remaining, "unit": unit,
                  "usage_source": "ListProductQuotas.TotalUsage.Value",
                  "applicable_type": quota.get("ApplicableType"),
                  "applicable_value_raw": quota.get("ApplicableValue")},
        "plan": {"additional_vpcs": planned_vpcs, "remaining_after": remaining - planned_vpcs,
                 "within_vpc_quota": remaining >= planned_vpcs,
                 "boundary": "Quota arithmetic only; not a resource creation or availability guarantee."},
        "reconciliation": {"owned_count_matches_quota_usage": None if unknown else len(owned) == used,
                           "reason_for_any_remaining_difference": "unknown" if unknown or len(owned) != used else None},
        "request_ids": {"identity": identity_metadata["RequestId"], "vpcs": vpc_requests, "quotas": quota_requests},
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--planned-vpcs", type=int, default=0)
    parser.add_argument("--answer-claims", type=Path, help="Structured final answer claims to compare")
    args = parser.parse_args()
    try:
        result = summarize(json.loads(args.snapshot.read_text(encoding="utf-8")), args.planned_vpcs)
        if args.answer_claims:
            result["answer_check"] = _proof.verify_claims(result, json.loads(args.answer_claims.read_text()))
    except (EvidenceError, ValueError, OSError, json.JSONDecodeError) as exc:
        # Do not echo raw API payloads or local file contents in an error.
        message = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
        print(json.dumps({"status": "error", "error": message}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("answer_check", {}).get("status") == "mismatch":
        return 2
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
