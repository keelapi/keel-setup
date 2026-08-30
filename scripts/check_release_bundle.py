#!/usr/bin/env python3
"""Fail closed if the public setup bundle differs from its reviewed allowlist."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "ef6b92880f0729c336b3fad85e256135c91968da"
SOURCE_MERGE_COMMIT = "165c5f308bb339d8024f9fb1d66956e0940db2e7"
EXPECTED_FILES = {
    ".github/workflows/ci.yml",
    ".gitignore",
    "LICENSE",
    "README.md",
    "SHA256SUMS",
    "SOURCE.json",
    "keel-policy/SKILL.md",
    "keel-policy/examples/README.md",
    "keel-policy/examples/stripe-refund-approval-enterprise.json",
    "keel-policy/examples/stripe-refund-approval.json",
    "keel-policy/reference/enforceability-report.schema.json",
    "keel-policy/reference/field-provenance.json",
    "keel-policy/reference/fields.md",
    "keel-policy/reference/policy-document.schema.json",
    "keel-policy/scripts/validate_enforceability_report.py",
    "keel-policy/tests/test_validate_enforceability_report.py",
    "keel-setup/SKILL.md",
    "keel-setup/reference/coverage.schema.json",
    "keel-setup/reference/setup-state.schema.json",
    "keel-setup/reference/unified-execute-request.contract.json",
    "keel-setup/scripts/inventory.py",
    "keel-setup/scripts/setup_state.py",
    "keel-setup/scripts/verify_execute.py",
    "keel-setup/tests/test_inventory.py",
    "keel-setup/tests/test_execute_request_contract.py",
    "keel-setup/tests/test_setup_state.py",
    "keel-setup/tests/test_verify_execute.py",
    "scripts/check_release_bundle.py",
    "scripts/check_execute_contract.py",
    "shared/CONSTITUTION.md",
    "shared/feedback-report.schema.json",
    "shared/feedback-report.template.md",
    "shared/scripts/schema_validation.py",
    "shared/scripts/test_schema_validation.py",
    "shared/scripts/test_validate_feedback_report.py",
    "shared/scripts/validate_feedback_report.py",
    "tools/public_surface.json",
}
NETWORK_MODULES = {"http", "httpx", "requests", "socket", "urllib"}
NETWORK_RUNTIME_EXCEPTION = "keel-setup/scripts/verify_execute.py"


def _files() -> set[str]:
    result: set[str] = set()
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if ".pytest_cache" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in the release bundle: {path}")
        result.add(path.relative_to(ROOT).as_posix())
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_manifest() -> None:
    lines = (ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    paths: list[str] = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise ValueError(f"invalid SHA256SUMS row: {line!r}")
        digest, relative = match.groups()
        if relative == "SHA256SUMS":
            raise ValueError("SHA256SUMS must not hash itself")
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError(f"unsafe manifest path: {relative}")
        if _sha256(ROOT / relative) != digest:
            raise ValueError(f"digest mismatch: {relative}")
        paths.append(relative)
    expected = sorted(EXPECTED_FILES - {"SHA256SUMS"})
    if paths != expected:
        raise ValueError("SHA256SUMS paths do not equal the release allowlist")


def _check_runtime_network_boundary() -> None:
    roots = (ROOT / "keel-setup/scripts", ROOT / "keel-policy/scripts", ROOT / "shared/scripts")
    for scripts_root in roots:
        for path in scripts_root.glob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".", 1)[0])
            network = imported & NETWORK_MODULES
            if network and relative != NETWORK_RUNTIME_EXCEPTION:
                raise ValueError(f"unexpected network import in {relative}: {sorted(network)}")


def main() -> int:
    actual = _files()
    if actual != EXPECTED_FILES:
        missing = sorted(EXPECTED_FILES - actual)
        extra = sorted(actual - EXPECTED_FILES)
        raise ValueError(f"release allowlist mismatch: missing={missing}, extra={extra}")

    source = json.loads((ROOT / "SOURCE.json").read_text(encoding="utf-8"))
    if source.get("source_commit") != SOURCE_COMMIT:
        raise ValueError("SOURCE.json does not pin the reviewed source commit")
    if source.get("source_merge_commit") != SOURCE_MERGE_COMMIT:
        raise ValueError("SOURCE.json does not pin the merged source commit")
    if source.get("included_roots") != ["keel-policy", "keel-setup", "shared"]:
        raise ValueError("SOURCE.json included_roots changed")
    if source.get("included_files") != ["tools/public_surface.json"]:
        raise ValueError("SOURCE.json included_files changed")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for marker in ("# Keel setup", "[`keel-setup`](keel-setup/SKILL.md)", "[`keel-policy`](keel-policy/SKILL.md)"):
        if marker not in readme:
            raise ValueError(f"README missing release marker: {marker}")

    _check_manifest()
    _check_runtime_network_boundary()
    print(f"PASS: public setup bundle ({len(actual)} allowlisted files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
