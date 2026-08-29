#!/usr/bin/env python3
"""Create a bounded, source-only execution-surface inventory."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared" / "scripts"))
from schema_validation import validate as validate_schema  # noqa: E402

COVERAGE_SCHEMA = ROOT / "keel-setup" / "reference" / "coverage.schema.json"

MAX_FILE_BYTES = 1_000_000
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__"}
TEXT_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".json", ".yaml", ".yml", ".toml"}
PATTERNS = (
    ("mcp", re.compile(r"\b(?:FastMCP|McpServer|registerTool|server\.tool|tools/list|tools/call)\b"), "MCP registration or invocation"),
    ("model", re.compile(r"\b(?:OpenAI|Anthropic|BedrockRuntime|generateContent|chat\.completions|responses\.create)\b"), "model-provider call"),
    ("payment", re.compile(r"\b(?:refund|charge|payment|issue_credit|Stripe|Adyen)\b", re.I), "payment or credit signal"),
    ("identity", re.compile(r"\b(?:disable_user|delete_user|revoke_role|grant_role|Okta|Auth0)\b", re.I), "identity or permission signal"),
    ("database", re.compile(r"\b(?:DELETE\s+FROM|drop_table|delete_rows|\.delete\(|\.remove\()", re.I), "database deletion or mutation"),
    ("background", re.compile(r"\b(?:Celery|Sidekiq|BackgroundJob|create_task|enqueue|queue\.add)\b"), "background or asynchronous execution"),
    ("browser", re.compile(r"\b(?:playwright|puppeteer|selenium)\b", re.I), "browser automation"),
    ("code_execution", re.compile(r"\b(?:subprocess\.|child_process\.|os\.system|exec\(|eval\()"), "code or process execution"),
    ("direct_api", re.compile(r"\b(?:requests\.(?:post|put|patch|delete)|fetch\(|axios\.|httpx\.(?:post|put|patch|delete))"), "direct network mutation candidate"),
)


def revision(root: pathlib.Path) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def iter_source_files(root: pathlib.Path):
    for path in sorted(root.rglob("*")):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in relative.parts) or path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path, relative


def inspect(root: pathlib.Path) -> dict[str, Any]:
    paths: list[dict[str, Any]] = []
    for path, relative in iter_source_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, 1):
            for surface, pattern, signal in PATTERNS:
                if not pattern.search(line):
                    continue
                paths.append(
                    {
                        "path": relative.as_posix(),
                        "line": line_number,
                        "surface": surface,
                        "signal": signal,
                        "status": "unresolved",
                        "evidence_level": "source_inspected",
                        "confidence": "medium",
                        "uncertainties": ["source signal is not trusted runtime semantics"],
                    }
                )
    return {
        "schema_version": "1.0",
        "report_basis": "source_inventory",
        "application_revision": revision(root),
        "paths": paths,
        "does_not_establish": [
            "deployment",
            "runtime_frequency",
            "trusted_semantics",
            "downstream_effect",
            "bypass_absence",
            "whole_application_protection",
            "independent_verification",
        ],
    }


def validate_coverage(report: Any) -> list[str]:
    schema = json.loads(COVERAGE_SCHEMA.read_text(encoding="utf-8"))
    failures = validate_schema(report, schema)
    if not isinstance(report, dict):
        return failures
    entries = report.get("paths")
    if not isinstance(entries, list):
        return failures
    identities: set[tuple[Any, ...]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        evidence_level = entry.get("evidence_level")
        if status in {"protected", "governed_routed"} and evidence_level != "runtime_observed":
            failures.append(f"paths[{index}] {status} requires per-entry runtime_observed evidence")
        if status == "intentionally_unprotected" and evidence_level != "human_asserted":
            failures.append(f"paths[{index}] intentionally_unprotected requires per-entry human_asserted evidence")
        candidate = entry.get("path")
        if not isinstance(candidate, str) or pathlib.PurePath(candidate).is_absolute() or ".." in pathlib.PurePosixPath(candidate).parts:
            failures.append(f"paths[{index}].path must be repository-relative")
        identity = (entry.get("path"), entry.get("line"), entry.get("surface"), entry.get("signal"))
        if identity in identities:
            failures.append(f"paths[{index}] duplicates an earlier path/line/surface/signal identity")
        identities.add(identity)
    required_limits = {"deployment", "runtime_frequency", "trusted_semantics", "downstream_effect", "bypass_absence", "whole_application_protection", "independent_verification"}
    if not required_limits.issubset(set(report.get("does_not_establish", []))):
        failures.append("does_not_establish is missing required evidence boundaries")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--validate-coverage", type=pathlib.Path)
    args = parser.parse_args(argv)
    if args.validate_coverage:
        try:
            report = json.loads(args.validate_coverage.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"invalid coverage report: {exc}", file=sys.stderr)
            return 1
        failures = validate_coverage(report)
        if failures:
            for failure in failures:
                print(failure, file=sys.stderr)
            return 1
        print("valid coverage report")
        return 0
    root = args.root.resolve()
    print(json.dumps(inspect(root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
