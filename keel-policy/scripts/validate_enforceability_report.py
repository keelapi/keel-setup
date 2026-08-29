#!/usr/bin/env python3
"""Offline validator for Keel policy enforceability reports."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared" / "scripts"))
from schema_validation import validate as validate_schema  # noqa: E402

MANIFEST = ROOT / "tools" / "public_surface.json"
SCHEMA = ROOT / "keel-policy" / "reference" / "enforceability-report.schema.json"
PROVENANCE = ROOT / "keel-policy" / "reference" / "field-provenance.json"
POLICY_SCHEMA = ROOT / "keel-policy" / "reference" / "policy-document.schema.json"
BOUND_TOOL_NAME = "context._keel.action_envelope.connector.tool_name.value"
PROHIBITED_KEYS = {"certified_action_contract_id", "activation", "is_active", "credential", "api_key", "token"}
NON_EFFECTING_ACTIONS = {"preview"}


def _walk(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _condition_fields(condition: Any) -> list[str]:
    fields: list[str] = []
    if isinstance(condition, dict):
        if isinstance(condition.get("field"), str):
            fields.append(condition["field"])
        for key in ("all", "any"):
            if isinstance(condition.get(key), list):
                for child in condition[key]:
                    fields.extend(_condition_fields(child))
        if "not" in condition:
            fields.extend(_condition_fields(condition["not"]))
    return fields


def validate(
    report: Any,
    manifest: dict[str, Any],
    provenance_artifact: dict[str, Any],
    schema: dict[str, Any],
    policy_schema: dict[str, Any],
) -> list[str]:
    failures = validate_schema(report, schema)
    if not isinstance(report, dict):
        return failures
    for key, _ in _walk(report):
        if key in PROHIBITED_KEYS:
            failures.append(f"prohibited authority or certified-contract field: {key}")

    public_fields = set(manifest.get("fields", {}).get("PUBLIC", []))
    pending_fields = set(manifest.get("fields", {}).get("PENDING_action_envelope", [])) | set(manifest.get("fields", {}).get("PENDING_other", []))
    provenance = provenance_artifact.get("fields", {}) if isinstance(provenance_artifact, dict) else {}
    if provenance_artifact.get("schema_version") != "1.0" or set(provenance) != public_fields:
        failures.append("pinned field-provenance artifact does not exactly cover the published field set")

    entries = report.get("enforceability") if isinstance(report.get("enforceability"), list) else []
    by_field: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if isinstance(field, str):
            by_field.setdefault(field, []).append((index, item))
        status = item.get("status")
        pinned_provenance = provenance.get(field) if isinstance(field, str) else None
        if field in pending_fields or (isinstance(field, str) and field not in public_fields):
            if status != "unresolved":
                failures.append(f"enforceability[{index}] unpublished field must be unresolved")
        elif isinstance(field, str) and item.get("provenance") != pinned_provenance:
            failures.append(f"enforceability[{index}] provenance disagrees with the pinned field artifact")
        if status == "trusted" and pinned_provenance != "keel_derived":
            failures.append(f"enforceability[{index}] {pinned_provenance or 'unknown'} field cannot be trusted")
        if item.get("safe_outcome") in {"auto_allow", "deny"} and status != "trusted":
            failures.append(f"enforceability[{index}] automatic outcome requires an independently trusted fact")
        if field == "action.name":
            if status != "untrusted":
                failures.append(f"enforceability[{index}] action.name must be untrusted")
            if item.get("replacement_field") != BOUND_TOOL_NAME:
                failures.append(f"enforceability[{index}] action.name must name the server-derived MCP replacement")
        if isinstance(field, str) and "governance_action_id" in field:
            if status == "trusted" or item.get("safe_outcome") == "auto_allow":
                failures.append(f"enforceability[{index}] governance action is interpretation-only and cannot supply trusted automatic facts")

    policy = report.get("policy")
    if policy is not None:
        failures.extend(validate_schema(policy, policy_schema, "$.policy"))
    rules = policy.get("rules") if isinstance(policy, dict) else []
    if isinstance(rules, list):
        for rule_index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            fields = _condition_fields(rule.get("if"))
            for field in fields:
                matching = by_field.get(field, [])
                if not matching:
                    failures.append(f"policy.rules[{rule_index}] field {field!r} has no enforceability entry")
                    continue
                if len(matching) > 1:
                    failures.append(f"policy.rules[{rule_index}] field {field!r} has ambiguous duplicate enforceability entries")
                if rule.get("action") not in NON_EFFECTING_ACTIONS:
                    for entry_index, item in matching:
                        if item.get("status") != "trusted" or provenance.get(field) != "keel_derived":
                            failures.append(
                                f"policy.rules[{rule_index}] automatic action uses field {field!r} without independently trusted enforceability[{entry_index}]"
                            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        policy_schema = json.loads(POLICY_SCHEMA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 1
    failures = validate(report, manifest, provenance, schema, policy_schema)
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("valid enforceability report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
