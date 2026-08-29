#!/usr/bin/env python3
"""Verify one deterministic /v1/execute allow/deny pair.

Standard library only. The execution key is read exclusively from KEEL_API_KEY
and is never included in output or accepted as an argument.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://api.keelapi.com"
OUTPUT_FIELDS = (
    "model",
    "expectation",
    "http_status",
    "body_status",
    "governance_decision",
    "error_stage",
    "error_code",
    "classification",
)


def _nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("base URL must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("base URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise argparse.ArgumentTypeError("base URL must not contain a path prefix")
    try:
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("base URL has an invalid port") from exc
    if parsed.netloc.endswith(":"):
        raise argparse.ArgumentTypeError("base URL has an empty port")
    if parsed.scheme == "https":
        if parsed.hostname != "api.keelapi.com" or port not in {None, 443}:
            raise argparse.ArgumentTypeError("HTTPS base URL must be exactly the Keel API origin on default port 443")
    else:
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or port is None or port < 1024:
            raise argparse.ArgumentTypeError("plain HTTP requires an explicit loopback protocol-double port")
    return value.rstrip("/")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the execution credential through a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _open(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, type=_nonempty)
    parser.add_argument("--allow-model", required=True, type=_nonempty)
    parser.add_argument("--deny-model", required=True, type=_nonempty)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, type=_base_url)
    return parser.parse_args(argv)


def classify(http_status: int | None, body: dict[str, Any] | None) -> dict[str, Any]:
    body = body or {}
    governance = body.get("governance") if isinstance(body.get("governance"), dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    status = body.get("status") if isinstance(body.get("status"), str) else None
    decision = governance.get("decision") if isinstance(governance.get("decision"), str) else None
    stage = error.get("stage") if isinstance(error.get("stage"), str) else None
    code = error.get("code") if isinstance(error.get("code"), str) else None

    if http_status == 200 and status == "completed" and decision == "allow":
        result = "allowed_completed"
    elif http_status == 403 and status == "denied" and stage == "permit" and decision == "deny":
        result = "keel_denied"
    elif status == "failed" and stage == "dispatch" and decision == "allow":
        result = "provider_dispatch_failed_after_allow"
    elif http_status == 401 and code == "request_not_fresh":
        result = "freshness_failed"
    elif http_status == 409 and code == "nonce_reuse":
        result = "replay_rejected"
    elif http_status == 401 and code == "unauthorized":
        result = "client_authentication_failed"
    elif http_status is None:
        result = "transport_failed"
    elif not body:
        result = "malformed_response"
    else:
        result = "unexpected"
    return {
        "http_status": http_status,
        "body_status": status,
        "governance_decision": decision,
        "error_stage": stage,
        "error_code": code,
        "classification": result,
    }


def execute_attempt(
    *, base_url: str, key: str, provider: str, model: str, expectation: str, timeout: float = 10.0
) -> dict[str, Any]:
    # Freshness is intentionally created inside this function, immediately before this attempt.
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    payload = json.dumps(
        {
            "provider": provider,
            "model": model,
            "operation": "generate.text",
            "input": {"messages": [{"role": "user", "content": "Reply with OK."}]},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/execute",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Keel-Timestamp": timestamp,
            "X-Keel-Nonce": nonce,
        },
    )
    http_status: int | None
    raw: bytes
    try:
        with _open(request, timeout=timeout) as response:
            http_status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        http_status = None
        raw = b""

    body: dict[str, Any] | None = None
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                body = decoded
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
    result = classify(http_status, body)
    result.update({"model": model, "expectation": expectation})
    return {field: result.get(field) for field in OUTPUT_FIELDS}


def redact_record(record: dict[str, Any], secret: str) -> dict[str, Any]:
    """Remove the environment secret even if an upstream field unexpectedly echoes it."""
    redacted: dict[str, Any] = {}
    for field in OUTPUT_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and secret and secret in value:
            value = "[REDACTED]"
        redacted[field] = value
    return redacted


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    key = os.environ.get("KEEL_API_KEY")
    if not key:
        print(
            "KEEL_API_KEY is not set. Install a client-scoped key outside the model conversation, "
            "then rerun.",
            file=sys.stderr,
        )
        return 2

    results = [
        execute_attempt(
            base_url=args.base_url,
            key=key,
            provider=args.provider,
            model=args.allow_model,
            expectation="allow",
        ),
        execute_attempt(
            base_url=args.base_url,
            key=key,
            provider=args.provider,
            model=args.deny_model,
            expectation="deny",
        ),
    ]
    safe_results = [redact_record(result, key) for result in results]
    for safe_result in safe_results:
        print(json.dumps(safe_result, sort_keys=True, separators=(",", ":")))
    if [item["classification"] for item in results] == ["allowed_completed", "keel_denied"]:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
