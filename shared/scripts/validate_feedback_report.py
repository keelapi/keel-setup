#!/usr/bin/env python3
"""Validate and exactly preview a Keel feedback report. Never transmits."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

from schema_validation import validate as validate_schema

MAX_BYTES = 16_384
TOP_FIELDS = {
    "category", "summary", "intended_task", "expected_behavior", "observed_behavior",
    "desired_outcome", "surface", "blocker", "evidence_level", "coding_agent", "skill_version",
    "keel_release", "decision_classification", "optional_context",
}
OPTIONAL_FIELDS = {"provider", "tool_name", "source_locations", "environment_details"}
PROHIBITED_KEYS = {
    "api_key", "key", "token", "secret", "password", "cookie", "authorization", "headers",
    "environment", "environment_variables", "project_id", "policy_id", "mapping_id", "account_id",
    "organization_id", "permit_body", "request_body", "provider_body", "raw_logs", "logs", "code",
    "diff", "prompt", "model_output", "attachment", "attachments", "hostname", "username",
}
SENSITIVE_TEXT = re.compile(r"(?i)(?:authorization\s*:|bearer\s+\S+|private[_ -]?key|client[_ -]?secret|api[_ -]?key\s*[:=])")
RAW_ENV_TEXT = re.compile(r"(?:^|\s)[A-Z][A-Z0-9_]{2,}=\S+")
TOKEN_TEXT = re.compile(r"(?i)(?:\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|\bAKIA[0-9A-Z]{16}\b|\b(?:sk|ks|ghp|github_pat|xox[abp])[-_][A-Za-z0-9_-]{8,}\b|\b(?:access[_-]?token|token|secret|password|cookie|api[_-]?key)\s*[:=]\s*\S+)")
LOCAL_PATH_TEXT = re.compile(r"(?i)(?:^|[\s('])(?:/(?:Users|home|root|private|tmp|var|opt|etc)/[^\s'\"]+|[A-Z]:\\Users\\[^\s'\"]+)")
RAW_CONTENT_TEXT = re.compile(r"(?im)(?:```|^diff --git\b|^@@\s+-\d|^Traceback \(most recent call last\):|^\[(?:DEBUG|INFO|WARN|WARNING|ERROR|TRACE)\]|\b(?:raw\s+code|source|raw\s+logs?|logs?)\s*:|\b(?:raw\s+)?(?:request|permit|provider)\s+body\s*:|\b(?:system|user|assistant)\s+prompt\s*:|\braw\s+(?:model|provider)\s+output\s*:)")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[1] / "feedback-report.schema.json"


def _walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            yield child_path, key, child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def validate(report: Any, approved_context: set[str], channel: str | None) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    failures = validate_schema(report, schema)
    if not isinstance(report, dict):
        return failures
    if len(json.dumps(report, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        failures.append(f"feedback report exceeds {MAX_BYTES} bytes")

    context = report.get("optional_context")
    if not isinstance(context, dict):
        failures.append("optional_context must be an object")
        context = {}
    failures.extend(f"unknown optional_context field: {key}" for key in sorted(set(context) - OPTIONAL_FIELDS))
    failures.extend(f"missing optional_context field: {key}" for key in sorted(OPTIONAL_FIELDS - set(context)))
    populated = {
        key for key in OPTIONAL_FIELDS
        if context.get(key) not in (None, "", [])
    }
    for key in sorted(populated - approved_context):
        failures.append(f"optional_context.{key} is populated without separate --approve-context {key}")
    for path, key, value in _walk(report):
        if key in PROHIBITED_KEYS:
            failures.append(f"prohibited field: {path}")
        if isinstance(value, str) and SENSITIVE_TEXT.search(value):
            failures.append(f"possible credential or authorization material in: {path}")
        if isinstance(value, str) and RAW_ENV_TEXT.search(value):
            failures.append(f"possible raw environment variable in: {path}")
        if isinstance(value, str) and TOKEN_TEXT.search(value):
            failures.append(f"possible token, cookie, password, or credential assignment in: {path}")
        if isinstance(value, str) and LOCAL_PATH_TEXT.search(value):
            failures.append(f"possible absolute user or system path in: {path}")
        if isinstance(value, str) and RAW_CONTENT_TEXT.search(value):
            failures.append(f"possible raw code, diff, log, traceback, request body, prompt, or output in: {path}")

    locations = context.get("source_locations")
    if isinstance(locations, list):
        for index, location in enumerate(locations):
            if not isinstance(location, str):
                failures.append(f"optional_context.source_locations[{index}] must be a string")
            elif pathlib.PurePath(location).is_absolute() or ".." in pathlib.PurePosixPath(location).parts:
                failures.append(f"optional_context.source_locations[{index}] must be repository-relative")
    else:
        failures.append("optional_context.source_locations must be an array")

    if report.get("category") == "security_concern" and channel != "private_security":
        failures.append("security_concern requires --channel private_security")
    if channel not in {None, "private_security", "feedback_page", "github_issue", "email"}:
        failures.append("unsupported manual handoff channel")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=pathlib.Path)
    parser.add_argument("--approve-context", action="append", default=[], choices=sorted(OPTIONAL_FIELDS))
    parser.add_argument("--channel", choices=["private_security", "feedback_page", "github_issue", "email"])
    parser.add_argument("--preview", action="store_true", help="print the exact validated payload for human review")
    args = parser.parse_args(argv)
    try:
        raw = args.report.read_bytes()
    except OSError:
        print("feedback report could not be read", file=sys.stderr)
        return 1
    if len(raw) > MAX_BYTES:
        print(f"feedback report exceeds {MAX_BYTES} bytes", file=sys.stderr)
        return 1
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("feedback report is not valid UTF-8 JSON", file=sys.stderr)
        return 1
    failures = validate(report, set(args.approve_context), args.channel)
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    if args.preview:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        print("\nValidated preview only. No transmission occurred.")
        print("Pattern checks are bounded and do not prove redaction, human approval, or private routing; inspect this exact payload before manual handoff.")
    else:
        print("valid feedback report; rerun with --preview before manual handoff")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
