#!/usr/bin/env python3
"""Deterministically prepare one narrow OpenAI Responses seam for Keel.

This is deliberately a golden-path helper, not a general code-rewriting agent.
It supports one synchronous Python function shape and fails without editing for
every unrecognized, ambiguous, streaming, or custom-endpoint case. It never
reads a credential and establishes local preparation only.
"""
from __future__ import annotations

import argparse
import ast
import collections
import difflib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any


sys.dont_write_bytecode = True

OFFICIAL_ORIGINS = {
    "https://github.com/keelapi/keel-setup",
    "https://github.com/keelapi/keel-setup.git",
    "git@github.com:keelapi/keel-setup.git",
    "ssh://git@github.com/keelapi/keel-setup.git",
}
GOLDEN_SHAPE = "python.openai.responses.create.injected_client.v1"
MAX_INSTRUCTION_FILES = 20
MAX_CLASSIFICATION_FILES = 400
MAX_TEST_FILES = 50
MAX_SOURCE_BYTES = 1_000_000
MAX_DIFF_BYTES = 64 * 1024
CLASSIFICATION_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".worktrees", ".tox", ".mypy_cache", ".pytest_cache",
    "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__",
}
SAFE_SUBPROCESS_ENV = {
    "PATH": os.defpath,
    "LANG": "C",
    "LC_ALL": "C",
    "PYTHONDONTWRITEBYTECODE": "1",
}
DOES_NOT_ESTABLISH = [
    "live_routing",
    "policy_activation",
    "provider_success",
    "runtime_verification",
    "bypass_absence",
    "whole_application_protection",
]
DIFF_CREDENTIAL_SHAPE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?key|secret|password|token|cookie|jwt|csrf|credential)"
    r"\s*[:=]\s*['\"](?!test-only-placeholder)[^'\"]{8,}['\"]|"
    r"(?<![A-Za-z0-9_])(?:ks_|sk-|sk_|pk_|rk_|ghp_|gho_|xox[baprs]-)[A-Za-z0-9_\-]{8,}"
)


class PipelineFailure(Exception):
    def __init__(self, outcome: str, reason: str):
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


@dataclass(frozen=True)
class GoldenSeam:
    path: pathlib.PurePosixPath
    line: int
    symbol: str
    signature: dict[str, Any]
    signature_line: str
    function: ast.FunctionDef
    client_if_index: int
    response_index: int
    summary_index: int
    model_expression: str
    input_expression: str
    instructions: str
    coupled_test: pathlib.PurePosixPath | None

    def public(self) -> dict[str, Any]:
        return {
            "path": self.path.as_posix(),
            "line": self.line,
            "symbol": self.symbol,
            "callable_signature": self.signature,
            "provider": "openai",
            "sdk_shape": GOLDEN_SHAPE,
            "custom_endpoint": False,
            "injection_hook": "client" if any(
                item.get("name") == "client" for item in self.signature["parameters"]
            ) else None,
            "directly_coupled_test": self.coupled_test.as_posix() if self.coupled_test else None,
            "blockers": [],
        }


def _run(
    argv: list[str],
    *,
    cwd: pathlib.Path,
    timeout: int = 10,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=dict(SAFE_SUBPROCESS_ENV if env is None else env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PipelineFailure("validation_failed", f"bounded command failed: {argv[0]}: {exc}") from exc


def _git(repo: pathlib.Path, *args: str, timeout: int = 10) -> str:
    result = _run(["git", "-C", str(repo), *args], cwd=repo, timeout=timeout)
    if result.returncode != 0:
        raise PipelineFailure("validation_failed", f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PipelineFailure("untrusted_bundle", f"required helper is unavailable: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_release(bundle: pathlib.Path, expected_sha: str) -> dict[str, Any]:
    """Verify public checkout identity, immutable revision, cleanliness, and bundle manifest."""

    if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
        raise PipelineFailure("untrusted_bundle", "bundle SHA must be 40 lowercase hexadecimal characters")
    if not bundle.is_dir():
        raise PipelineFailure("untrusted_bundle", "bundle path is not a directory")
    head = _git(bundle, "rev-parse", "HEAD")
    if head != expected_sha:
        raise PipelineFailure("untrusted_bundle", "bundle checkout does not match the requested immutable SHA")
    origin = _git(bundle, "remote", "get-url", "origin")
    if origin not in OFFICIAL_ORIGINS:
        raise PipelineFailure("untrusted_bundle", "bundle origin is not the official public repository")
    if _git(bundle, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PipelineFailure("untrusted_bundle", "bundle checkout is not clean")
    verifier = bundle / "scripts" / "check_release_bundle.py"
    if not verifier.is_file():
        raise PipelineFailure("untrusted_bundle", "bundle verifier is missing")
    checked = _run([sys.executable, str(verifier)], cwd=bundle, timeout=20)
    if checked.returncode != 0 or "PASS: public setup bundle" not in checked.stdout:
        raise PipelineFailure("untrusted_bundle", "public allowlist, provenance, or checksum verification failed")
    try:
        source = json.loads((bundle / "SOURCE.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineFailure("untrusted_bundle", "SOURCE.json is unreadable") from exc
    product_digest = source.get("product_source_sha256")
    if not isinstance(product_digest, str) or re.fullmatch(r"[0-9a-f]{64}", product_digest) is None:
        raise PipelineFailure("untrusted_bundle", "SOURCE.json has no valid product digest")
    return {
        "status": "trusted",
        "bundle_sha": head,
        "public_release_version": source.get("public_release_version"),
        "product_source_sha256": product_digest,
        "allowlist_and_sha256s": "passed",
    }


def classify_repository(repo: pathlib.Path) -> dict[str, Any]:
    """Return bounded repository facts without importing or executing application code."""

    revision = _git(repo, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise PipelineFailure("model_review_required", "repository has no immutable Git revision")
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    if dirty:
        raise PipelineFailure("model_review_required", "golden path requires a clean application checkout")

    instruction_names = {"AGENTS.md", "CLAUDE.md", ".cursorrules", "copilot-instructions.md"}
    instructions: list[str] = []
    copilot_instructions = repo / ".github" / "copilot-instructions.md"
    if copilot_instructions.is_file():
        instructions.append(".github/copilot-instructions.md")
    files_considered = 0
    for current, directories, filenames in os.walk(repo, topdown=True, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if name not in CLASSIFICATION_SKIP_DIRS and not name.startswith(".")
        )
        current_path = pathlib.Path(current)
        for filename in sorted(filenames):
            files_considered += 1
            if files_considered > MAX_CLASSIFICATION_FILES:
                raise PipelineFailure("model_review_required", "repository classification exceeded its bounded file limit")
            path = current_path / filename
            if path.is_symlink():
                continue
            if filename in instruction_names:
                instructions.append(path.relative_to(repo).as_posix())
                if len(instructions) > MAX_INSTRUCTION_FILES:
                    raise PipelineFailure("model_review_required", "repository instruction set exceeds the bounded limit")

    manifests: list[str] = []
    for pattern in ("pyproject.toml", "requirements*.txt", "setup.py", "setup.cfg", "Pipfile"):
        manifests.extend(path.relative_to(repo).as_posix() for path in sorted(repo.glob(pattern)) if path.is_file())
    manifests = sorted(set(manifests))
    if not manifests:
        raise PipelineFailure("unsupported_shape", "no Python dependency manifest was found")
    dependency_signal = False
    for relative in manifests:
        path = repo / relative
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise PipelineFailure("model_review_required", "Python dependency manifest exceeds the bounded limit")
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PipelineFailure("model_review_required", "Python dependency manifest could not be inspected") from exc
        if re.search(r"(?im)^\s*openai(?:\[[^]]+\])?\s*(?:[<>=!~]|$)", content):
            dependency_signal = True
    if not dependency_signal:
        raise PipelineFailure("unsupported_shape", "official OpenAI Python SDK dependency was not established")
    internal = (repo / "keel-setup" / "SKILL.md").is_file() or (repo / "shared" / "CONSTITUTION.md").is_file()
    if internal:
        raise PipelineFailure("model_review_required", "Keel's own source repository is not a customer adapter target")
    if instructions:
        raise PipelineFailure("model_review_required", "repository instructions require model review before editing")
    return {
        "revision": revision,
        "dirty": False,
        "repo_instructions": instructions,
        "runtime": "python",
        "dependency_manifests": manifests,
        "existing_setup_state": (repo / ".keel" / "setup-state.json").exists(),
        "repository_kind": "application",
        "files_considered": files_considered,
    }


def _attribute_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _default_value(node: ast.AST | None) -> Any:
    if node is None:
        return {"required": True}
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return {"expression": ast.unparse(node)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"expression": ast.unparse(node)}


def callable_signature(function: ast.FunctionDef) -> dict[str, Any]:
    positional = [*function.args.posonlyargs, *function.args.args]
    positional_defaults: list[ast.AST | None] = [None] * (len(positional) - len(function.args.defaults)) + list(function.args.defaults)
    parameters: list[dict[str, Any]] = []
    for index, (argument, default) in enumerate(zip(positional, positional_defaults)):
        parameters.append(
            {
                "name": argument.arg,
                "kind": "positional_only" if index < len(function.args.posonlyargs) else "positional_or_keyword",
                "default": _default_value(default),
            }
        )
    if function.args.vararg:
        parameters.append({"name": function.args.vararg.arg, "kind": "var_positional", "default": {"required": False}})
    for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults):
        parameters.append({"name": argument.arg, "kind": "keyword_only", "default": _default_value(default)})
    if function.args.kwarg:
        parameters.append({"name": function.args.kwarg.arg, "kind": "var_keyword", "default": {"required": False}})
    return {"parameters": parameters}


def _is_none_default(function: ast.FunctionDef, name: str) -> bool:
    for argument, default in zip(function.args.kwonlyargs, function.args.kw_defaults):
        if argument.arg == name:
            return isinstance(default, ast.Constant) and default.value is None
    return False


def _find_coupled_tests(repo: pathlib.Path, symbol: str) -> list[pathlib.PurePosixPath]:
    matches: list[pathlib.PurePosixPath] = []
    considered = 0
    for current, directories, filenames in os.walk(repo, topdown=True, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if name not in CLASSIFICATION_SKIP_DIRS and not name.startswith(".")
        )
        current_path = pathlib.Path(current)
        for filename in sorted(name for name in filenames if name.endswith(".py")):
            path = current_path / filename
            relative = pathlib.PurePosixPath(path.relative_to(repo).as_posix())
            lowered = {part.lower() for part in relative.parts}
            if not (lowered & {"test", "tests"} or relative.name.startswith("test_")):
                continue
            considered += 1
            if considered > MAX_TEST_FILES:
                raise PipelineFailure("model_review_required", "directly relevant tests exceed the bounded limit")
            try:
                if path.is_symlink() or path.stat().st_size > MAX_SOURCE_BYTES:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative.as_posix())
            except (OSError, UnicodeDecodeError, SyntaxError):
                raise PipelineFailure("model_review_required", "a candidate compatibility test could not be parsed")
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != symbol:
                    continue
                if any(keyword.arg == "client" for keyword in node.keywords):
                    matches.append(relative)
                    break
    return matches


def discover_golden_seam(repo: pathlib.Path, inventory: Any) -> GoldenSeam:
    report = inventory.fast_inspect(repo)
    decision = report.get("decision")
    if decision == "ambiguous_multiple_seams" or str(decision).startswith("ambiguous_"):
        raise PipelineFailure("ambiguous", f"bounded discovery returned {decision}")
    if decision == "blocked_structural_condition":
        raise PipelineFailure("unsupported_shape", "streaming or a custom provider endpoint is outside the golden path")
    if decision != "single_narrow_seam":
        raise PipelineFailure("model_review_required", f"bounded discovery returned {decision}")
    selected = report.get("selected_seam")
    if not isinstance(selected, dict) or selected.get("provider") != "openai":
        raise PipelineFailure("unsupported_shape", "only the official OpenAI Responses API is supported")
    raw_path = selected.get("path")
    if not isinstance(raw_path, str) or pathlib.PurePosixPath(raw_path).suffix != ".py":
        raise PipelineFailure("unsupported_shape", "only Python source is supported")
    relative = pathlib.PurePosixPath(raw_path)
    path = repo.joinpath(*relative.parts)
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=raw_path)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise PipelineFailure("unsupported_shape", "selected Python source could not be parsed") from exc
    line = selected.get("line")
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and node.lineno == line and _attribute_name(node.func).endswith(".responses.create")
    ]
    if len(calls) != 1:
        raise PipelineFailure("model_review_required", "selected request does not have one recognized Responses call")
    call = calls[0]
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.lineno <= call.lineno <= (node.end_lineno or node.lineno)
    ]
    if len(functions) != 1:
        raise PipelineFailure("model_review_required", "request is not inside one top-level synchronous function")
    function = functions[0]
    if function.decorator_list:
        raise PipelineFailure("model_review_required", "decorated callables are outside the golden path")
    signature_line = source.splitlines()[function.lineno - 1]
    if not signature_line.startswith("def ") or not signature_line.rstrip().endswith(":"):
        raise PipelineFailure("model_review_required", "multiline or nonstandard callable signatures require model review")
    if not _is_none_default(function, "client"):
        raise PipelineFailure("model_review_required", "golden path requires the recognized client=None injection hook")
    if _attribute_name(call.func) != "client.responses.create":
        raise PipelineFailure("model_review_required", "Responses call receiver is an unrecognized wrapper")
    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg is not None}
    if set(keywords) != {"model", "instructions", "input"} or call.args:
        raise PipelineFailure("model_review_required", "Responses request arguments do not match the golden shape")
    try:
        instructions = ast.literal_eval(keywords["instructions"])
    except (ValueError, TypeError) as exc:
        raise PipelineFailure("model_review_required", "instructions are not a static string") from exc
    if not isinstance(instructions, str):
        raise PipelineFailure("model_review_required", "instructions are not a static string")

    client_if_index = response_index = summary_index = -1
    response_name = ""
    summary_name = ""
    for index, statement in enumerate(function.body):
        if isinstance(statement, ast.If) and any(
            isinstance(node, ast.Call) and _attribute_name(node.func) == "OpenAI" for node in ast.walk(statement)
        ):
            client_if_index = index
        if isinstance(statement, ast.Assign) and statement.value is call and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            response_index = index
            response_name = statement.targets[0].id
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Call)
            and _attribute_name(statement.value.func) == f"{response_name}.output_text.strip"
        ):
            summary_index = index
            summary_name = statement.targets[0].id
    if not (0 <= client_if_index < response_index < summary_index):
        raise PipelineFailure("model_review_required", "provider construction and response mapping do not match the golden shape")
    if response_name != "response" or summary_name != "summary":
        raise PipelineFailure("model_review_required", "response variable shape is not recognized")
    constructor_calls = [node for node in ast.walk(function.body[client_if_index]) if isinstance(node, ast.Call) and _attribute_name(node.func) == "OpenAI"]
    if len(constructor_calls) != 1 or constructor_calls[0].args or constructor_calls[0].keywords:
        raise PipelineFailure("unsupported_shape", "OpenAI client is not using the default official endpoint")
    coupled = _find_coupled_tests(repo, function.name)
    if len(coupled) > 1:
        raise PipelineFailure("model_review_required", "more than one test file uses the provider-client hook")
    model_expression = ast.get_source_segment(source, keywords["model"])
    input_expression = ast.get_source_segment(source, keywords["input"])
    if not model_expression or not input_expression:
        raise PipelineFailure("model_review_required", "request expressions could not be preserved")
    return GoldenSeam(
        path=relative,
        line=int(line),
        symbol=function.name,
        signature=callable_signature(function),
        signature_line=signature_line,
        function=function,
        client_if_index=client_if_index,
        response_index=response_index,
        summary_index=summary_index,
        model_expression=model_expression,
        input_expression=input_expression,
        instructions=instructions,
        coupled_test=coupled[0] if coupled else None,
    )


def _indent_segment(source: str, node: ast.AST) -> str:
    if not getattr(node, "lineno", None) or not getattr(node, "end_lineno", None):
        raise PipelineFailure("model_review_required", "callable behavior could not be preserved")
    # The selected callable is top-level. Its statement lines already carry the
    # correct function-body and nested indentation, so copying the original
    # source slice is both safer and more faithful than normalizing indentation.
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def _adapter_support() -> str:
    return '''KEEL_EXECUTE_URL = "https://api.keelapi.com/v1/execute"
KEEL_MAX_RESPONSE_BYTES = 1024 * 1024
KEEL_REQUEST_TIMEOUT_SECONDS = 30


class _KeelNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _keel_read_json(response: Any) -> dict[str, Any]:
    raw = response.read(KEEL_MAX_RESPONSE_BYTES + 1)
    if len(raw) > KEEL_MAX_RESPONSE_BYTES:
        raise RuntimeError("Keel returned an oversized response.")
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Keel returned an invalid response.") from exc
    if not isinstance(body, dict):
        raise RuntimeError("Keel returned an invalid response.")
    return body


def _keel_openai_output_text(body: dict[str, Any]) -> str:
    provider_output = body.get("output")
    if not isinstance(provider_output, dict):
        raise RuntimeError("Keel returned an invalid provider response.")
    convenience = provider_output.get("output_text")
    if isinstance(convenience, str):
        return convenience.strip()
    output = provider_output.get("output")
    if not isinstance(output, list):
        raise RuntimeError("Keel returned an invalid provider response.")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts).strip()
'''


def generate_adapter(repo: pathlib.Path, seam: GoldenSeam) -> tuple[str, str | None]:
    path = repo.joinpath(*seam.path.parts)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    reserved = {
        "KEEL_EXECUTE_URL", "KEEL_MAX_RESPONSE_BYTES", "KEEL_REQUEST_TIMEOUT_SECONDS",
        "_KeelNoRedirectHandler", "_keel_read_json", "_keel_openai_output_text",
    }
    top_names = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    top_names.update(
        target.id for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(target, ast.Name)
    )
    if reserved & top_names:
        raise PipelineFailure("model_review_required", "adapter support names conflict with application source")

    lines = source.splitlines(keepends=True)
    function = seam.function
    prefix = [_indent_segment(source, statement) for statement in function.body[:seam.client_if_index]]
    suffix = [_indent_segment(source, statement) for statement in function.body[seam.summary_index + 1 :]]
    function_text = "\n".join(
        [
            seam.signature_line,
            *prefix,
            "",
            "    key = os.getenv(\"KEEL_API_KEY\")",
            "    if not key:",
            "        raise RuntimeError(\"KEEL_API_KEY is not set.\")",
            "",
            "    payload = json.dumps(",
            "        {",
            "            \"provider\": \"openai\",",
            f"            \"model\": {seam.model_expression},",
            "            \"input\": {\"messages\": [",
            f"                {{\"role\": \"developer\", \"content\": {seam.instructions!r}}},",
            f"                {{\"role\": \"user\", \"content\": {seam.input_expression}}},",
            "            ]},",
            "        },",
            "        separators=(\",\", \":\"),",
            "    ).encode(\"utf-8\")",
            "    request = urllib.request.Request(",
            "        KEEL_EXECUTE_URL,",
            "        data=payload,",
            "        method=\"POST\",",
            "        headers={",
            "            \"Authorization\": f\"Bearer {key}\",",
            "            \"Content-Type\": \"application/json\",",
            "            \"X-Keel-Timestamp\": str(int(time.time())),",
            "            \"X-Keel-Nonce\": secrets.token_urlsafe(18),",
            "        },",
            "    )",
            "    opener = client if client is not None else urllib.request.build_opener(_KeelNoRedirectHandler())",
            "    if not hasattr(opener, \"open\"):",
            "        raise TypeError(\"client must be an HTTP opener for the Keel endpoint\")",
            "    try:",
            "        with opener.open(request, timeout=KEEL_REQUEST_TIMEOUT_SECONDS) as response:",
            "            status = response.status",
            "            body = _keel_read_json(response)",
            "    except (urllib.error.HTTPError, urllib.error.URLError) as exc:",
            "        raise RuntimeError(\"Keel did not complete the request.\") from exc",
            "    governance = body.get(\"governance\")",
            "    if (",
            "        status != 200",
            "        or body.get(\"status\") != \"completed\"",
            "        or not isinstance(governance, dict)",
            "        or governance.get(\"decision\") != \"allow\"",
            "    ):",
            "        raise RuntimeError(\"Keel did not complete the request.\")",
            "    summary = _keel_openai_output_text(body)",
            *suffix,
            "",
        ]
    )
    lines[function.lineno - 1 : function.end_lineno] = [_adapter_support() + "\n\n\n" + function_text]
    import_line = 0
    for index, line in enumerate(lines):
        if line.startswith("from __future__ import"):
            import_line = index + 1
            break
    import_block = "\nimport json\nimport secrets\nimport time\nimport urllib.error\nimport urllib.request\n"
    lines.insert(import_line, import_block)
    application = "".join(lines)
    test_source = _generate_coupled_test(repo, seam) if seam.coupled_test else None
    return application, test_source


def _generate_coupled_test(repo: pathlib.Path, seam: GoldenSeam) -> str:
    assert seam.coupled_test is not None
    path = repo.joinpath(*seam.coupled_test.parts)
    original = path.read_text(encoding="utf-8")
    required = (
        "class FakeResponses", "class FakeClient", "class SummarizerTests",
        "test_sends_text_to_the_selected_model", "test_rejects_empty_text_before_calling_the_provider",
        "test_rejects_overlong_text_before_calling_the_provider", "test_rejects_an_empty_model_response",
    )
    if any(marker not in original for marker in required):
        raise PipelineFailure("model_review_required", "coupled test structure is not the recognized golden fixture")
    module = seam.path.with_suffix("").as_posix().replace("/", ".")
    return f'''from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from {module} import MAX_INPUT_CHARACTERS, {seam.symbol}


class FakeResponse:
    def __init__(self, output_text: str = "A short summary.") -> None:
        self.status = 200
        self._body = json.dumps({{
            "status": "completed",
            "governance": {{"decision": "allow"}},
            "output": {{"output_text": output_text}},
        }}).encode()

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeClient:
    def __init__(self, output_text: str = "A short summary.") -> None:
        self.output_text = output_text
        self.calls: list[object] = []

    def open(self, request: object, *, timeout: int) -> FakeResponse:
        self.calls.append(request)
        return FakeResponse(self.output_text)


class SummarizerTests(unittest.TestCase):
    def test_sends_text_to_the_selected_model(self) -> None:
        client = FakeClient("The answer is 42.")
        with patch.dict(os.environ, {{"KEEL_API_KEY": "test-only-placeholder"}}):
            result = {seam.symbol}("  A report whose answer is 42.  ", client=client, model="test-model")
        self.assertEqual(result, "The answer is 42.")
        self.assertEqual(len(client.calls), 1)
        request = client.calls[0]
        self.assertEqual(request.full_url, "https://api.keelapi.com/v1/execute")
        payload = json.loads(request.data)
        self.assertEqual(payload["provider"], "openai")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["input"]["messages"][1]["content"], "A report whose answer is 42.")
        self.assertIn("three concise sentences", payload["input"]["messages"][0]["content"])

    def test_rejects_empty_text_before_calling_the_provider(self) -> None:
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            {seam.symbol}("   ", client=client)
        self.assertEqual(client.calls, [])

    def test_rejects_overlong_text_before_calling_the_provider(self) -> None:
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            {seam.symbol}("x" * (MAX_INPUT_CHARACTERS + 1), client=client)
        self.assertEqual(client.calls, [])

    def test_rejects_an_empty_model_response(self) -> None:
        client = FakeClient("  ")
        with patch.dict(os.environ, {{"KEEL_API_KEY": "test-only-placeholder"}}):
            with self.assertRaisesRegex(ValueError, "empty summary"):
                {seam.symbol}("Some source text", client=client)


if __name__ == "__main__":
    unittest.main()
'''


def _function_signature(source: str, symbol: str) -> dict[str, Any]:
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == symbol]
    if len(functions) != 1:
        raise PipelineFailure("validation_failed", "selected callable is missing after generation")
    return callable_signature(functions[0])


def validate_patch(
    repo: pathlib.Path,
    seam: GoldenSeam,
    before_source: str,
    after_source: str,
    allowed_paths: set[str],
    inventory: Any,
) -> dict[str, Any]:
    """Validate the generated diff without importing application code."""

    try:
        compile(after_source, seam.path.as_posix(), "exec")
    except SyntaxError as exc:
        raise PipelineFailure("validation_failed", f"generated application source does not compile: line {exc.lineno}") from exc
    if _function_signature(before_source, seam.symbol) != _function_signature(after_source, seam.symbol):
        raise PipelineFailure("unsafe_contract_change", "callable parameters, kinds, or defaults changed")
    if "/v1/execute" not in after_source:
        raise PipelineFailure("validation_failed", "generated source has no /v1/execute route")
    if "/v1/proxy/" in after_source:
        raise PipelineFailure("validation_failed", "generated source contains /v1/proxy/*")
    lowered = after_source.lower()
    if "api.openai.com" in lowered or re.search(r"(?:from|import)\s+openai\b", after_source):
        raise PipelineFailure("validation_failed", "generated source retains a direct provider path")
    tree = ast.parse(after_source)
    provider_calls = inventory._python_candidates(repo.joinpath(*seam.path.parts), seam.path)
    if provider_calls or any(_attribute_name(node.func).endswith(".responses.create") for node in ast.walk(tree) if isinstance(node, ast.Call)):
        raise PipelineFailure("validation_failed", "generated source retains a direct provider execution")
    called = {_attribute_name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    if "time.time" not in called or "secrets.token_urlsafe" not in called:
        raise PipelineFailure("validation_failed", "fresh timestamp and nonce generation are missing")
    if re.search(r"(?i)KEEL_API_KEY\s*[:=]\s*['\"][^'\"]+", after_source):
        raise PipelineFailure("validation_failed", "generated source contains a credential value")
    status = _run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo,
    )
    if status.returncode != 0:
        raise PipelineFailure("validation_failed", "changed paths could not be established")
    changed = {
        line[3:] for line in status.stdout.splitlines()
        if len(line) > 3 and not line[3:].startswith(".keel/")
    }
    if changed != allowed_paths:
        raise PipelineFailure("validation_failed", "changed paths exceed the deterministic allowlist")
    focused_test = None
    if seam.coupled_test:
        module = seam.coupled_test.with_suffix("").as_posix().replace("/", ".")
        focused_test = f"{module}.SummarizerTests.test_sends_text_to_the_selected_model"
        tested = _run(
            [sys.executable, "-m", "unittest", focused_test, "-v"],
            cwd=repo,
            timeout=15,
            env=SAFE_SUBPROCESS_ENV,
        )
        if tested.returncode != 0:
            raise PipelineFailure("validation_failed", "the one focused callable-compatibility test failed")
    return {
        "callable_signature_compatible": True,
        "compile_without_import": True,
        "execute_route": "POST /v1/execute",
        "direct_provider_removed": True,
        "proxy_absent": True,
        "provider_fallback_absent": True,
        "freshness_at_request_time": True,
        "credential_value_absent": True,
        "changed_paths": sorted(allowed_paths),
        "focused_compatibility_test": focused_test,
    }


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ensure_state_ignored(repo: pathlib.Path) -> bool:
    checked = _run(["git", "-C", str(repo), "check-ignore", "-q", ".keel/setup-state.json"], cwd=repo)
    if checked.returncode == 0:
        return False
    path = repo / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    suffix = "" if not existing or existing.endswith("\n") else "\n"
    _write(path, existing + suffix + ".keel/setup-state.json\n")
    checked = _run(["git", "-C", str(repo), "check-ignore", "-q", ".keel/setup-state.json"], cwd=repo)
    if checked.returncode != 0:
        raise PipelineFailure("validation_failed", "local setup state could not be git-ignored")
    return True


def _restore(repo: pathlib.Path, originals: dict[pathlib.PurePosixPath, bytes | None]) -> None:
    for relative, content in originals.items():
        path = repo.joinpath(*relative.parts)
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    state = repo / ".keel" / "setup-state.json"
    state.unlink(missing_ok=True)
    try:
        state.parent.rmdir()
    except OSError:
        pass


def _line_change_stats(before: str, after: str) -> tuple[int, int, int]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(
        None,
        before_lines,
        after_lines,
        autojunk=False,
    )
    insertions = deletions = whitespace_only = 0
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = before_lines[before_start:before_end]
        added = after_lines[after_start:after_end]
        deletions += len(removed)
        insertions += len(added)
        if tag != "replace":
            continue
        removed_exact = collections.Counter(removed)
        added_exact = collections.Counter(added)
        exact_overlap = removed_exact & added_exact
        removed_exact -= exact_overlap
        added_exact -= exact_overlap
        removed_normalized = collections.Counter(
            normalized
            for line, count in removed_exact.items()
            for _ in range(count)
            if (normalized := re.sub(r"\s+", "", line))
        )
        added_normalized = collections.Counter(
            normalized
            for line, count in added_exact.items()
            for _ in range(count)
            if (normalized := re.sub(r"\s+", "", line))
        )
        whitespace_only += 2 * sum((removed_normalized & added_normalized).values())
    return insertions, deletions, whitespace_only


def build_diff_evidence(
    repo: pathlib.Path,
    allowed_paths: set[str],
    originals: dict[pathlib.PurePosixPath, bytes | None],
    *,
    max_bytes: int = MAX_DIFF_BYTES,
) -> dict[str, Any]:
    """Return a bounded, source-only unified diff for the exact changed-path allowlist."""

    sections: list[str] = []
    stats: dict[str, dict[str, int]] = {}
    whitespace_only = 0
    for relative_text in sorted(allowed_paths):
        relative = pathlib.PurePosixPath(relative_text)
        original = originals.get(relative)
        if relative not in originals:
            raise PipelineFailure("validation_failed", "diff evidence path was not captured before editing")
        try:
            before = original.decode("utf-8") if original is not None else ""
            after = repo.joinpath(*relative.parts).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PipelineFailure("validation_failed", "diff evidence is not bounded UTF-8 text") from exc
        insertions, deletions, whitespace = _line_change_stats(before, after)
        stats[relative_text] = {"insertions": insertions, "deletions": deletions}
        whitespace_only += whitespace
        lines = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"a/{relative_text}",
                tofile=f"b/{relative_text}",
                lineterm="",
            )
        )
        if lines:
            sections.append("\n".join(lines) + "\n")

    full_diff = "".join(sections)
    if DIFF_CREDENTIAL_SHAPE.search(full_diff):
        raise PipelineFailure("validation_failed", "bounded diff may contain credential material")
    encoded = full_diff.encode("utf-8")
    truncated = len(encoded) > max_bytes
    if truncated:
        bounded = encoded[:max_bytes].decode("utf-8", errors="ignore")
    else:
        bounded = full_diff
    return {
        "unified_diff": bounded,
        "per_file": stats,
        "whitespace_only_changed_lines": whitespace_only,
        "diff_truncated": truncated,
        "max_bytes": max_bytes,
    }


def run_pipeline(bundle: pathlib.Path, bundle_sha: str, repo: pathlib.Path) -> dict[str, Any]:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    originals: dict[pathlib.PurePosixPath, bytes | None] = {}
    edited = False
    try:
        mark = time.perf_counter()
        trust = verify_release(bundle, bundle_sha)
        timings["release_trust_ms"] = round((time.perf_counter() - mark) * 1000, 3)

        mark = time.perf_counter()
        repository = classify_repository(repo)
        timings["repository_classification_ms"] = round((time.perf_counter() - mark) * 1000, 3)
        if repository["existing_setup_state"]:
            raise PipelineFailure("model_review_required", "existing setup state must resume through the standard lifecycle")

        inventory = _load_module("keel_fast_inventory", bundle / "keel-setup" / "scripts" / "inventory.py")
        setup_state = _load_module("keel_fast_setup_state", bundle / "keel-setup" / "scripts" / "setup_state.py")
        mark = time.perf_counter()
        seam = discover_golden_seam(repo, inventory)
        timings["seam_discovery_ms"] = round((time.perf_counter() - mark) * 1000, 3)

        app_path = repo.joinpath(*seam.path.parts)
        app_before = app_path.read_text(encoding="utf-8")
        originals[seam.path] = app_path.read_bytes()
        if seam.coupled_test:
            test_path = repo.joinpath(*seam.coupled_test.parts)
            originals[seam.coupled_test] = test_path.read_bytes()
        ignore_relative = pathlib.PurePosixPath(".gitignore")
        ignore_path = repo / ".gitignore"
        originals[ignore_relative] = ignore_path.read_bytes() if ignore_path.exists() else None

        mark = time.perf_counter()
        app_after, test_after = generate_adapter(repo, seam)
        edited = True
        _write(app_path, app_after)
        if seam.coupled_test and test_after is not None:
            _write(repo.joinpath(*seam.coupled_test.parts), test_after)
        ignore_changed = _ensure_state_ignored(repo)
        timings["adapter_generation_ms"] = round((time.perf_counter() - mark) * 1000, 3)

        allowed = {seam.path.as_posix()}
        if seam.coupled_test:
            allowed.add(seam.coupled_test.as_posix())
        if ignore_changed:
            allowed.add(".gitignore")
        mark = time.perf_counter()
        validation = validate_patch(repo, seam, app_before, app_after, allowed, inventory)
        timings["patch_validation_ms"] = round((time.perf_counter() - mark) * 1000, 3)

        diff_evidence = build_diff_evidence(repo, allowed, originals)

        mark = time.perf_counter()
        state_path = repo / ".keel" / "setup-state.json"
        initialized = setup_state.begin(state_path, repo)
        if not initialized.get("state_persisted"):
            raise PipelineFailure("validation_failed", "setup state initialization failed")
        failures = setup_state.mark_waiting_for_human(
            state_path,
            repo,
            provider="openai",
            pinned_skill_ref=bundle_sha,
            application_revision=repository["revision"],
            changed_paths=sorted(allowed),
            focused_checks=list(setup_state.REQUIRED_PRE_GATE_CHECKS),
        )
        if failures:
            raise PipelineFailure("validation_failed", "setup state refused the deterministic preparation")
        timings["setup_state_ms"] = round((time.perf_counter() - mark) * 1000, 3)
        timings["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {
            "schema_version": "1.0",
            "outcome": "ready_for_human",
            "trusted_release": trust,
            "repository": repository,
            "seam": seam.public(),
            "validation": validation,
            "diff": diff_evidence,
            "setup_state": {"stage": "waiting_for_human", "path": ".keel/setup-state.json"},
            "timings": timings,
            "evidence_level": "source_inspected",
            "does_not_establish": DOES_NOT_ESTABLISH,
        }
    except PipelineFailure as exc:
        if edited:
            _restore(repo, originals)
        timings["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {
            "schema_version": "1.0",
            "outcome": exc.outcome,
            "reason": exc.reason,
            "changed_paths": [],
            "timings": timings,
            "evidence_level": "unresolved",
            "does_not_establish": DOES_NOT_ESTABLISH,
        }
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if edited:
            _restore(repo, originals)
        timings["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return {
            "schema_version": "1.0",
            "outcome": "validation_failed",
            "reason": f"bounded local operation failed: {type(exc).__name__}",
            "changed_paths": [],
            "timings": timings,
            "evidence_level": "unresolved",
            "does_not_establish": DOES_NOT_ESTABLISH,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=pathlib.Path, required=True)
    parser.add_argument("--bundle-sha", required=True)
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    args = parser.parse_args(argv)
    result = run_pipeline(args.bundle.resolve(), args.bundle_sha, args.repo.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["outcome"] == "ready_for_human" else 2


if __name__ == "__main__":
    raise SystemExit(main())
