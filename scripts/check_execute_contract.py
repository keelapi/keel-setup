#!/usr/bin/env python3
"""Verify the public helper against the pinned unified-execute request contract."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import pathlib
import re
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "keel-setup/reference/unified-execute-request.contract.json"
HELPER = ROOT / "keel-setup/scripts/verify_execute.py"
EXPECTED_CONTRACT_VERSION = "keel.public_unified_execute_request_contract.v1"
EXPECTED_API_REPOSITORY = "keelapi/keel-api"


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _helper_request_keys(path: pathlib.Path) -> set[str]:
    """Read the literal JSON payload keys from execute_attempt without importing it."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute_attempt"
    ]
    if len(functions) != 1:
        raise ValueError("verify_execute.py must define exactly one execute_attempt")

    payloads: list[ast.Dict] = []
    for node in ast.walk(functions[0]):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json"
            and node.func.attr == "dumps"
        ):
            continue
        if node.args and isinstance(node.args[0], ast.Dict):
            payloads.append(node.args[0])
    if len(payloads) != 1:
        raise ValueError("execute_attempt must JSON-encode exactly one literal request object")

    keys: set[str] = set()
    for key in payloads[0].keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise ValueError("execute request keys must be string literals")
        keys.add(key.value)
    return keys


def validate(
    *,
    contract_path: pathlib.Path = CONTRACT,
    helper_path: pathlib.Path = HELPER,
    openapi_path: pathlib.Path | None = None,
) -> None:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if contract.get("contract_schema_version") != EXPECTED_CONTRACT_VERSION:
        raise ValueError("unknown unified-execute contract snapshot version")
    if contract.get("source_repository") != EXPECTED_API_REPOSITORY:
        raise ValueError("unified-execute contract must name keelapi/keel-api")
    source_commit = contract.get("source_commit")
    if not isinstance(source_commit, str) or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ValueError("unified-execute contract must pin a 40-character API commit")

    schema = contract.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("unified-execute contract has no schema object")
    if _canonical_sha256(schema) != contract.get("schema_sha256"):
        raise ValueError("unified-execute schema digest mismatch")
    if schema.get("additionalProperties") is not False:
        raise ValueError("UnifiedExecuteRequest must continue to forbid extra fields")

    properties = schema.get("properties")
    required = schema.get("required")
    declared_helper_keys = contract.get("helper_request_keys")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError("unified-execute schema is missing properties or required")
    if not isinstance(declared_helper_keys, list) or not all(
        isinstance(item, str) for item in declared_helper_keys
    ):
        raise ValueError("helper_request_keys must be a list of strings")

    emitted = _helper_request_keys(helper_path)
    declared = set(declared_helper_keys)
    allowed = set(properties)
    required_keys = set(required)
    if emitted != declared:
        raise ValueError(
            f"helper request keys changed: emitted={sorted(emitted)} snapshot={sorted(declared)}"
        )
    if not emitted <= allowed:
        raise ValueError(f"helper emits fields forbidden by UnifiedExecuteRequest: {sorted(emitted - allowed)}")
    if not required_keys <= emitted:
        raise ValueError(f"helper omits required UnifiedExecuteRequest fields: {sorted(required_keys - emitted)}")
    if "operation" in emitted:
        raise ValueError("the unified execute helper must not send a top-level operation")

    if openapi_path is not None:
        openapi = json.loads(openapi_path.read_text(encoding="utf-8"))
        live_schema = openapi.get("components", {}).get("schemas", {}).get("UnifiedExecuteRequest")
        if live_schema != schema:
            raise ValueError("committed UnifiedExecuteRequest snapshot differs from supplied OpenAPI")
        if _file_sha256(openapi_path) != contract.get("source_artifact_sha256"):
            raise ValueError("supplied OpenAPI digest differs from the pinned API artifact")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--openapi",
        type=pathlib.Path,
        help="optional local keel-api OpenAPI artifact for cross-repository verification",
    )
    args = parser.parse_args(argv)
    validate(openapi_path=args.openapi)
    print("PASS: verify_execute request matches pinned UnifiedExecuteRequest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
