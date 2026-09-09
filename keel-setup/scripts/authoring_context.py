#!/usr/bin/env python3
"""Retrieve Keel's non-secret authoring context for one Runtime key's project.

Standard library only. The Runtime key is read from a no-echo terminal prompt
after the pinned public release is verified. It is never accepted as an
argument, read from the environment, written to a file, or printed.

This helper is transport, not judgement. It performs one bounded read-only
``GET /v1/authoring-context?view=operations``, validates the exact closed response schema, and
prints the server's answer unchanged. It does not choose, rank, filter, or
reinterpret models: deciding which model suits an application requires that
application's intent, which this process does not have and must not invent.

In particular it never treats a recorded rate of zero as cheap, an absent rate
as free, ``routable_in_policies`` as fitness for a caller's use, or a low rate
as a recommendation. ``pricing_quality``, ``pricing_asof``, ``lifecycle_status``
and ``truncated`` are carried through exactly so the consumer keeps the
uncertainty Keel actually recorded.
"""
from __future__ import annotations

import argparse
import decimal
import getpass
import importlib.util
import json
import pathlib
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Any

DEFAULT_BASE_URL = "https://api.keelapi.com"

#: The response bound this helper enforces. It mirrors the route's own ceiling;
#: a larger body is refused rather than truncated locally, because a locally cut
#: response would misreport ``model_count`` and ``truncated``.
MAX_RESPONSE_BYTES = 64 * 1024

#: The largest model list schema version 2 may carry.
MAX_MODELS = 200

#: A sane ceiling on the server's reported total, so a corrupt count cannot
#: describe an unbounded catalog.
MAX_MODEL_COUNT = 1_000_000

AUTHORING_CONTEXT_TIMEOUT_SECONDS = 15.0
SCHEMA_VERSION = 2

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "authoring_level",
        "pricing_asof",
        "model_count",
        "truncated",
        "models",
    }
)
_MODEL_FIELDS = frozenset(
    {
        "provider",
        "model_id",
        "display_name",
        "lifecycle_status",
        "routable_in_policies",
        "prompt_per_1k_usd",
        "completion_per_1k_usd",
        "pricing_quality",
        "operations",
    }
)

#: Closed vocabularies. Both carry decision semantics for the consumer, so an
#: unrecognised value is refused rather than passed through as if understood.
_AUTHORING_LEVELS = frozenset({"template", "basic", "full"})
_PRICING_QUALITIES = frozenset({"authoritative", "approximate", "placeholder", "unknown"})

#: Published schema-2 operation vocabulary. Exact data values, never commands.
#: Unknown values require an explicitly reviewed helper release; no guessing,
#: normalisation, filtering, ranking, or fallback to the v1 response.
_OPERATIONS = frozenset(
    {
        "generate.text",
        "embed.text",
        "generate.image",
        "edit.image",
        "understand.image",
        "generate.audio",
        "transcribe.audio",
        "generate.video",
        "understand.video",
        "realtime.session",
        "call.outbound",
        "call.respond",
        "message.send",
        "calendar.event.create",
        "travel.air.search",
        "travel.air.price",
        "travel.air.book",
        "travel.air.manage",
        "travel.lodging.search",
        "travel.lodging.book",
        "travel.lodging.manage",
        "travel.car.search",
        "travel.car.book",
        "travel.car.manage",
        "travel.rail.search",
        "travel.rail.book",
        "travel.rail.manage",
        "travel.order.get",
        "travel.order.create",
        "travel.order.change",
        "travel.order.cancel",
        "travel.ticket.issue",
        "travel.ticket.void",
        "travel.exchange",
        "travel.refund",
        "travel.seatmap.get",
        "travel.seat.select",
        "travel.profile.read",
        "travel.profile.write",
        "travel.queue.read",
        "travel.queue.write",
        "travel.trip.read",
        "travel.trip.sync",
        "computer.use",
        "browser.action",
        "code_execution",
        "call.tools",
        "run.batch",
        "run.async",
        "cost_permit.authorize",
        "payment.execute",
    }
)

#: Descriptive identifiers. These are bounded, not allowlisted: a new provider
#: or lifecycle value is Keel's to introduce, and refusing it would make this
#: helper a gate on the catalog rather than a transport for it.
_PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}")
_LIFECYCLE_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_RATE_PATTERN = re.compile(r"[0-9]{1,10}(?:\.[0-9]{1,12})?")
_MAX_DISPLAY_NAME = 128

_SAFE_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_SECRET_ARGUMENT_WORDS = frozenset({"key", "token", "secret", "credential", "password"})

#: Bounded, human-readable causes for the failures this helper must fail closed
#: on. The protocol code is reported alongside, never in place of, the action.
_ERROR_GUIDANCE = {
    "unauthorized": "That Runtime key was not accepted. Check it is a Runtime key and not expired.",
    "request_not_fresh": "This computer's clock is too far from Keel's. Correct the system time and retry.",
    "nonce_reuse": "Keel rejected a repeated request identifier. Run the command again.",
}


class AuthoringContextError(RuntimeError):
    """A bounded failure to obtain or validate the non-secret authoring context."""


def _nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _base_url(value: str) -> str:
    """Accept only the Keel origin, or an explicit loopback protocol double.

    Deliberately duplicated from ``verify_execute.py`` rather than imported. The
    two helpers are separate credential-custody paths, and a change to one must
    not silently alter where the other may send a Runtime key.
    """

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
            raise argparse.ArgumentTypeError(
                "HTTPS base URL must be exactly the Keel API origin on default port 443"
            )
    else:
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or port is None or port < 1024:
            raise argparse.ArgumentTypeError(
                "plain HTTP requires an explicit loopback protocol-double port"
            )
    return value.rstrip("/")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the Runtime key through a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _open(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, type=_base_url)
    parser.add_argument(
        "--bundle-sha",
        required=True,
        type=_nonempty,
        help="exact 40-character public bundle SHA, verified before the prompt",
    )
    return parser.parse_args(argv)


def _contains_secret_argument(argv: list[str]) -> bool:
    for item in argv:
        option = item.split("=", 1)[0].lower()
        if option.startswith("--") and any(word in option for word in _SECRET_ARGUMENT_WORDS):
            return True
    return False


def _verify_pinned_release(bundle_sha: str) -> None:
    """Run the existing public release verifier before accepting a credential."""

    try:
        bundle = pathlib.Path(__file__).resolve().parents[2]
        helper_path = bundle / "keel-setup" / "scripts" / "fast_first_run.py"
        spec = importlib.util.spec_from_file_location("keel_release_verifier", helper_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("pinned release verifier is unavailable")
        module = importlib.util.module_from_spec(spec)
        previous_module = sys.modules.get(spec.name)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            if previous_module is None:
                sys.modules.pop(spec.name, None)
            else:
                sys.modules[spec.name] = previous_module
        module.verify_release(bundle, bundle_sha)
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
            entered = getpass.getpass("Keel Runtime key: ", stream=sys.stderr)
    except getpass.GetPassWarning as exc:
        raise RuntimeError("terminal echo could not be disabled") from exc
    except (EOFError, OSError) as exc:
        raise RuntimeError("Runtime key could not be read securely from this terminal") from exc
    if not entered:
        raise RuntimeError("Runtime key was not entered")
    return entered


def _fresh_headers(credential: str) -> dict[str, str]:
    # Freshness is created immediately before the single request this helper makes.
    return {
        "Authorization": f"Bearer {credential}",
        "Accept": "application/json",
        "X-Keel-Timestamp": str(int(time.time())),
        "X-Keel-Nonce": secrets.token_urlsafe(18),
        "Cache-Control": "no-store",
    }


def _read_bounded(response: Any) -> bytes:
    """Read one response body, refusing anything past the fixed ceiling."""

    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AuthoringContextError("authoring context response exceeded the 64 KiB bound")
    return raw


def _safe_error_code(value: Any) -> str | None:
    """Return one bounded protocol error code, never arbitrary response text."""

    if not isinstance(value, str) or _SAFE_ERROR_CODE_PATTERN.fullmatch(value) is None:
        return None
    return value


def _error_code_of(raw: bytes) -> str | None:
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    error = decoded.get("error") if isinstance(decoded, dict) else None
    return _safe_error_code(error.get("code")) if isinstance(error, dict) else None


def _bounded_string(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _rate(value: Any, field: str) -> str | None:
    """Return the recorded rate exactly as Keel wrote it, or ``None``.

    The original string is preserved rather than normalised. ``"0.00000"`` and
    an absent rate mean opposite things to a consumer deciding what is cheap,
    and reformatting would blur that distinction.
    """

    if value is None:
        return None
    if not _bounded_string(value, _RATE_PATTERN):
        raise AuthoringContextError(f"authoring context {field} was not a recorded decimal rate")
    try:
        decimal.Decimal(value)
    except decimal.InvalidOperation as exc:
        raise AuthoringContextError(f"authoring context {field} was not a recorded decimal rate") from exc
    return value


def _parse_model(entry: Any, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != _MODEL_FIELDS:
        raise AuthoringContextError(f"authoring context model {index} had an unexpected shape")
    provider = entry["provider"]
    model_id = entry["model_id"]
    display_name = entry["display_name"]
    lifecycle_status = entry["lifecycle_status"]
    routable = entry["routable_in_policies"]
    quality = entry["pricing_quality"]
    operations = entry["operations"]

    if not _bounded_string(provider, _PROVIDER_PATTERN):
        raise AuthoringContextError(f"authoring context model {index} had an invalid provider")
    if not _bounded_string(model_id, _MODEL_ID_PATTERN):
        raise AuthoringContextError(f"authoring context model {index} had an invalid model id")
    # Bounded and printable: this block is pasted into a coding agent, so a
    # display name may not carry control characters, bidirectional overrides, or
    # unbounded text.
    if (
        not isinstance(display_name, str)
        or not 1 <= len(display_name) <= _MAX_DISPLAY_NAME
        or not display_name.isprintable()
    ):
        raise AuthoringContextError(f"authoring context model {index} had an invalid display name")
    if not _bounded_string(lifecycle_status, _LIFECYCLE_PATTERN):
        raise AuthoringContextError(f"authoring context model {index} had an invalid lifecycle status")
    if not isinstance(routable, bool):
        raise AuthoringContextError(f"authoring context model {index} had an invalid routable flag")
    if quality not in _PRICING_QUALITIES:
        raise AuthoringContextError(f"authoring context model {index} had an unrecognised pricing quality")

    if (
        not isinstance(operations, list)
        or not 1 <= len(operations) <= len(_OPERATIONS)
        or any(not isinstance(value, str) or value not in _OPERATIONS for value in operations)
        or len(set(operations)) != len(operations)
    ):
        raise AuthoringContextError(f"authoring context model {index} had invalid operations")

    return {
        "provider": provider,
        "model_id": model_id,
        "display_name": display_name,
        "lifecycle_status": lifecycle_status,
        "routable_in_policies": routable,
        "prompt_per_1k_usd": _rate(entry["prompt_per_1k_usd"], f"model {index} prompt rate"),
        "completion_per_1k_usd": _rate(entry["completion_per_1k_usd"], f"model {index} completion rate"),
        "pricing_quality": quality,
        "operations": operations[:],
    }


def parse_authoring_context(raw: bytes) -> dict[str, Any]:
    """Validate the exact closed schema and return it with server order intact."""

    if len(raw) > MAX_RESPONSE_BYTES:
        raise AuthoringContextError("authoring context response exceeded the 64 KiB bound")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthoringContextError("authoring context response was malformed") from exc
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        raise AuthoringContextError("authoring context response had an unexpected shape")
    if not _is_int(payload["schema_version"]) or payload["schema_version"] != SCHEMA_VERSION:
        raise AuthoringContextError("authoring context schema version 2 is required; this helper cannot fall back to v1")
    if payload["authoring_level"] not in _AUTHORING_LEVELS:
        raise AuthoringContextError("authoring context authoring level was unrecognised")
    # Schema version 2 declares this null. A real timestamp would be a different
    # contract, and silently passing one through would let a consumer describe
    # prices as current when this helper cannot establish that.
    if payload["pricing_asof"] is not None:
        raise AuthoringContextError("authoring context carried a pricing timestamp this schema does not define")

    truncated = payload["truncated"]
    model_count = payload["model_count"]
    models = payload["models"]
    if not isinstance(truncated, bool):
        raise AuthoringContextError("authoring context truncation flag was invalid")
    if not _is_int(model_count) or not 0 <= model_count <= MAX_MODEL_COUNT:
        raise AuthoringContextError("authoring context model count was invalid")
    if not isinstance(models, list):
        raise AuthoringContextError("authoring context model list was invalid")
    if len(models) > MAX_MODELS:
        raise AuthoringContextError(f"authoring context returned more than {MAX_MODELS} models")

    parsed = [_parse_model(entry, index) for index, entry in enumerate(models)]
    identities = {(model["provider"], model["model_id"]) for model in parsed}
    if len(identities) != len(parsed):
        raise AuthoringContextError("authoring context listed the same model more than once")

    # ``truncated`` is the consumer's only signal that the catalog is larger than
    # the list, so it must agree with the count rather than be taken on trust.
    if truncated:
        if len(parsed) >= model_count:
            raise AuthoringContextError("authoring context claimed truncation but returned the full count")
    elif len(parsed) != model_count:
        raise AuthoringContextError("authoring context model count disagrees with the models returned")

    return {
        "schema_version": SCHEMA_VERSION,
        "authoring_level": payload["authoring_level"],
        "pricing_asof": None,
        "model_count": model_count,
        "truncated": truncated,
        "models": parsed,
    }


def fetch_authoring_context(
    *,
    base_url: str,
    credential: str,
    timeout: float = AUTHORING_CONTEXT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Perform the one bounded read-only request this helper is allowed to make."""

    request = urllib.request.Request(
        f"{base_url}/v1/authoring-context?view=operations",
        method="GET",
        headers=_fresh_headers(credential),
    )
    try:
        with _open(request, timeout=timeout) as response:
            http_status = response.status
            raw = _read_bounded(response)
    except urllib.error.HTTPError as exc:
        code = _error_code_of(_read_bounded(exc))
        guidance = _ERROR_GUIDANCE.get(code or "")
        detail = f" ({code})" if code else ""
        if guidance:
            raise AuthoringContextError(f"{guidance}{detail}") from None
        raise AuthoringContextError(
            f"Keel did not return authoring context v2 (HTTP {exc.code}{detail})."
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuthoringContextError(
            "Could not reach Keel. Check that this terminal has internet access, then try again."
        ) from exc
    if http_status != 200 or not raw:
        raise AuthoringContextError("authoring context response was malformed")
    return parse_authoring_context(raw)


def render_block(context: dict[str, Any]) -> str:
    """Serialise the validated context as one deterministic pasteable line."""

    block = json.dumps(context, sort_keys=True, separators=(",", ":"))
    if len(block.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise AuthoringContextError("authoring context block exceeded the 64 KiB bound")
    return block


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _contains_secret_argument(raw_argv):
        print(
            "Runtime keys are never accepted in command-line arguments; this helper prompts for one.",
            file=sys.stderr,
        )
        return 2
    try:
        args = parse_args(raw_argv)
    except SystemExit as exc:
        return int(exc.code)

    credential: str | None = None
    try:
        try:
            _verify_pinned_release(args.bundle_sha)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("Pinned Keel setup release verified.", file=sys.stderr)
        try:
            credential = _read_hidden_runtime_key()
        except (RuntimeError, KeyboardInterrupt) as exc:
            print(str(exc) or "Runtime-key entry was cancelled", file=sys.stderr)
            return 2

        try:
            context = fetch_authoring_context(base_url=args.base_url, credential=credential)
            block = render_block(context)
        except AuthoringContextError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        # Structural, not cosmetic: every field above is rebuilt from a validated
        # value, so no response text can reach this block unchecked. The check
        # fails closed rather than redacting, because a key here would mean the
        # validated schema itself had been violated.
        if credential in block:
            print("Refusing to print a block that contains the Runtime key.", file=sys.stderr)
            return 1

        print(block)
        summary = (
            f"Authoring context retrieved: {context['model_count']} model(s), "
            f"authoring level {context['authoring_level']}, "
            f"truncated={'yes' if context['truncated'] else 'no'}."
        )
        print(summary, file=sys.stderr)
        print(
            "This block is Keel's own record. It is not a recommendation, and a recorded rate of "
            "zero is not a claim that a model is free.",
            file=sys.stderr,
        )
        return 0
    finally:
        # Python strings cannot be reliably zeroized. Drop our reference promptly and make no
        # stronger memory-erasure claim; the process exits immediately after this function.
        credential = None


if __name__ == "__main__":
    raise SystemExit(main())
