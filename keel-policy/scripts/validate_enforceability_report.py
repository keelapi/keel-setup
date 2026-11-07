#!/usr/bin/env python3
"""Offline schema and field-provenance checks; no policy-outcome certification."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

sys.dont_write_bytecode = True

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared" / "scripts"))
from bounded_input import read_bytes
from schema_validation import complexity_failures, validate as validate_schema  # noqa: E402

MANIFEST = ROOT / "tools" / "public_surface.json"
SCHEMA = ROOT / "keel-policy" / "reference" / "enforceability-report.schema.json"
PROVENANCE = ROOT / "keel-policy" / "reference" / "field-provenance.json"
POLICY_SCHEMA = ROOT / "keel-policy" / "reference" / "policy-document.schema.json"
MAX_REPORT_BYTES = 1_000_000
BOUND_TOOL_NAME = "context._keel.action_envelope.connector.tool_name.value"
PROHIBITED_KEYS = {"certified_action_contract_id", "activation", "is_active", "credential", "api_key", "token"}
NON_EFFECTING_ACTIONS = {"preview"}
# Conservative positive availability contract. Other derived fields require a
# future source-backed matrix update; global provenance alone is insufficient.
SURFACE_FIELDS = {
    "managed_execute": {"provider", "model"},
    "managed_mcp": {
        "context._keel.action_envelope.connector.identity.value", BOUND_TOOL_NAME,
    },
    "realtime": set(),
    "voice": set(),
}
ACTION_DEPENDENCIES: dict[str, tuple[str, ...] | None] = {
    "allow": (), "deny": (), "preview": (), "require_human_review": (),
    "constrain_max_output_tokens": (), "constrain_permit_lifetime": (),
    "deny_if_model_not_in": ("model",),
    "deny_if_cost_exceeds": ("estimated_cost_usd_micros", "context._keel.action_envelope.pricing.estimated_cost_usd_micros.state"),
    # These handlers consume runtime capabilities/facts for which this bundle
    # publishes no independently trusted offline evidence contract.
    "deny_if_rate_exceeds": None, "throttle_if_rate_exceeds": None,
    "deny_if_spike_detected": None, "deny_if_projected_monthly_ratio_exceeds": None,
    "require_budget_envelope": None, "constrain_permit_target": None,
    "constrain_work_authority": None,
}


def _action_dependencies(rule: dict[str, Any]) -> tuple[str, ...] | None:
    action = rule.get("action")
    if not isinstance(action, str):
        return None
    params = rule.get("params")
    if action == "deny_if_cost_exceeds" and (not isinstance(params, dict) or params.get("window") != "request"):
        return None  # Window spend is not a published field/capability contract.
    return ACTION_DEPENDENCIES.get(action)


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


def derive_readback(policy: dict[str, Any]) -> list[str]:
    """Canonical readback binds every condition, parameter and requirement."""
    return [f"Rule {index + 1}: {json.dumps(rule, sort_keys=True, ensure_ascii=False)}"
            for index, rule in enumerate(policy.get("rules", []))]


def _safe_outcome(rule: dict[str, Any]) -> str:
    action = rule.get("action")
    if action == "allow":
        return "review" if rule.get("approval_requirement") else "auto_allow"
    if action == "require_human_review":
        return "review"
    if action == "deny" or str(action).startswith("deny_if_"):
        return "deny"
    return "proposal_only"


def validate(
    report: Any,
    manifest: dict[str, Any],
    provenance_artifact: dict[str, Any],
    schema: dict[str, Any],
    policy_schema: dict[str, Any],
) -> list[str]:
    limits = complexity_failures(report, max_nodes=10_000, max_items=1_000, max_text=1_000_000)
    if limits:
        return limits
    failures = validate_schema(report, schema)
    if not isinstance(report, dict):
        return failures
    for key, _ in _walk(report):
        if key in PROHIBITED_KEYS:
            failures.append(f"prohibited authority or certified-contract field: {key}")

    public_fields = set(manifest.get("fields", {}).get("PUBLIC", []))
    if set(ACTION_DEPENDENCIES) != set(manifest.get("actions", {}).get("PUBLIC", [])):
        failures.append("action semantics table does not exactly cover the public action set")
    provenance = provenance_artifact.get("fields", {}) if isinstance(provenance_artifact, dict) else {}
    if provenance_artifact.get("schema_version") != "1.0" or set(provenance) != public_fields:
        failures.append("pinned field-provenance artifact does not exactly cover the published field set")

    entries = report.get("enforceability") if isinstance(report.get("enforceability"), list) else []
    by_rule_field: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if isinstance(field, str):
            reference = item.get("rule_reference")
            if isinstance(reference, str):
                by_rule_field.setdefault((reference, field), []).append((index, item))
        status = item.get("status")
        pinned_provenance = provenance.get(field) if isinstance(field, str) else None
        surface = item.get("surface")
        if not isinstance(surface, str) or surface not in SURFACE_FIELDS:
            failures.append(f"enforceability[{index}] unsupported execution surface")
        elif status == "trusted" and field not in SURFACE_FIELDS[surface]:
            failures.append(f"enforceability[{index}] field availability is not established on {surface}; use unavailable or unresolved")
        if field == BOUND_TOOL_NAME and surface in {"realtime", "voice"} and status not in {"unavailable", "unresolved"}:
            failures.append(f"enforceability[{index}] connector tool name is unavailable on {surface}")
        if isinstance(field, str) and field not in public_fields:
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
    if isinstance(policy, dict) and isinstance(rules, list):
        if report.get("readback") != derive_readback(policy):
            failures.append("readback must exactly match the structurally derived policy rules")
        # Without an active-policy baseline or runtime composition, a draft
        # cannot establish narrowing, widening, or unchanged activation.
        if report.get("activation_effect") != "unresolved":
            failures.append("activation_effect must be unresolved without an authenticated active-policy baseline")
        expected_refs = {f"rules[{index}]" for index in range(len(rules))}
        for entry_index, item in enumerate(entries):
            if isinstance(item, dict) and (not isinstance(item.get("rule_reference"), str)
                                          or item["rule_reference"] not in expected_refs):
                failures.append(f"enforceability[{entry_index}] must reference exactly one existing rules[index]")
    if isinstance(rules, list):
        for rule_index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            fields = _condition_fields(rule.get("if"))
            if rule.get("action") not in NON_EFFECTING_ACTIONS and not fields:
                failures.append(f"policy.rules[{rule_index}] automatic action requires a trusted condition leaf")
            dependencies = _action_dependencies(rule)
            if dependencies is None:
                failures.append(f"policy.rules[{rule_index}] implicit runtime facts/capabilities have no pinned trusted evidence contract")
                dependencies = ()
            fields = sorted(set(fields) | set(dependencies))
            reference = f"rules[{rule_index}]"
            for entry_index, item in enumerate(entries):
                if not isinstance(item, dict) or item.get("rule_reference") != reference:
                    continue
                if item.get("field") not in fields:
                    failures.append(f"enforceability[{entry_index}] fact is not consumed by the referenced rule")
                if item.get("safe_outcome") != _safe_outcome(rule):
                    failures.append(f"enforceability[{entry_index}] safe_outcome disagrees with the referenced action")
            for field in fields:
                matching = by_rule_field.get((reference, field), [])
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
                        if field in dependencies and item.get("surface") != "managed_execute":
                            failures.append(f"policy.rules[{rule_index}] implicit field {field!r} requires the managed_execute surface")
    return failures[:100]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        report = json.loads(read_bytes(args.report, MAX_REPORT_BYTES))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        policy_schema = json.loads(POLICY_SCHEMA.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 1
    failures = validate(report, manifest, provenance, schema, policy_schema)
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("schema and field-provenance checks passed; outcomes, readback, rule order, activation effect, and runtime enforcement remain unverified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
