#!/usr/bin/env python3
"""Read, advance, and validate the local `.keel/setup-state.json` continuity file.

Standard library only. This helper holds local workflow state, not Keel
evidence: the invocation count records how often setup ran in this checkout and
never establishes that a prior run succeeded. The file is refused if it carries
a credential, an opaque bearer value, raw prompt or response text, or a local
claim of mapping authority.

Exit 0 = the state is usable. Exit 1 = the state was refused or invalid.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared" / "scripts"))
from schema_validation import validate as validate_schema  # noqa: E402

STATE_SCHEMA = ROOT / "keel-setup" / "reference" / "setup-state.schema.json"
DEFAULT_STATE_PATH = pathlib.PurePosixPath(".keel/setup-state.json")

SCHEMA_VERSION = "1.0"
MAX_STRING_LENGTH = 512
REQUIRED_PRE_GATE_CHECKS = (
    "syntax_or_compile",
    "fail_closed_integration_review",
    "adjacent_bypass_search",
)
LEGACY_PRE_GATE_CHECKS = (
    "syntax_or_compile",
    "module_load",
    "protocol_double",
    "adjacent_bypass_search",
)
ALLOWED_PRE_GATE_CHECKS = set(REQUIRED_PRE_GATE_CHECKS) | set(LEGACY_PRE_GATE_CHECKS)
INTEGRATION_SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs",
}
NON_PRODUCTION_PATH_PARTS = {
    "test", "tests", "__tests__", "spec", "specs", "docs", "examples", "example",
    "demo", "demos", "samples", "scripts", "benchmarks", "notebooks",
}

#: Cadence thresholds from the return loop. These are cadence, not authority:
#: reaching one never re-runs a completed human step.
RESUME_FROM_INVOCATION = 2
DRIFT_AUDIT_INTERVAL = 5
MAINTENANCE_REVIEW_INTERVAL = 20

#: Stages this revision can actually reach. The schema keeps the full stage
#: vocabulary so a state file written by a later revision still parses, but
#: state-F stages are reported unsupported rather than read as progress.
STATE_D_STAGES = (
    "discovery",
    "waiting_for_human",
    "integration_ready",
    "state_d_verified",
    "drifted",
    "blocked",
)

POST_FIRST_GATE_STAGES = (
    "integration_ready",
    "state_d_verified",
    "verified",
    "drifted",
)

#: Value shapes that must never reach a local state file. These match assignment
#: and bearer shapes rather than bare words, so an ordinary repository path such
#: as ``src/api_keys.py`` is still storable.
CREDENTIAL_SHAPES = (
    (re.compile(r"(?i)\bbearer\s+\S"), "carries a bearer value"),
    (
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?key|secret|password|token|cookie|jwt|csrf|credential)\b\s*[:=]\s*\S"
        ),
        "carries a credential assignment",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9_])(?:ks_|sk-|sk_|pk_|rk_|ghp_|gho_|xox[baprs]-)[A-Za-z0-9_\-]{8,}"
        ),
        "matches a known credential prefix",
    ),
)

DOES_NOT_ESTABLISH = (
    "prior_run_success",
    "deployment",
    "runtime_frequency",
    "trusted_semantics",
    "bypass_absence",
    "whole_application_protection",
    "independent_verification",
)


def _walk_strings(node: Any, path: str = "$"):
    if isinstance(node, dict):
        for name in sorted(node):
            yield from _walk_strings(node[name], f"{path}.{name}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_strings(item, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def refusals(state: Any) -> list[str]:
    """Return the reasons this state file must not be stored or trusted."""

    problems: list[str] = []
    if not isinstance(state, dict):
        return ["state must be a JSON object"]

    # A mapping-authority field is refused by name as well as by the schema, so
    # the reason is explicit rather than a bare unknown-property error.
    for name in sorted(state):
        lowered = name.lower()
        if "mapping" in lowered:
            problems.append(
                f"property {name!r} would record a local claim of mapping authority; "
                "mapping state is server-owned and out of scope for this revision"
            )
        if "prompt" in lowered or "response_body" in lowered or "output_text" in lowered:
            problems.append(f"property {name!r} would record raw prompt or response content")

    for location, value in _walk_strings(state):
        if len(value) > MAX_STRING_LENGTH:
            problems.append(
                f"{location} is longer than {MAX_STRING_LENGTH} characters; "
                "local state stores classifications, not prompt or response content"
            )
            continue
        for pattern, reason in CREDENTIAL_SHAPES:
            if pattern.search(value):
                problems.append(f"{location} {reason}")
                break
    return problems


def validate_state(state: Any) -> list[str]:
    """Schema, version, and refusal checks. Empty means the state is usable."""

    schema = json.loads(STATE_SCHEMA.read_text(encoding="utf-8"))
    failures = validate_schema(state, schema)
    failures.extend(refusals(state))
    if isinstance(state, dict):
        last_drift = state.get("last_drift_audit_invocation")
        last_maintenance = state.get("last_maintenance_review_invocation")
        count = state.get("invocation_count")
        if isinstance(count, int) and not isinstance(count, bool):
            for label, marker in (
                ("last_drift_audit_invocation", last_drift),
                ("last_maintenance_review_invocation", last_maintenance),
            ):
                if isinstance(marker, int) and not isinstance(marker, bool) and marker > count:
                    failures.append(f"{label} is ahead of invocation_count")
        changed_paths = state.get("changed_paths")
        if isinstance(changed_paths, list):
            for index, candidate in enumerate(changed_paths):
                if not isinstance(candidate, str):
                    continue
                parsed = pathlib.PurePosixPath(candidate)
                if parsed.is_absolute() or ".." in parsed.parts:
                    failures.append(f"changed_paths[{index}] must be repository-relative")
        if state.get("stage") == "waiting_for_human":
            if not isinstance(changed_paths, list) or not changed_paths:
                failures.append("waiting_for_human requires at least one changed path")
            pre_gate_checks = state.get("pre_gate_checks")
            if not isinstance(pre_gate_checks, dict):
                failures.append("waiting_for_human requires passed focused pre-gate checks")
            else:
                recorded_checks = pre_gate_checks.get("checks")
                if (
                    pre_gate_checks.get("status") != "passed"
                    or not isinstance(recorded_checks, list)
                    or any(not isinstance(item, str) for item in recorded_checks)
                    or (
                        not set(REQUIRED_PRE_GATE_CHECKS).issubset(recorded_checks)
                        and set(recorded_checks) != set(LEGACY_PRE_GATE_CHECKS)
                    )
                    or not set(recorded_checks).issubset(ALLOWED_PRE_GATE_CHECKS)
                ):
                    failures.append("waiting_for_human requires every focused pre-gate check to pass")
    return failures


def validate_prepared_paths(
    state: Any,
    repo_root: pathlib.Path,
    *,
    require_worktree_change: bool = False,
) -> list[str]:
    """Validate the local files underlying a resumable first-human-gate milestone."""

    if not isinstance(state, dict) or state.get("stage") != "waiting_for_human":
        return []
    changed_paths = state.get("changed_paths")
    if not isinstance(changed_paths, list) or not changed_paths:
        return ["waiting_for_human requires at least one changed path"]

    failures: list[str] = []
    root = repo_root.resolve()
    integration_marker_found = False
    for index, raw_path in enumerate(changed_paths):
        if not isinstance(raw_path, str):
            continue
        relative = pathlib.PurePosixPath(raw_path)
        if relative.is_absolute() or ".." in relative.parts or "\\" in raw_path:
            failures.append(f"changed_paths[{index}] must be repository-relative")
            continue
        candidate = root.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            failures.append(f"changed_paths[{index}] does not exist inside the repository")
            continue
        if candidate.is_symlink() or not resolved.is_file():
            failures.append(f"changed_paths[{index}] is not a regular repository file")
            continue
        if require_worktree_change:
            try:
                status = subprocess.run(
                    [
                        "git", "-C", str(root), "status", "--porcelain=v1",
                        "--untracked-files=all", "--", raw_path,
                    ],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                failures.append(f"changed_paths[{index}] working-tree status could not be established")
            else:
                if status.returncode != 0:
                    failures.append(f"changed_paths[{index}] working-tree status could not be established")
                elif not status.stdout:
                    failures.append(f"changed_paths[{index}] is not changed in the working tree")
        if resolved.suffix.lower() not in INTEGRATION_SOURCE_SUFFIXES:
            continue
        lowered_parts = {part.lower() for part in relative.parts}
        name = relative.name.lower()
        test_filename = (
            name.startswith(("test_", "test-", "spec."))
            or "_test." in name
            or ".test." in name
            or ".spec." in name
        )
        if lowered_parts & NON_PRODUCTION_PATH_PARTS or test_filename:
            continue
        try:
            integration_marker_found = b"/v1/execute" in resolved.read_bytes()
        except OSError:
            failures.append(f"changed_paths[{index}] could not be inspected")
        if integration_marker_found:
            break
    if not integration_marker_found:
        failures.append("changed paths do not contain a prepared /v1/execute integration")
    return failures


def due_reviews(
    invocation_count: int,
    last_drift_audit: int | None = None,
    last_maintenance_review: int | None = None,
    *,
    stage: str = "discovery",
) -> list[str]:
    """Return the cadence steps due at this invocation.

    Milestone state always wins over the number: this says what cadence has
    come around, never that an authority step may be repeated.
    """

    due: list[str] = []
    if invocation_count >= RESUME_FROM_INVOCATION:
        due.append("resume_earliest_unmet_milestone")
    # Count-based maintenance must never delay the first human-owned gate. The
    # milestone is authoritative for workflow ordering; the count is only a
    # cadence once local preparation has reached that gate.
    if stage not in POST_FIRST_GATE_STAGES:
        return due
    if invocation_count >= DRIFT_AUDIT_INTERVAL and (
        last_drift_audit is None or invocation_count - last_drift_audit >= DRIFT_AUDIT_INTERVAL
    ):
        due.append("drift_audit")
    if invocation_count >= MAINTENANCE_REVIEW_INTERVAL and (
        last_maintenance_review is None
        or invocation_count - last_maintenance_review >= MAINTENANCE_REVIEW_INTERVAL
    ):
        due.append("maintenance_review")
    return due


def next_phase(stage: str) -> str:
    """Return the earliest phase this revision can safely resume."""

    return {
        "discovery": "fast_first_run",
        "waiting_for_human": "first_human_gate",
        "integration_ready": "deterministic_verification",
        "state_d_verified": "deep_assurance",
        "verified": "maintenance",
        "drifted": "drift_audit",
        "blocked": "blocked",
    }.get(stage, "unsupported_stage")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def initial_state(*, updated_at: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "invocation_count": 1,
        "stage": "discovery",
        "updated_at": updated_at or _utc_now(),
    }


def is_git_ignored(repo_root: pathlib.Path, relative: pathlib.PurePosixPath) -> bool | None:
    """True/False when git answers, None when git cannot be consulted."""

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", str(relative)],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def write_state_atomically(state_path: pathlib.Path, state: dict[str, Any]) -> None:
    """Persist validated continuity without ever exposing a partial JSON file."""

    failures = validate_state(state)
    if failures:
        raise ValueError("refusing to persist invalid local setup state: " + "; ".join(failures))

    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_path.parent,
            prefix=f".{state_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = pathlib.Path(handle.name)
            os.chmod(temporary_path, 0o600)
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, state_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def begin(state_path: pathlib.Path, repo_root: pathlib.Path) -> dict[str, Any]:
    """Read, advance, and atomically persist one local invocation."""

    now = _utc_now()
    continuity = "resumed"
    continuity_reason: str | None = None
    problems: list[str] = []
    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError:
        state = initial_state(updated_at=now)
        continuity = "lost"
        continuity_reason = "no local state file was readable at this path"
    else:
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            state = initial_state(updated_at=now)
            continuity = "lost"
            continuity_reason = f"local state file is not valid JSON: {exc}"
        else:
            problems = validate_state(loaded)
            problems.extend(validate_prepared_paths(loaded, repo_root))
            if problems:
                state = initial_state(updated_at=now)
                continuity = "lost"
                continuity_reason = "local state file was refused; see refusals"
            else:
                state = dict(loaded)
                state["invocation_count"] = int(state["invocation_count"]) + 1
                state["updated_at"] = now

    stage = state.get("stage")
    relative = state_path
    try:
        relative = state_path.resolve().relative_to(repo_root.resolve())
    except (OSError, ValueError):
        relative = state_path
    ignored = is_git_ignored(repo_root, pathlib.PurePosixPath(pathlib.PurePath(relative).as_posix()))

    state_persisted = False
    persistence_error: str | None = None
    if ignored is True:
        try:
            write_state_atomically(state_path, state)
        except (OSError, ValueError) as exc:
            persistence_error = f"local state was not persisted: {exc}"
        else:
            state_persisted = True
    elif ignored is False:
        persistence_error = "local state path is not ignored by git"
    else:
        persistence_error = "could not establish that the local state path is ignored by git"

    return {
        "schema_version": SCHEMA_VERSION,
        "continuity": continuity,
        "continuity_reason": continuity_reason,
        "refusals": problems,
        "invocation_count": state["invocation_count"],
        "stage": stage,
        "next_phase": next_phase(str(stage)),
        "stage_supported_on_this_revision": stage in STATE_D_STAGES,
        "due": due_reviews(
            state["invocation_count"],
            state.get("last_drift_audit_invocation"),
            state.get("last_maintenance_review_invocation"),
            stage=str(stage),
        ),
        "state_path_git_ignored": ignored,
        "state_persisted": state_persisted,
        "persistence_error": persistence_error,
        "evidence_level": "unresolved",
        "does_not_establish": list(DOES_NOT_ESTABLISH),
    }


def mark_waiting_for_human(
    state_path: pathlib.Path,
    repo_root: pathlib.Path,
    *,
    provider: str,
    pinned_skill_ref: str,
    application_revision: str | None = None,
    changed_paths: list[str] | None = None,
    focused_checks: list[str] | None = None,
) -> list[str]:
    """Persist only the local-preparation milestone; never runtime or authority state."""

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"local state must be initialized before recording the human gate: {exc}"]
    failures = validate_state(state)
    if failures:
        return failures
    if state.get("stage") not in {"discovery", "waiting_for_human"}:
        return [f"cannot record the first human gate from stage {state.get('stage')!r}"]
    if not re.fullmatch(r"[0-9a-f]{40}", pinned_skill_ref):
        return ["pinned skill ref must be an immutable 40-character lowercase commit SHA"]
    if not changed_paths:
        return ["recording the first human gate requires at least one --changed-path"]
    recorded_check_set = set(focused_checks or [])
    checks_are_current = set(REQUIRED_PRE_GATE_CHECKS).issubset(recorded_check_set)
    checks_are_legacy = recorded_check_set == set(LEGACY_PRE_GATE_CHECKS)
    if not (checks_are_current or checks_are_legacy) or not recorded_check_set.issubset(
        ALLOWED_PRE_GATE_CHECKS
    ):
        return [
            "recording the first human gate requires all focused checks to pass: "
            + ", ".join(REQUIRED_PRE_GATE_CHECKS)
        ]

    relative = state_path
    try:
        relative = state_path.resolve().relative_to(repo_root.resolve())
    except (OSError, ValueError):
        pass
    ignored = is_git_ignored(
        repo_root,
        pathlib.PurePosixPath(pathlib.PurePath(relative).as_posix()),
    )
    if ignored is not True:
        return [
            "local state path is not ignored by git"
            if ignored is False
            else "could not establish that the local state path is ignored by git"
        ]

    updated = dict(state)
    recorded_paths = set(updated.get("changed_paths", []))
    recorded_paths.update(changed_paths or [])
    updated.update(
        {
            "stage": "waiting_for_human",
            "provider": provider,
            "pinned_skill_ref": pinned_skill_ref,
            "changed_paths": sorted(recorded_paths),
            "pre_gate_checks": {
                "status": "passed",
                "checks": sorted(set(focused_checks or [])),
                "checked_at": _utc_now(),
            },
            "updated_at": _utc_now(),
        }
    )
    if application_revision is not None:
        updated["application_revision"] = application_revision
    failures = validate_state(updated)
    failures.extend(validate_prepared_paths(updated, repo_root, require_worktree_change=True))
    if failures:
        return failures
    try:
        write_state_atomically(state_path, updated)
    except (OSError, ValueError) as exc:
        return [f"local state was not persisted: {exc}"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        type=pathlib.Path,
        default=pathlib.Path(str(DEFAULT_STATE_PATH)),
        help="path to the local, git-ignored continuity file",
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate an existing state file without incrementing the count",
    )
    parser.add_argument(
        "--mark-waiting-for-human",
        action="store_true",
        help="record that narrow local preparation reached the first human gate",
    )
    parser.add_argument("--provider", help="non-secret provider name for the prepared seam")
    parser.add_argument("--pinned-skill-ref", help="immutable public bundle commit SHA")
    parser.add_argument("--application-revision", help="non-secret inspected application revision")
    parser.add_argument("--changed-path", action="append", default=[], help="repository-relative changed path")
    parser.add_argument(
        "--focused-check",
        action="append",
        default=[],
        choices=REQUIRED_PRE_GATE_CHECKS,
        help="focused pre-gate check that passed; repeat for every required check",
    )
    args = parser.parse_args(argv)

    if args.validate_only and args.mark_waiting_for_human:
        parser.error("choose only one state operation")

    if args.mark_waiting_for_human:
        if not args.provider or not args.pinned_skill_ref:
            parser.error("--mark-waiting-for-human requires --provider and --pinned-skill-ref")
        failures = mark_waiting_for_human(
            args.state,
            args.repo_root,
            provider=args.provider,
            pinned_skill_ref=args.pinned_skill_ref,
            application_revision=args.application_revision,
            changed_paths=args.changed_path,
            focused_checks=args.focused_check,
        )
        if failures:
            for failure in failures:
                print(failure, file=sys.stderr)
            return 1
        print("recorded local preparation at first human gate")
        return 0

    if args.validate_only:
        try:
            state = json.loads(args.state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"unreadable local state: {exc}", file=sys.stderr)
            return 1
        failures = validate_state(state)
        failures.extend(validate_prepared_paths(state, args.repo_root))
        if failures:
            for failure in failures:
                print(failure, file=sys.stderr)
            return 1
        print("valid local setup state")
        return 0

    report = begin(args.state, args.repo_root)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["state_path_git_ignored"] is False:
        print(
            "local state path is not ignored by git; ignore it before writing continuity",
            file=sys.stderr,
        )
        return 1
    if not report["state_persisted"]:
        print(report["persistence_error"] or "local state was not persisted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
