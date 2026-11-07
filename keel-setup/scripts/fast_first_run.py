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
import contextlib
import difflib
import importlib.util
import hashlib
import stat
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
MAX_SCAN_ENTRIES = 10_000
MAX_SCAN_DIRECTORIES = 512
MAX_SCAN_DEPTH = 24
MAX_SCAN_SECONDS = 10
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
            "callable_signature": {"parameters": [
                {"name": item["name"], "kind": item["kind"],
                 "default": {"required": isinstance(item["default"], dict) and item["default"].get("required") is True}}
                for item in self.signature["parameters"]
            ]},
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


def _git(repo: pathlib.Path, *args: str, timeout: int = 10, path_output: bool = False) -> str:
    result = _run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args], cwd=repo, timeout=timeout)
    if result.returncode != 0:
        raise PipelineFailure("validation_failed", f"git {' '.join(args)} failed")
    return result.stdout.removesuffix("\n") if path_output else result.stdout.strip()


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PipelineFailure("untrusted_bundle", f"required helper is unavailable: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_pinned_release(bundle_sha: str, *, bundle: pathlib.Path | None = None) -> None:
    """Authenticate the complete pinned snapshot without executing bundle code.

    Each standalone entrypoint carries this gate because a sibling import cannot
    establish its own trust. The entrypoint and caller-supplied pin are trusted.
    An explicit bundle is used only by the orchestrator; credential commands
    always derive their bundle from the trusted entrypoint's installation path.
    """
    try:
        if re.fullmatch(r"[0-9a-f]{40}", bundle_sha) is None:
            raise ValueError("invalid bundle SHA")
        bundle = (pathlib.Path(__file__).resolve().parents[2] if bundle is None else bundle.resolve())

        def git(*args: str) -> bytes:
            """Read local Git data without inherited configuration or hooks."""
            result = subprocess.run(
                ["git", "--no-replace-objects", "-c", "core.fsmonitor=false",
                 "-C", str(bundle), *args],
                env={
                    "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
                },
                capture_output=True, timeout=20, check=True,
            )
            return result.stdout

        worktree = pathlib.Path(git("rev-parse", "--show-toplevel").decode().removesuffix("\n")).resolve()
        if bundle != worktree:
            raise ValueError("bundle must be the Git worktree root")
        if git("rev-parse", "HEAD").decode().strip() != bundle_sha:
            raise ValueError("bundle revision mismatch")
        if git("remote", "get-url", "origin").decode().strip() not in {
            "https://github.com/keelapi/keel-setup",
            "https://github.com/keelapi/keel-setup.git",
            "git@github.com:keelapi/keel-setup.git",
            "ssh://git@github.com/keelapi/keel-setup.git",
        }:
            raise ValueError("unofficial bundle origin")
        if git("diff", "--cached", "--name-only", "--no-ext-diff", "--no-textconv", bundle_sha):
            raise ValueError("bundle index is not clean")

        def object_bytes(kind: str, oid: str) -> bytes:
            """Authenticate raw Git objects, including the commit and trees."""
            if re.fullmatch(r"[0-9a-f]{40}", oid) is None:
                raise ValueError("invalid object ID")
            size = int(git("cat-file", "-s", oid))
            if not 0 <= size <= 8 * 1024 * 1024:
                raise ValueError("bundle object exceeds verification limit")
            data = git("cat-file", kind, oid)
            header = f"{kind} {len(data)}\0".encode("ascii")
            if len(data) != size or hashlib.sha1(header + data).hexdigest() != oid:
                raise ValueError("Git object differs from its authenticated ID")
            return data

        commit = object_bytes("commit", bundle_sha)
        tree_line = commit.split(b"\n", 1)[0]
        if re.fullmatch(rb"tree [0-9a-f]{40}", tree_line) is None:
            raise ValueError("invalid pinned commit tree")
        files: dict[pathlib.PurePosixPath, bytes] = {}
        total_bytes = 0

        def tree_files(oid: str, parent: pathlib.PurePosixPath) -> None:
            """Walk only entries from independently authenticated tree objects."""
            nonlocal total_bytes
            if len(parent.parts) > 32:
                raise ValueError("bundle tree exceeds depth limit")
            data = object_bytes("tree", oid)
            offset = 0
            names: set[str] = set()
            while offset < len(data):
                end = data.index(b"\0", offset)
                mode, raw_name = data[offset:end].split(b" ", 1)
                name = raw_name.decode("utf-8")
                if (name in {"", ".", ".."} or name.lower() == ".git"
                        or "/" in name or name in names):
                    raise ValueError("unsafe or duplicate bundle entry")
                names.add(name)
                raw_oid = data[end + 1:end + 21]
                if len(raw_oid) != 20:
                    raise ValueError("truncated bundle tree")
                offset = end + 21
                relative = parent / name
                if mode == b"40000":
                    tree_files(raw_oid.hex(), relative)
                elif mode in {b"100644", b"100755"}:
                    if len(files) >= 4096:
                        raise ValueError("bundle exceeds file limit")
                    content = object_bytes("blob", raw_oid.hex())
                    total_bytes += len(content)
                    if total_bytes > 64 * 1024 * 1024:
                        raise ValueError("bundle exceeds byte limit")
                    files[relative] = content
                else:
                    raise ValueError("unsupported bundle entry")

        tree_files(tree_line[5:].decode("ascii"), pathlib.PurePosixPath())
        if not files:
            raise ValueError("empty bundle tree")

        # Include ignored additions and reject symlinked directories. Git metadata
        # alone is exempt, including the .git file used by linked worktrees.
        actual: set[pathlib.PurePosixPath] = set()
        def walk_error(error: OSError) -> None:
            """Fail closed when any part of the checkout cannot be enumerated."""
            raise error

        for current, directories, filenames in os.walk(bundle, followlinks=False, onerror=walk_error):
            directory = pathlib.Path(current)
            if directory == bundle:
                directories[:] = [name for name in directories if name != ".git"]
                filenames = [name for name in filenames if name != ".git"]
            for name in directories:
                if (directory / name).is_symlink():
                    raise ValueError("symlink in bundle directory")
            actual.update(pathlib.PurePosixPath((directory / name).relative_to(bundle).as_posix())
                          for name in filenames)
        if actual != set(files):
            raise ValueError("checkout files differ from the pinned allowlist")

        # Descriptor-relative traversal refuses parent symlinks. Nonblocking open
        # and fstat reject FIFOs/devices before reading, with bounded file reads.
        for relative, expected in files.items():
            directory_fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in relative.parts[:-1]:
                    child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directory_fd)
                    os.close(directory_fd)
                    directory_fd = child_fd
                fd = os.open(relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=directory_fd)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("bundle entry is not a regular file")
                    if stream.read(len(expected) + 1) != expected:
                        raise ValueError("bundle file differs from pinned bytes")
            finally:
                os.close(directory_fd)
    except Exception as exc:
        raise RuntimeError("pinned release verification failed") from exc


def verify_release(bundle: pathlib.Path, expected_sha: str) -> dict[str, Any]:
    """Verify public checkout identity, immutable revision, cleanliness, and bundle manifest."""

    if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
        raise PipelineFailure("untrusted_bundle", "bundle SHA must be 40 lowercase hexadecimal characters")
    if not bundle.is_dir():
        raise PipelineFailure("untrusted_bundle", "bundle path is not a directory")
    bundle = bundle.resolve()
    # Git discovers enclosing repositories; bind its facts to the directory
    # supplying executable bundle files before inspecting or running those files.
    worktree_root = pathlib.Path(_git(bundle, "rev-parse", "--show-toplevel", path_output=True)).resolve()
    if bundle != worktree_root:
        raise PipelineFailure("untrusted_bundle", "bundle path must be the Git worktree root")
    try:
        _verify_pinned_release(expected_sha, bundle=bundle)
    except RuntimeError as exc:
        raise PipelineFailure("untrusted_bundle", "pinned bundle byte authentication failed") from exc
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
    checked = _run([sys.executable, "-I", str(verifier)], cwd=bundle, timeout=20)
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


def bounded_walk(root: pathlib.Path):
    """Bound every enumerated entry, including files later skipped by callers."""
    started = time.monotonic()
    stack = [(root, 0)]
    entries_seen = directories_seen = 0
    while stack:
        current, depth = stack.pop()
        directories_seen += 1
        if depth > MAX_SCAN_DEPTH or directories_seen > MAX_SCAN_DIRECTORIES:
            raise PipelineFailure("model_review_required", "repository exceeds traversal limits")
        directories, filenames = [], []
        with os.scandir(current) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_SCAN_ENTRIES or time.monotonic() - started > MAX_SCAN_SECONDS:
                    raise PipelineFailure("model_review_required", "repository exceeds entry or elapsed-time limit")
                if entry.is_dir(follow_symlinks=False):
                    directories.append(entry.name)
                else:
                    filenames.append(entry.name)
        directories.sort()
        filenames.sort()
        yield str(current), directories, filenames
        stack.extend((current / name, depth + 1) for name in reversed(directories))


def classify_repository(repo: pathlib.Path) -> dict[str, Any]:
    """Return bounded repository facts without importing or executing application code."""

    revision = _git(repo, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise PipelineFailure("model_review_required", "repository has no immutable Git revision")

    instruction_names = {"AGENTS.md", "CLAUDE.md", ".cursorrules", "copilot-instructions.md"}
    instructions: list[str] = []
    copilot_instructions = repo / ".github" / "copilot-instructions.md"
    if copilot_instructions.is_file():
        instructions.append(".github/copilot-instructions.md")
    files_considered = 0
    for current, directories, filenames in bounded_walk(repo):
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

    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    if dirty:
        raise PipelineFailure(
            "dirty_checkout",
            "Keel will not modify existing uncommitted work automatically. Commit or stash it and retry, "
            "use a fresh disposable checkout, or explicitly request deeper model-driven inspection.",
        )


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
            raw = _read_repo_file(path, repo)
            if raw is None:
                raise PipelineFailure("model_review_required", "Python dependency manifest disappeared")
            content = raw.decode("utf-8")
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
    for current, directories, filenames in bounded_walk(repo):
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
                raw = _read_repo_file(path, repo)
                if raw is None:
                    raise PipelineFailure("model_review_required", "compatibility test disappeared")
                tree = ast.parse(raw.decode("utf-8"), filename=relative.as_posix())
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
        raw = _read_repo_file(path, repo)
        if raw is None:
            raise PipelineFailure("model_review_required", "selected source disappeared")
        source = raw.decode("utf-8")
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
    if set(keywords) != {"model", "instructions", "input"} or call.args or len(call.keywords) != 3:
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
    expected_constructor = ast.parse("if client is None:\n    from openai import OpenAI\n    client = OpenAI()\n").body[0]
    expected_mapping = ast.parse("summary = response.output_text.strip()\n").body[0]
    if (response_index != client_if_index + 1 or summary_index != response_index + 1
            or ast.dump(function.body[client_if_index]) != ast.dump(expected_constructor)
            or ast.dump(function.body[summary_index]) != ast.dump(expected_mapping)):
        raise PipelineFailure("model_review_required", "provider replacement contains additional or unrecognized logic")
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


def _keel_output_text(body: dict[str, Any]) -> str:
    output = body.get("output")
    if not isinstance(output, dict):
        raise RuntimeError("Keel returned an invalid normalized response.")
    content = output.get("content")
    if not isinstance(content, list) or not content:
        raise RuntimeError("Keel returned an invalid normalized response.")
    parts: list[str] = []
    for block in content:
        if (
            not isinstance(block, dict)
            or block.get("type") != "text"
            or block.get("role") != "assistant"
            or not isinstance(block.get("text"), str)
        ):
            raise RuntimeError("Keel returned an invalid normalized response.")
        parts.append(block["text"])
    return "".join(parts).strip()
'''


def generate_adapter(repo: pathlib.Path, seam: GoldenSeam) -> tuple[str, str | None]:
    path = repo.joinpath(*seam.path.parts)
    raw = _read_repo_file(path, repo)
    if raw is None:
        raise PipelineFailure("model_review_required", "selected source disappeared")
    source = raw.decode("utf-8")
    tree = ast.parse(source)
    reserved = {
        "KEEL_EXECUTE_URL", "KEEL_MAX_RESPONSE_BYTES", "KEEL_REQUEST_TIMEOUT_SECONDS",
        "_KeelNoRedirectHandler", "_keel_read_json", "_keel_output_text",
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
            "    summary = _keel_output_text(body)",
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
    import_block = "\nimport os\nfrom typing import Any\nimport json\nimport secrets\nimport time\nimport urllib.error\nimport urllib.request\n"
    lines.insert(import_line, import_block)
    application = "".join(lines)
    test_source = _generate_coupled_test(repo, seam) if seam.coupled_test else None
    return application, test_source


def _generate_coupled_test(repo: pathlib.Path, seam: GoldenSeam) -> str:
    assert seam.coupled_test is not None
    path = repo.joinpath(*seam.coupled_test.parts)
    raw = _read_repo_file(path, repo)
    if raw is None:
        raise PipelineFailure("model_review_required", "compatibility test disappeared")
    original = raw.decode("utf-8")
    # This whole-file migration is authorized only for the exact published
    # fixture. Any additional test, fixture, hook, or comment requires review.
    if hashlib.sha256(original.encode("utf-8")).hexdigest() != "bdb7e770dfe505e3efab6e6852afb70b88b38b2b6222cb451aacf25bef29592b":
        raise PipelineFailure("model_review_required", "coupled tests differ from the exact supported fixture")
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
    def __init__(self, text: str = "A short summary.") -> None:
        self.status = 200
        self._body = json.dumps({{
            "status": "completed",
            "governance": {{"decision": "allow"}},
            "output": {{
                "content": [{{
                    "type": "text",
                    "role": "assistant",
                    "text": text,
                }}],
            }},
        }}).encode()

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeClient:
    def __init__(self, text: str = "A short summary.") -> None:
        self.text = text
        self.calls: list[object] = []

    def open(self, request: object, *, timeout: int) -> FakeResponse:
        self.calls.append(request)
        return FakeResponse(self.text)


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
        ["git", "-c", "core.fsmonitor=false", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
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
        # Test collection imports target packages and application initialization.
        # A reduced environment and timeout do not isolate the host. Only compile
        # here; runtime compatibility requires isolated post-gate execution.
        try:
            compile(_read_repo_file(repo.joinpath(*seam.coupled_test.parts), repo) or b"",
                    seam.coupled_test.as_posix(), "exec")
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise PipelineFailure("validation_failed", "generated compatibility test does not compile") from exc
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
        "focused_compatibility_test_status": "deferred_requires_isolation" if focused_test else "not_applicable",
    }


@contextlib.contextmanager
def _mutation_parent(path: pathlib.Path, repo: pathlib.Path):
    relative = path.relative_to(repo)
    if not relative.parts or ".." in relative.parts:
        raise PipelineFailure("validation_failed", "mutation path escapes the repository")
    fd = os.open(repo.resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, relative.name
    finally:
        os.close(fd)


def _read_repo_file(path: pathlib.Path, repo: pathlib.Path) -> bytes | None:
    with _mutation_parent(path, repo) as (parent, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                raise PipelineFailure("validation_failed", "mutation target is not a bounded regular file")
            raw = handle.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES:
                raise PipelineFailure("validation_failed", "mutation target exceeds its byte limit")
            return raw


def _write(path: pathlib.Path, content: str, repo: pathlib.Path) -> None:
    with _mutation_parent(path, repo) as (parent, name):
        # Never truncate through an existing link or modify another hardlink.
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            mode = 0o644
        else:
            if not stat.S_ISREG(info.st_mode):
                raise PipelineFailure("validation_failed", "mutation target must be a nonsymlink regular file")
            mode = stat.S_IMODE(info.st_mode)
        temporary = f".keel-edit-{os.urandom(16).hex()}"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content.encode("utf-8"))
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def _ensure_state_ignored(repo: pathlib.Path) -> bool:
    checked = _run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), "check-ignore", "-q", ".keel/setup-state.json"], cwd=repo)
    if checked.returncode == 0:
        return False
    path = repo / ".gitignore"
    existing = (_read_repo_file(path, repo) or b"").decode("utf-8")
    suffix = "" if not existing or existing.endswith("\n") else "\n"
    _write(path, existing + suffix + ".keel/setup-state.json\n", repo)
    checked = _run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), "check-ignore", "-q", ".keel/setup-state.json"], cwd=repo)
    if checked.returncode != 0:
        raise PipelineFailure("validation_failed", "local setup state could not be git-ignored")
    return True


def _restore(repo: pathlib.Path, originals: dict[pathlib.PurePosixPath, bytes | None]) -> None:
    for relative, content in originals.items():
        path = repo.joinpath(*relative.parts)
        if content is None:
            with _mutation_parent(path, repo) as (parent, name):
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
        else:
            _write(path, content.decode("utf-8"), repo)
    state = repo / ".keel" / "setup-state.json"
    try:
        with _mutation_parent(state, repo) as (parent, name):
            os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass
    try:
        state.parent.rmdir()
    except OSError:
        pass


def _line_change_stats(before: str, after: str) -> tuple[int, int, int]:
    if len(before.encode()) > MAX_SOURCE_BYTES or len(after.encode()) > MAX_SOURCE_BYTES:
        raise PipelineFailure("model_review_required", "source comparison exceeds its byte limit")
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if len(before_lines) * len(after_lines) > 250_000:
        # Large repeated-line inputs use bounded linear multiset statistics.
        # These counts describe content changes, not line movement.
        removed = collections.Counter(before_lines) - collections.Counter(after_lines)
        added = collections.Counter(after_lines) - collections.Counter(before_lines)
        return sum(added.values()), sum(removed.values()), 0
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
    """Return metadata only; source and literal values stay in the local checkout."""

    stats: dict[str, dict[str, int]] = {}
    digests: dict[str, dict[str, str]] = {}
    whitespace_only = 0
    for relative_text in sorted(allowed_paths):
        relative = pathlib.PurePosixPath(relative_text)
        original = originals.get(relative)
        if relative not in originals:
            raise PipelineFailure("validation_failed", "diff evidence path was not captured before editing")
        try:
            before = original.decode("utf-8") if original is not None else ""
            raw = _read_repo_file(repo.joinpath(*relative.parts), repo)
            if raw is None:
                raise PipelineFailure("validation_failed", "diff evidence source disappeared")
            after = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PipelineFailure("validation_failed", "diff evidence is not bounded UTF-8 text") from exc
        insertions, deletions, whitespace = _line_change_stats(before, after)
        stats[relative_text] = {"insertions": insertions, "deletions": deletions}
        whitespace_only += whitespace
        digests[relative_text] = {"before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                                  "after_sha256": hashlib.sha256(after.encode()).hexdigest()}
    return {
        "per_file": stats,
        "digests": digests,
        "whitespace_only_changed_lines": whitespace_only,
        "human_review": "Inspect the changed files with local git diff before approval.",
    }


def run_pipeline(bundle: pathlib.Path, bundle_sha: str, repo: pathlib.Path) -> dict[str, Any]:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    originals: dict[pathlib.PurePosixPath, bytes | None] = {}
    edited = False
    try:
        bundle = bundle.resolve()
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
        originals[seam.path] = _read_repo_file(app_path, repo)
        if originals[seam.path] is None:
            raise PipelineFailure("validation_failed", "selected source disappeared")
        app_before = originals[seam.path].decode("utf-8")
        if seam.coupled_test:
            test_path = repo.joinpath(*seam.coupled_test.parts)
            originals[seam.coupled_test] = _read_repo_file(test_path, repo)
        ignore_relative = pathlib.PurePosixPath(".gitignore")
        ignore_path = repo / ".gitignore"
        originals[ignore_relative] = _read_repo_file(ignore_path, repo)

        mark = time.perf_counter()
        app_after, test_after = generate_adapter(repo, seam)
        _line_change_stats(app_before, app_after)
        if seam.coupled_test and test_after is not None:
            _line_change_stats(originals[seam.coupled_test].decode("utf-8"), test_after)
        edited = True
        _write(app_path, app_after, repo)
        if seam.coupled_test and test_after is not None:
            _write(repo.joinpath(*seam.coupled_test.parts), test_after, repo)
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
