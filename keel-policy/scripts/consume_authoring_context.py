#!/usr/bin/env python3
"""Offline authoring-context intake and operation intersection; never drafts or activates.

Uses the unchanged published v2 transport validator as the single schema/vocabulary
source. Importing it performs no I/O. No credential or network path is called here.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "keel_published_authoring_context", ROOT / "keel-setup/scripts/authoring_context.py"
)
_transport = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_transport)
ContextError = _transport.AuthoringContextError


def _closed_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContextError("authoring context contains duplicate JSON keys")
        result[key] = value
    return result


def parse_context(raw: bytes) -> dict:
    """Accept only the safe JSON block, never a transcript or partial context."""
    if len(raw) > _transport.MAX_RESPONSE_BYTES:
        raise ContextError("authoring context exceeds 64 KiB")
    try:
        json.loads(raw, object_pairs_hook=_closed_pairs)
        context = _transport.parse_authoring_context(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContextError("malformed authoring context; no candidates produced") from exc
    if context["schema_version"] != 2:
        raise ContextError("this consumer requires authoring-context schema version 2")
    for model in context["models"]:
        if model["operations"] != sorted(model["operations"]):
            raise ContextError("model operations must be sorted and unique")
    return context


def prepare(raw: bytes, operation: str, evidence: str, current: tuple[str, str] | None = None,
            selected: list[tuple[str, str]] | None = None,
            accept_blocking_default: bool = False) -> dict:
    """Evidence is source-inspected by the agent, not certified by this function.

    Selection is supplied only after a human decision. Onboarding/test models and
    prior pricing knowledge have no input channel. Catalog ordering is not a ranking.
    """
    context = parse_context(raw)
    if operation not in _transport._OPERATIONS:
        raise ContextError("application operation is unsupported; inspect the call contract")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000 or not evidence.isprintable():
        raise ContextError("a bounded source location and call-contract explanation are required")
    compatible = [m for m in context["models"]
                  if operation in m["operations"] and m["routable_in_policies"]]
    # Only the existing literal active state is a default presentation candidate.
    # Preview, deprecated, sunset and unfamiliar states retain their exact labels
    # in details; none is reinterpreted as active or as a customer preference.
    candidates = [m for m in compatible if m["lifecycle_status"] == "active"
                  and m["pricing_quality"] in {"authoritative", "approximate"}
                  and m["prompt_per_1k_usd"] is not None
                  and m["completion_per_1k_usd"] is not None]
    details = [m for m in compatible if m not in candidates]
    warnings = [
        "These are Keel-recorded token rates, not a recommendation or predicted request cost.",
        "Authoritative means provider-published source pricing, not guaranteed fresh; approximate means approximate.",
        "Keel does not establish when it last checked these prices (pricing_asof is null).",
        "Zero recorded token rates do not establish that a model is free.",
        "Placeholder is not a comparable token price; unknown has no usable recorded rate. Neither is price-ranked.",
    ]
    if context["truncated"]:
        warnings.append("This catalog is truncated; these options cannot establish a global cheapest model.")
    level = context["authoring_level"]
    decision = '“Cheap” needs one decision from you: where should I draw the line? Choose specific models from these recorded rates.'
    status = "needs_model_selection"
    selected_rows = []
    if selected is not None:
        if not selected or len(set(selected)) != len(selected):
            raise ContextError("choose a nonempty unique model set")
        by_id = {(m["provider"], m["model_id"]): m for m in candidates}
        if any(identity not in by_id for identity in selected):
            raise ContextError("selection is outside the active, compatible, meaningfully priced options; resolve explicitly before drafting")
        selected_rows = [by_id[identity] for identity in selected]
        status = "selection_recorded"
        decision = "Validate scope, canonical policy schema, selected authoring profile and enforceability before human review."
        if current is None:
            status = "needs_default_evidence"
            decision = "The current application model is unresolved; establish its source configuration before drafting."
        elif current not in selected:
            warnings.append("Turning this on would block the model your app currently uses.")
            if not accept_blocking_default:
                status = "needs_default_decision"
                decision = "Change the app default with your explicit approval, widen the allowed set, or explicitly accept blocking the current default?"
    if not candidates:
        status = "no_priced_candidates"
        decision = "No active compatible models have comparable recorded token rates in this block. Specific-model restrictions remain possible after resolving the missing facts; no price-based set can be inferred."
    if level == "template":
        status = "template_only"
        decision = "Custom drafting is unavailable under your current authoring entitlement. Choose a supported starter or change the entitlement before custom drafting."
        selected_rows = []
    return {
        "authoring_level": level,
        "application_operation": operation,
        "source_inspected_evidence": evidence,
        "current_model": list(current) if current else None,
        "pricing_asof": context["pricing_asof"],
        "model_count": context["model_count"],
        "truncated": context["truncated"],
        "candidates": candidates,
        "details": details,
        "selected_models": selected_rows,
        "status": status,
        "human_decision": decision,
        "warnings": warnings,
        "handoff": "Human-readable review → dashboard coding-agent import → Test policy → Save inactive draft → explicit human activation.",
    }


def present(report: dict, view: str = "summary", page: int = 1) -> dict:
    """Bound display only. Eligibility, human selections and original rates are unchanged."""
    if view not in {"summary", "more", "details"} or page < 1:
        raise ContextError("invalid presentation page")
    candidates, details = report["candidates"], report["details"]
    identity_key = lambda row: (row["provider"], row["model_id"])
    # Separate axes: no assumed input/output mix, quality score, provider preference
    # or combined dollar estimate. Identity only breaks exact numeric-rate ties.
    examples = {}
    for field in ("prompt_per_1k_usd", "completion_per_1k_usd"):
        ordered = sorted(candidates, key=lambda row: (Decimal(row[field]), *identity_key(row)))
        for row in ordered[:3]:
            examples[identity_key(row)] = row
    result = dict(report)
    result["candidates"] = []
    result["details"] = []
    result["candidate_count"] = len(candidates)
    result["detail_count"] = len(details)
    result["current_model_context"] = next(
        (row for row in candidates + details if list(identity_key(row)) == report["current_model"]), None)
    if view == "summary":
        result["candidates"] = sorted(examples.values(), key=identity_key)
    else:
        rows = sorted(candidates if view == "more" else details, key=identity_key)
        if page > max(1, (len(rows) + 9) // 10):
            raise ContextError("presentation page is out of range")
        result["candidates" if view == "more" else "details"] = rows[(page - 1) * 10:page * 10]
    result["presentation"] = {
        "view": view, "page": page,
        "rule": "Price examples: three lowest recorded input rates and three lowest recorded output rates in this block, combined without duplicates. Exact rate ties use provider/model ID order. This is not a quality comparison or a chosen allowed set.",
        "show_more": f"Show more: all {len(candidates)} active priced options, ten per page.",
        "technical_details": f"Technical details: {len(details)} other compatible entries, with their original lifecycle and pricing labels.",
    }
    return result


def identity(value: str) -> tuple[str, str]:
    provider, separator, model = value.partition(":")
    if not separator or not provider or not model:
        raise argparse.ArgumentTypeError("use provider:model_id from the validated context")
    return provider, model


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=pathlib.Path, required=True, help="safe JSON block only; never a Runtime key")
    parser.add_argument("--operation", required=True)
    parser.add_argument("--evidence", required=True, help="source path/line and inspected call semantics")
    parser.add_argument("--current-model", type=identity)
    parser.add_argument("--select", type=identity, action="append", help="only models explicitly selected by the human")
    parser.add_argument("--accept-blocking-default", action="store_true", help="only after the human explicitly accepts the disclosed block")
    parser.add_argument("--presentation", choices=("summary", "more", "details"), default="summary")
    parser.add_argument("--page", type=int, default=1, help="ten entries per show-more/details page")
    args = parser.parse_args(argv)
    try:
        with args.context.open("rb") as stream:
            raw = stream.read(_transport.MAX_RESPONSE_BYTES + 1)
        result = present(prepare(raw, args.operation, args.evidence, args.current_model, args.select, args.accept_blocking_default), args.presentation, args.page)
    except ContextError as exc:
        print(f"Stop before drafting: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Cannot read the safe context file; stop before drafting.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
