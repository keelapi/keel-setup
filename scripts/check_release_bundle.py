#!/usr/bin/env python3
"""Fail closed if the public setup bundle differs from its reviewed allowlist."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_RELEASE_VERSION = "2026-09-03.2"
PRODUCT_SOURCE_SHA256 = "c281a5d173588c99e7b79a31d24cbfbe17779e2d50a8fc3d66ae533310d933ca"
PUBLICATION_LAYER_FILES = [
    ".github/workflows/ci.yml",
    ".gitignore",
    "README.md",
    "SHA256SUMS",
    "SOURCE.json",
    "scripts/check_execute_contract.py",
    "scripts/check_release_bundle.py",
]
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
    "keel-setup/scripts/fast_first_run.py",
    "keel-setup/scripts/inventory.py",
    "keel-setup/scripts/setup_state.py",
    "keel-setup/scripts/verify_execute.py",
    "keel-setup/tests/test_inventory.py",
    "keel-setup/tests/test_execute_request_contract.py",
    "keel-setup/tests/fixtures/fast_first_run_cases.py",
    "keel-setup/tests/test_fast_first_run.py",
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


def _product_source_sha256(exemptions: set[str]) -> str:
    digest = hashlib.sha256()
    paths = sorted(EXPECTED_FILES - exemptions)
    for relative in paths:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256((ROOT / relative).read_bytes()).digest())
    return digest.hexdigest()


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

    source_raw = (ROOT / "SOURCE.json").read_text(encoding="utf-8")
    source = json.loads(source_raw)
    if source.get("public_release_version") != PUBLIC_RELEASE_VERSION:
        raise ValueError("SOURCE.json does not identify the reviewed public release version")
    if source.get("product_source_sha256") != PRODUCT_SOURCE_SHA256:
        raise ValueError("SOURCE.json does not pin the reviewed public product digest")
    if {"source_repository", "source_commit", "source_merge_commit"} & set(source):
        raise ValueError("SOURCE.json exposes private source provenance")
    private_source_name = "keel-" + "skills"
    if private_source_name in source_raw:
        raise ValueError("SOURCE.json names the private source repository")
    if source.get("included_roots") != ["keel-policy", "keel-setup", "shared"]:
        raise ValueError("SOURCE.json included_roots changed")
    if source.get("included_files") != ["tools/public_surface.json"]:
        raise ValueError("SOURCE.json included_files changed")
    if source.get("publication_layer_files") != PUBLICATION_LAYER_FILES:
        raise ValueError("SOURCE.json publication-layer exemption changed")
    computed_product_digest = _product_source_sha256(set(PUBLICATION_LAYER_FILES))
    if computed_product_digest != PRODUCT_SOURCE_SHA256:
        raise ValueError("public product files differ from the immutable SOURCE.json commitment")

    manifest_raw = (ROOT / "tools/public_surface.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_raw)
    if set(manifest) != {"_comment", "actions", "requirements", "fields", "field_adjudications"}:
        raise ValueError("public surface manifest is not the positive allowlist shape")
    for section in ("actions", "requirements", "fields"):
        if set(manifest.get(section, {})) != {"PUBLIC"}:
            raise ValueError(f"public surface {section} exposes non-public buckets")
    if private_source_name in manifest_raw:
        raise ValueError("public surface manifest names the private source repository")

    fields_raw = (ROOT / "keel-policy/reference/fields.md").read_text(encoding="utf-8")
    if "keel-" + "api" in fields_raw or "app/" + "services/" in fields_raw:
        raise ValueError("fields.md exposes a private repository or source path")

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
