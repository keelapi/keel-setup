#!/usr/bin/env python3
"""Create bounded, source-only execution-surface discovery reports.

The default mode produces the existing exhaustive coverage inventory. ``--fast``
performs a deliberately shallow first-run search for one obvious model request
site. Its result is a routing aid for local preparation, never a coverage report.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared" / "scripts"))
from schema_validation import validate as validate_schema  # noqa: E402

COVERAGE_SCHEMA = ROOT / "keel-setup" / "reference" / "coverage.schema.json"

MAX_FILE_BYTES = 1_000_000
FAST_MAX_FILES = 400
FAST_MAX_CANDIDATES = 20
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".worktrees", ".tox", ".mypy_cache", ".pytest_cache",
    "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".vue", ".svelte", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs",
    ".json", ".yaml", ".yml", ".toml",
}
DIRECT_API_PATTERN = re.compile(
    r"\b(?:requests\.(?:post|put|patch|delete)|fetch\(|axios\.|"
    r"httpx\.(?:post|put|patch|delete)|http\.(?:Post|NewRequest)|"
    r"HttpRequest\.newBuilder|Net::HTTP)"
)
PATTERNS = (
    ("mcp", re.compile(r"\b(?:FastMCP|McpServer|registerTool|server\.tool|tools/list|tools/call)\b"), "MCP registration or invocation"),
    ("model", re.compile(r"\b(?:OpenAI|Anthropic|BedrockRuntime|generateContent|chat\.completions|responses\.create)\b"), "model-provider call"),
    ("payment", re.compile(r"\b(?:refund|charge|payment|issue_credit|Stripe|Adyen)\b", re.I), "payment or credit signal"),
    ("identity", re.compile(r"\b(?:disable_user|delete_user|revoke_role|grant_role|Okta|Auth0)\b", re.I), "identity or permission signal"),
    ("database", re.compile(r"\b(?:DELETE\s+FROM|drop_table|delete_rows|\.delete\(|\.remove\()", re.I), "database deletion or mutation"),
    ("background", re.compile(r"\b(?:Celery|Sidekiq|BackgroundJob|create_task|enqueue|queue\.add)\b"), "background or asynchronous execution"),
    ("browser", re.compile(r"\b(?:playwright|puppeteer|selenium)\b", re.I), "browser automation"),
    ("code_execution", re.compile(r"\b(?:subprocess\.|child_process\.|os\.system|exec\(|eval\()"), "code or process execution"),
    ("direct_api", DIRECT_API_PATTERN, "direct network mutation candidate"),
)

FAST_SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte"}
NON_SELECTABLE_DIRS = {
    "example", "examples", "demo", "demos", "samples", "docs", "scripts",
    "benchmarks", "notebooks",
}
FAST_DOES_NOT_ESTABLISH = [
    "exhaustive_discovery",
    "deployment",
    "runtime_behavior",
    "bypass_absence",
    "whole_application_protection",
]
JS_REQUEST_PATTERNS = (
    ("openai", re.compile(r"\.(?:responses\.create|chat\.completions\.create)\s*\("), "sdk_request"),
    ("anthropic", re.compile(r"\.messages\.create\s*\("), "sdk_request"),
    ("google", re.compile(r"\.generateContent\s*\("), "sdk_request"),
    ("aws-bedrock", re.compile(r"\.invokeModel\s*\("), "sdk_request"),
)
PROVIDER_HOST_PATTERNS = {
    "openai": re.compile(r"(?i)\bapi\.openai\.com\b"),
    "anthropic": re.compile(r"(?i)\bapi\.anthropic\.com\b"),
    "google": re.compile(r"(?i)\b(?:generativelanguage|aiplatform)\.googleapis\.com\b"),
    "aws-bedrock": re.compile(r"(?i)\bbedrock-runtime\.[a-z0-9-]+\.amazonaws\.com\b"),
}
PROVIDER_IMPORT_PATTERNS = {
    "openai": re.compile(r"(?i)(?:\bfrom\s+openai\b|\bimport\s+openai\b|['\"]openai['\"]|go-openai)"),
    "anthropic": re.compile(r"(?i)(?:\bfrom\s+anthropic\b|\bimport\s+anthropic\b|@anthropic-ai/sdk|anthropic-sdk)"),
    "google": re.compile(r"(?i)(?:google\.generativeai|@google/(?:generative-ai|genai)|google-genai)"),
    "aws-bedrock": re.compile(r"(?i)(?:BedrockRuntime|client-bedrock-runtime|bedrockruntime)"),
}
OFFICIAL_PROVIDER_HOSTS = {
    "openai": ("api.openai.com",),
    "anthropic": ("api.anthropic.com",),
    "google": ("generativelanguage.googleapis.com", "aiplatform.googleapis.com"),
    "aws-bedrock": ("amazonaws.com",),
}
SENSITIVE_PATH_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|token|credential)\s*[:=][^/]+|"
    r"(?:ks_|sk-|sk_|pk_|rk_|ghp_|gho_|xox[baprs]-)[A-Za-z0-9_-]{8,}"
)


def _targets_official_provider(literal: str, provider: str) -> bool:
    match = re.match(r"(?i)^https://([^/?#:]+)(?::[0-9]+)?(?:[/?#]|$)", literal.strip())
    if match is None:
        return False
    hostname = match.group(1).lower().rstrip(".")
    if provider == "aws-bedrock":
        return re.fullmatch(r"bedrock-runtime\.[a-z0-9-]+\.amazonaws\.com", hostname) is not None
    return hostname in OFFICIAL_PROVIDER_HOSTS.get(provider, ())


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


def iter_fast_source_files(root: pathlib.Path):
    """Yield shallow-search candidates without materializing the whole tree."""

    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in SKIP_DIRS and not name.startswith(".")
        )
        current_path = pathlib.Path(current)
        for filename in sorted(filenames):
            path = current_path / filename
            if path.is_symlink() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                relative = path.relative_to(root)
            except (OSError, ValueError):
                continue
            yield path, relative


def _is_test_path(relative: pathlib.PurePath) -> bool:
    parts = {part.lower() for part in relative.parts}
    name = relative.name.lower()
    return (
        bool(parts & {"test", "tests", "__tests__", "spec", "specs"})
        or name.startswith(("test_", "test-", "spec."))
        or "_test." in name
        or ".test." in name
        or ".spec." in name
    )


def _selection_exclusion(relative: pathlib.PurePath) -> str | None:
    if _is_test_path(relative):
        return "test_surface"
    if {part.lower() for part in relative.parts} & NON_SELECTABLE_DIRS:
        return "non_production_surface"
    return None


def _attribute_name(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _python_custom_base_url_reasons(tree: ast.AST, provider: str) -> list[str]:
    constructors = {
        "openai": ("OpenAI", "AsyncOpenAI"),
        "anthropic": ("Anthropic", "AsyncAnthropic"),
    }.get(provider, ())
    if not constructors:
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _attribute_name(node.func).endswith(constructors):
            continue
        for keyword in node.keywords:
            if keyword.arg != "base_url":
                continue
            if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                continue
            literal = _literal_string(keyword.value)
            if literal and _targets_official_provider(literal, provider):
                continue
            return ["provider client uses a non-default or unresolved base_url"]
    return []


def _python_candidates(path: pathlib.Path, relative: pathlib.PurePath) -> list[dict[str, Any]]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative.as_posix())
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0].lower() for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0].lower())

    candidates: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = _attribute_name(node.func)
        provider: str | None = None
        if called.endswith((".chat.completions.create", ".responses.create")) and "openai" in imported:
            provider = "openai"
        elif called.endswith(".messages.create") and "anthropic" in imported:
            provider = "anthropic"
        elif called.endswith((".generate_content", ".generate_content_async")) and (
            "google" in imported or "google.generativeai" in source
        ):
            provider = "google"
        elif called.endswith((".invoke_model", ".invoke_model_with_response_stream")) and (
            "boto3" in imported or "botocore" in imported
        ):
            provider = "aws-bedrock"
        if provider is None:
            continue

        reasons = _python_custom_base_url_reasons(tree, provider)
        streaming = called.endswith((".invoke_model_with_response_stream",))
        for keyword in node.keywords:
            if keyword.arg == "stream" and not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is False
            ):
                streaming = True
        if streaming:
            reasons.append("streaming is not supported by the pinned /v1/execute contract")
        if "/v1/proxy/" in source:
            reasons.append("the same file contains a deprecated /v1/proxy/ path")
        exclusion = _selection_exclusion(relative)
        candidates.append(
            {
                "path": relative.as_posix(),
                "line": node.lineno,
                "provider": provider,
                "request_kind": "sdk_request",
                "eligibility": "blocked" if reasons else "eligible_for_local_review",
                "reasons": reasons,
                "test_only": exclusion == "test_surface",
                "selectable": exclusion is None,
                "selection_exclusion": exclusion,
            }
        )
    return candidates


def _javascript_candidates(path: pathlib.Path, relative: pathlib.PurePath) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    source = "\n".join(lines)
    provider_imports = {
        "openai": re.search(r"(?:from\s+|require\s*\(|import\s*\()['\"]openai['\"]", source) is not None,
        "anthropic": "@anthropic-ai/sdk" in source,
        "google": "@google/generative-ai" in source or "@google/genai" in source,
        "aws-bedrock": "@aws-sdk/client-bedrock-runtime" in source,
    }
    custom_base_url: dict[str, bool] = {}
    for provider in provider_imports:
        match = re.search(r"\bbaseURL\s*:\s*([^,}\n]+)", source)
        if match is None:
            custom_base_url[provider] = False
            continue
        raw_value = match.group(1).strip().strip("'\"")
        custom_base_url[provider] = not _targets_official_provider(raw_value, provider)
    candidates: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        for provider, pattern, request_kind in JS_REQUEST_PATTERNS:
            if not provider_imports[provider] or not pattern.search(line):
                continue
            reasons: list[str] = []
            if custom_base_url[provider]:
                reasons.append("provider client uses a non-default or unresolved base_url")
            window = "\n".join(lines[line_number - 1 : line_number + 12])
            if re.search(r"\bstream\s*:\s*(?!false\b)", window):
                reasons.append("streaming is not supported by the pinned /v1/execute contract")
            if "/v1/proxy/" in source:
                reasons.append("the same file contains a deprecated /v1/proxy/ path")
            exclusion = _selection_exclusion(relative)
            candidates.append(
                {
                    "path": relative.as_posix(),
                    "line": line_number,
                    "provider": provider,
                    "request_kind": request_kind,
                    "eligibility": "blocked" if reasons else "eligible_for_local_review",
                    "reasons": reasons,
                    "test_only": exclusion == "test_surface",
                    "selectable": exclusion is None,
                    "selection_exclusion": exclusion,
                }
            )
    return candidates


def _provider_signal_candidates(
    path: pathlib.Path,
    relative: pathlib.PurePath,
    sdk_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find bounded raw-provider or import signals omitted by SDK call matching."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    source = "\n".join(lines)
    existing = {(item["path"], item["provider"]) for item in sdk_candidates}
    exclusion = _selection_exclusion(relative)
    signals: list[dict[str, Any]] = []
    for provider, hostname_pattern in PROVIDER_HOST_PATTERNS.items():
        hostname_lines = [index for index, line in enumerate(lines, 1) if hostname_pattern.search(line)]
        if hostname_lines:
            raw_http = DIRECT_API_PATTERN.search(source) is not None
            if not raw_http and (relative.as_posix(), provider) in existing:
                # An explicit official endpoint on the already-identified SDK seam
                # is not evidence of a second path.
                continue
            signals.append(
                {
                    "path": relative.as_posix(),
                    "line": hostname_lines[0],
                    "provider": provider,
                    "request_kind": "raw_http_provider" if raw_http else "provider_hostname",
                    "eligibility": "unresolved",
                    "reasons": ["provider hostname requires targeted bypass inspection"],
                    "test_only": exclusion == "test_surface",
                    "selectable": False,
                    "selection_exclusion": exclusion or "raw_or_unresolved_provider_path",
                }
            )
            continue
        if (relative.as_posix(), provider) in existing:
            continue
        import_pattern = PROVIDER_IMPORT_PATTERNS[provider]
        import_lines = [index for index, line in enumerate(lines, 1) if import_pattern.search(line)]
        if import_lines:
            signals.append(
                {
                    "path": relative.as_posix(),
                    "line": import_lines[0],
                    "provider": provider,
                    "request_kind": "provider_sdk_signal",
                    "eligibility": "unresolved",
                    "reasons": ["provider SDK signal has no safely identified request site"],
                    "test_only": exclusion == "test_surface",
                    "selectable": False,
                    "selection_exclusion": exclusion or "unresolved_provider_path",
                }
            )
    return signals


def fast_inspect(
    root: pathlib.Path,
    *,
    max_files: int = FAST_MAX_FILES,
    max_candidates: int = FAST_MAX_CANDIDATES,
) -> dict[str, Any]:
    """Find one obvious request seam without claiming whole-repository coverage."""

    started = time.perf_counter()
    candidates: list[dict[str, Any]] = []
    files_considered = 0
    test_files_skipped = 0
    sensitive_paths_skipped = 0
    truncated = False
    for path, relative in iter_fast_source_files(root):
        if SENSITIVE_PATH_PATTERN.search(relative.as_posix()):
            sensitive_paths_skipped += 1
            continue
        if _is_test_path(relative):
            test_files_skipped += 1
            continue
        if files_considered >= max_files or len(candidates) >= max_candidates:
            truncated = True
            break
        files_considered += 1
        sdk_candidates: list[dict[str, Any]] = []
        if path.suffix.lower() == ".py":
            sdk_candidates = _python_candidates(path, relative)
        elif path.suffix.lower() in FAST_SOURCE_SUFFIXES:
            sdk_candidates = _javascript_candidates(path, relative)
        candidates.extend(sdk_candidates)
        candidates.extend(_provider_signal_candidates(path, relative, sdk_candidates))
        if len(candidates) > max_candidates:
            truncated = True
            break

    candidates = candidates[:max_candidates]
    sdk_candidates = [item for item in candidates if item["request_kind"] == "sdk_request"]
    selectable_candidates = [item for item in sdk_candidates if item["selectable"]]
    non_test_candidates = [item for item in candidates if not item["test_only"]]
    selected: dict[str, Any] | None = None
    if truncated:
        decision = "ambiguous_scan_truncated"
    elif sensitive_paths_skipped:
        decision = "ambiguous_sensitive_path"
    elif len(selectable_candidates) > 1:
        decision = "ambiguous_multiple_seams"
    elif len(selectable_candidates) == 1:
        proposed = selectable_candidates[0]
        alternate_signals = [item for item in non_test_candidates if item is not proposed]
        if alternate_signals:
            decision = "ambiguous_adjacent_bypass"
        elif proposed["eligibility"] != "eligible_for_local_review":
            decision = "blocked_structural_condition"
        else:
            decision = "single_narrow_seam"
            selected = proposed
    elif sdk_candidates:
        decision = "ambiguous_nonproduction_surface"
    elif non_test_candidates:
        decision = "ambiguous_provider_signals"
    else:
        decision = "no_obvious_seam"

    return {
        "schema_version": "1.0",
        "phase": "fast_first_run",
        "decision": decision,
        "selected_seam": selected,
        "candidates": candidates,
        "scan": {
            "files_considered": files_considered,
            "test_files_skipped": test_files_skipped,
            "sensitive_paths_skipped": sensitive_paths_skipped,
            "candidate_limit": max_candidates,
            "file_limit": max_files,
            "truncated": truncated,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        },
        "evidence_level": "source_inspected",
        "does_not_establish": FAST_DOES_NOT_ESTABLISH,
    }


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
    parser.add_argument(
        "--fast",
        action="store_true",
        help="run bounded first-run seam discovery instead of exhaustive coverage inventory",
    )
    parser.add_argument("--max-files", type=int, default=FAST_MAX_FILES, help=argparse.SUPPRESS)
    parser.add_argument("--max-candidates", type=int, default=FAST_MAX_CANDIDATES, help=argparse.SUPPRESS)
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
    if args.fast:
        if args.max_files < 1 or args.max_candidates < 1:
            print("fast discovery limits must be positive", file=sys.stderr)
            return 1
        print(json.dumps(fast_inspect(root, max_files=args.max_files, max_candidates=args.max_candidates), indent=2, sort_keys=True))
        return 0
    print(json.dumps(inspect(root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
