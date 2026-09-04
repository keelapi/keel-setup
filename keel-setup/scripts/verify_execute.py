#!/usr/bin/env python3
"""Verify one deterministic /v1/execute allow/deny pair.

Standard library only. The execution key is read exclusively from KEEL_API_KEY
and is never included in output or accepted as an argument.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

DEFAULT_BASE_URL = "https://api.keelapi.com"
OUTPUT_FIELDS = (
    "model",
    "expectation",
    "request_id",
    "permit_id",
    "http_status",
    "body_status",
    "governance_decision",
    "error_stage",
    "error_code",
    "classification",
)

CORRELATION_HEADERS = {
    "request_id": "X-Keel-Request-ID",
    "permit_id": "X-Keel-Permit-ID",
}

MAX_RESPONSE_BYTES = 64 * 1024
_ULID_PATTERN = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}", re.IGNORECASE)
_SAFE_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_BODY_STATUSES = frozenset({"completed", "denied", "failed"})
_GOVERNANCE_DECISIONS = frozenset({"allow", "deny"})
_ERROR_STAGES = frozenset({"permit", "dispatch"})


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
    status = _allowlisted_scalar(body.get("status"), _BODY_STATUSES)
    decision = _allowlisted_scalar(governance.get("decision"), _GOVERNANCE_DECISIONS)
    stage = _allowlisted_scalar(error.get("stage"), _ERROR_STAGES)
    code = _safe_error_code(error.get("code"))

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


def _allowlisted_scalar(value: Any, allowed: frozenset[str]) -> str | None:
    """Return only a response scalar from a closed protocol vocabulary."""

    if not isinstance(value, str) or value not in allowed:
        return None
    return value


def _safe_error_code(value: Any) -> str | None:
    """Return one bounded protocol error code, never arbitrary response text."""

    if not isinstance(value, str) or _SAFE_ERROR_CODE_PATTERN.fullmatch(value) is None:
        return None
    return value


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
    response_headers: Any = None
    try:
        with _open(request, timeout=timeout) as response:
            http_status = response.status
            response_headers = response.headers
            raw = _read_bounded(response)
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        response_headers = exc.headers
        raw = _read_bounded(exc)
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
    correlation = {
        field: _correlation_header(response_headers, field=field, name=header)
        for field, header in CORRELATION_HEADERS.items()
    }
    result.update({"model": model, "expectation": expectation, **correlation})
    return {field: result.get(field) for field in OUTPUT_FIELDS}


def _read_bounded(response: Any) -> bytes:
    """Read one response body up to the verification helper's fixed ceiling."""

    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        return b""
    return raw


def _correlation_header(headers: Any, *, field: str, name: str) -> str | None:
    """Return only a correlation value in the format emitted by ``/v1/execute``."""

    if headers is None or not hasattr(headers, "get"):
        return None
    value = headers.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if field == "request_id":
        return value if _ULID_PATTERN.fullmatch(value) is not None else None
    if field != "permit_id":
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    canonical = str(parsed)
    return canonical if value.lower() == canonical else None


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
            "KEEL_API_KEY is not set. Install a Runtime key outside the model conversation, "
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
