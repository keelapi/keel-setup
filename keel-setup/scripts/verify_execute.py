#!/usr/bin/env python3
"""Verify one deterministic /v1/execute allow/deny pair.

Standard library only. The execution key is read either from ``KEEL_API_KEY``
or, in explicit interactive mode, from a no-echo terminal prompt. It is never
included in output or accepted as an argument.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import getpass
import hashlib
import stat
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
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
EXECUTE_TIMEOUT_SECONDS = 30.0
_ULID_PATTERN = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}", re.IGNORECASE)
_SAFE_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_BODY_STATUSES = frozenset({"completed", "denied", "failed"})
_GOVERNANCE_DECISIONS = frozenset({"allow", "deny"})
_ERROR_STAGES = frozenset({"permit", "dispatch"})
_SECRET_ARGUMENT_WORDS = frozenset({"key", "token", "secret", "credential", "password"})
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "provider",
        "allowed_model",
        "denied_model",
        "policy",
        "effective_policy_set_digest",
        "profile_digest",
        "generated_at",
    }
)
_PROFILE_POLICY_FIELDS = frozenset({"id", "version", "content_digest"})


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
    parser.add_argument("--provider", type=_nonempty)
    parser.add_argument("--allow-model", type=_nonempty)
    parser.add_argument("--deny-model", type=_nonempty)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, type=_base_url)
    parser.add_argument(
        "--hidden-input",
        action="store_true",
        help="prompt for the Runtime key without echo after verifying the pinned bundle",
    )
    parser.add_argument(
        "--bundle-sha",
        help="exact 40-character public bundle SHA; required with --hidden-input",
    )
    return parser.parse_args(argv)


def _contains_secret_argument(argv: list[str]) -> bool:
    for item in argv:
        option = item.split("=", 1)[0].lower()
        if option.startswith("--") and any(word in option for word in _SECRET_ARGUMENT_WORDS):
            return True
    return False


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


def _read_hidden_runtime_key() -> str:
    """Read one Runtime key from an interactive terminal without visible fallback."""

    if not sys.stdin.isatty():
        raise RuntimeError("interactive Runtime-key entry requires a TTY")
    try:
        with warnings.catch_warnings():
            # getpass warns immediately before its visible-input fallback. Converting that
            # warning to an exception prevents the fallback from reading any credential.
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Keel Runtime key: ", stream=sys.stderr)
    except getpass.GetPassWarning as exc:
        raise RuntimeError("terminal echo could not be disabled") from exc
    except (EOFError, OSError) as exc:
        raise RuntimeError("Runtime key could not be read securely from this terminal") from exc
    if not key:
        raise RuntimeError("Runtime key was not entered")
    return key


class VerificationProfileError(RuntimeError):
    """A bounded failure to obtain or validate non-secret proof configuration."""


def _fresh_headers(key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "X-Keel-Timestamp": str(int(time.time())),
        "X-Keel-Nonce": secrets.token_urlsafe(18),
        "Cache-Control": "no-store",
    }


def _parse_verification_profile(raw: bytes) -> dict[str, Any]:
    try:
        profile = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationProfileError("verification profile response was malformed") from exc
    if not isinstance(profile, dict) or set(profile) != _PROFILE_FIELDS:
        raise VerificationProfileError("verification profile response had an unexpected shape")
    if profile.get("schema_version") != 1 or profile.get("provider") != "openai":
        raise VerificationProfileError("verification profile response was unsupported")

    allowed_model = profile.get("allowed_model")
    denied_model = profile.get("denied_model")
    if (
        not isinstance(allowed_model, str)
        or _MODEL_PATTERN.fullmatch(allowed_model) is None
        or not isinstance(denied_model, str)
        or _MODEL_PATTERN.fullmatch(denied_model) is None
        or allowed_model == denied_model
    ):
        raise VerificationProfileError("verification profile model pair was invalid")

    policy = profile.get("policy")
    if not isinstance(policy, dict) or set(policy) != _PROFILE_POLICY_FIELDS:
        raise VerificationProfileError("verification profile policy binding was invalid")
    try:
        policy_id = uuid.UUID(policy.get("id"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise VerificationProfileError("verification profile policy binding was invalid") from exc
    if str(policy_id) != policy.get("id"):
        raise VerificationProfileError("verification profile policy binding was invalid")
    if not isinstance(policy.get("version"), int) or policy["version"] < 1:
        raise VerificationProfileError("verification profile policy binding was invalid")
    for digest in (
        policy.get("content_digest"),
        profile.get("effective_policy_set_digest"),
        profile.get("profile_digest"),
    ):
        if not isinstance(digest, str) or _DIGEST_PATTERN.fullmatch(digest) is None:
            raise VerificationProfileError("verification profile digest was invalid")
    generated_at = profile.get("generated_at")
    if not isinstance(generated_at, str) or len(generated_at) > 64:
        raise VerificationProfileError("verification profile timestamp was invalid")
    try:
        parsed_time = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationProfileError("verification profile timestamp was invalid") from exc
    if parsed_time.tzinfo is None:
        raise VerificationProfileError("verification profile timestamp was invalid")
    return profile


def fetch_verification_profile(
    *, base_url: str, key: str, timeout: float = 10.0
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}/v1/verification-profile",
        method="GET",
        headers=_fresh_headers(key),
    )
    try:
        with _open(request, timeout=timeout) as response:
            http_status = response.status
            raw = _read_bounded(response)
    except urllib.error.HTTPError as exc:
        code = None
        raw = _read_bounded(exc)
        if raw:
            try:
                decoded = json.loads(raw)
                error = decoded.get("error") if isinstance(decoded, dict) else None
                code = _safe_error_code(error.get("code")) if isinstance(error, dict) else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        suffix = f": {code}" if code else ""
        raise VerificationProfileError(
            f"verification profile unavailable (HTTP {exc.code}{suffix})"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VerificationProfileError("verification profile transport failed") from exc
    if http_status != 200 or not raw:
        raise VerificationProfileError("verification profile response was malformed")
    return _parse_verification_profile(raw)


def _profile_binding(profile: dict[str, Any]) -> tuple[Any, ...]:
    policy = profile["policy"]
    return (
        profile["profile_digest"],
        profile["provider"],
        profile["allowed_model"],
        profile["denied_model"],
        policy["id"],
        policy["version"],
        policy["content_digest"],
        profile["effective_policy_set_digest"],
    )


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
    *,
    base_url: str,
    key: str,
    provider: str,
    model: str,
    expectation: str,
    timeout: float = EXECUTE_TIMEOUT_SECONDS,
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
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _contains_secret_argument(raw_argv):
        print(
            "Runtime keys are never accepted in command-line arguments; use --hidden-input.",
            file=sys.stderr,
        )
        return 2
    try:
        args = parse_args(raw_argv)
    except SystemExit as exc:
        return int(exc.code)
    key: str | None = None
    try:
        if args.hidden_input:
            if args.provider or args.allow_model or args.deny_model:
                print(
                    "Hidden-input verification obtains its model pair from Keel; "
                    "do not pass provider or model selectors.",
                    file=sys.stderr,
                )
                return 2
            if not args.bundle_sha:
                print("--bundle-sha is required with --hidden-input.", file=sys.stderr)
                return 2
            try:
                _verify_pinned_release(args.bundle_sha)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print("Pinned Keel setup release verified.", file=sys.stderr)
            if args.base_url != DEFAULT_BASE_URL:
                print(f"Protocol double destination: {args.base_url}. Use a test-only credential.", file=sys.stderr)
                try:
                    confirmed = input("Type the exact destination URL to confirm: ")
                except (EOFError, OSError, KeyboardInterrupt):
                    confirmed = ""
                if confirmed != args.base_url:
                    print("Destination was not confirmed; no credential was read.", file=sys.stderr)
                    return 2
            try:
                key = _read_hidden_runtime_key()
            except (RuntimeError, KeyboardInterrupt) as exc:
                message = str(exc) if str(exc) else "Runtime-key entry was cancelled"
                print(message, file=sys.stderr)
                return 2
            try:
                initial_profile = fetch_verification_profile(
                    base_url=args.base_url,
                    key=key,
                )
            except VerificationProfileError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        else:
            if not (args.provider and args.allow_model and args.deny_model):
                print(
                    "--provider, --allow-model, and --deny-model are required in environment mode.",
                    file=sys.stderr,
                )
                return 2
            key = os.environ.get("KEEL_API_KEY")
            if not key:
                print(
                    "KEEL_API_KEY is not set. Install a Runtime key outside the model conversation, "
                    "then rerun.",
                    file=sys.stderr,
                )
                return 2

            initial_profile = {
                "provider": args.provider,
                "allowed_model": args.allow_model,
                "denied_model": args.deny_model,
            }

        results = [
            execute_attempt(
                base_url=args.base_url,
                key=key,
                provider=initial_profile["provider"],
                model=initial_profile["allowed_model"],
                expectation="allow",
            ),
            execute_attempt(
                base_url=args.base_url,
                key=key,
                provider=initial_profile["provider"],
                model=initial_profile["denied_model"],
                expectation="deny",
            ),
        ]
        safe_results = [redact_record(result, key) for result in results]
        if any(result["classification"] == "transport_failed" for result in safe_results):
            print(
                "Could not reach Keel. Check that this terminal has internet access, then try again.",
                file=sys.stderr,
            )
            print("Technical details: transport_failed", file=sys.stderr)
        for safe_result in safe_results:
            print(json.dumps(safe_result, sort_keys=True, separators=(",", ":")))
        if args.hidden_input:
            try:
                final_profile = fetch_verification_profile(
                    base_url=args.base_url,
                    key=key,
                )
            except VerificationProfileError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if _profile_binding(final_profile) != _profile_binding(initial_profile):
                print("Verification profile changed during proof; result invalid.", file=sys.stderr)
                return 1
        passed = [item["classification"] for item in results] == ["allowed_completed", "keel_denied"]
        if args.hidden_input:
            if passed:
                print("Allowed request: PASS")
                print("Blocked request: PASS")
            else:
                print("Allowed request: FAIL")
                print("Blocked request: FAIL")
        return 0 if passed else 1
    finally:
        # Python strings cannot be reliably zeroized. Drop our reference promptly and make no
        # stronger memory-erasure claim; the process exits immediately after this function.
        key = None


if __name__ == "__main__":
    raise SystemExit(main())
