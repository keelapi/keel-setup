from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "verify_execute.py"
SPEC = importlib.util.spec_from_file_location("verify_execute", SCRIPT)
verify_execute = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(verify_execute)


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    responses: list[tuple[int, object] | tuple[int, object, dict[str, str]]] = []

    def _respond(self, body):
        type(self).requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        queued = type(self).responses.pop(0)
        status, response = queued[:2]
        extra_headers = queued[2] if len(queued) == 3 else {}
        raw = response if isinstance(response, bytes) else json.dumps(response).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        request_number = len(type(self).requests)
        default_headers = {
            "X-Keel-Request-ID": f"01K4{request_number:022d}",
            "X-Keel-Permit-ID": str(uuid.UUID(int=request_number)),
        }
        for name, value in {**default_headers, **extra_headers}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._respond(None)

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self._respond(body)

    def log_message(self, format, *args):  # noqa: A002
        return


class _InteractiveInput(io.StringIO):
    def isatty(self):
        return True


class _NonInteractiveInput(io.StringIO):
    def isatty(self):
        return False


@contextlib.contextmanager
def server(responses):
    _Handler.requests = []
    _Handler.responses = list(responses)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def verification_profile(*, digest: str = "a" * 64):
    return {
        "schema_version": 1,
        "provider": "openai",
        "allowed_model": "gpt-4o-mini",
        "denied_model": "o4-mini",
        "policy": {
            "id": "00000000-0000-0000-0000-000000000001",
            "version": 1,
            "content_digest": f"sha256:{'b' * 64}",
        },
        "effective_policy_set_digest": f"sha256:{'c' * 64}",
        "profile_digest": f"sha256:{digest}",
        "generated_at": "2026-09-04T12:00:00+00:00",
    }


class ClassifierTest(unittest.TestCase):
    def test_http_403_requires_full_tuple(self):
        denied = verify_execute.classify(
            403, {"status": "denied", "governance": {"decision": "deny"}, "error": {"stage": "permit"}}
        )
        provider = verify_execute.classify(
            403, {"status": "failed", "governance": {"decision": "allow"}, "error": {"stage": "dispatch"}}
        )
        bare = verify_execute.classify(403, {})
        self.assertEqual(denied["classification"], "keel_denied")
        self.assertEqual(provider["classification"], "provider_dispatch_failed_after_allow")
        self.assertEqual(bare["classification"], "malformed_response")

    def test_freshness_replay_auth_and_malformed(self):
        self.assertEqual(verify_execute.classify(401, {"error": {"code": "request_not_fresh"}})["classification"], "freshness_failed")
        self.assertEqual(verify_execute.classify(409, {"error": {"code": "nonce_reuse"}})["classification"], "replay_rejected")
        self.assertEqual(verify_execute.classify(401, {"error": {"code": "unauthorized"}})["classification"], "client_authentication_failed")
        self.assertEqual(verify_execute.classify(500, None)["classification"], "malformed_response")
        self.assertEqual(verify_execute.classify(None, None)["classification"], "transport_failed")


class ProtocolDoubleTest(unittest.TestCase):
    def test_expected_pair_uses_unique_freshness_and_messages_without_leak(self):
        allow = {"status": "completed", "governance": {"decision": "allow"}, "output": {"sensitive": "not printed"}}
        deny = {"status": "denied", "governance": {"decision": "deny"}, "error": {"stage": "permit", "code": "policy.rule_denied"}}
        sentinel = "unit-test-value-that-must-stay-redacted"
        with server([(200, allow), (403, deny)]) as base_url:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"KEEL_API_KEY": sentinel}, clear=False), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = verify_execute.main(["--provider", "test", "--allow-model", "small", "--deny-model", "large", "--base-url", base_url])
        self.assertEqual(code, 0)
        self.assertNotIn(sentinel, out.getvalue() + err.getvalue())
        self.assertNotIn("sensitive", out.getvalue())
        self.assertEqual([item["path"] for item in _Handler.requests], ["/v1/execute", "/v1/execute"])
        nonces = [item["headers"]["X-Keel-Nonce"] for item in _Handler.requests]
        self.assertEqual(len(set(nonces)), 2)
        self.assertTrue(all(len(item) >= 16 for item in nonces))
        self.assertTrue(all(item["headers"]["X-Keel-Timestamp"].isdigit() for item in _Handler.requests))
        self.assertTrue(all("messages" in item["body"]["input"] and "text" not in item["body"]["input"] for item in _Handler.requests))
        self.assertTrue(
            all(set(item["body"]) == {"provider", "model", "input"} for item in _Handler.requests)
        )
        self.assertTrue(all("operation" not in item["body"] for item in _Handler.requests))
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(
            [(item["request_id"], item["permit_id"]) for item in records],
            [
                ("01K40000000000000000000001", "00000000-0000-0000-0000-000000000001"),
                ("01K40000000000000000000002", "00000000-0000-0000-0000-000000000002"),
            ],
        )

    def test_missing_or_oversized_correlation_headers_do_not_become_evidence(self):
        self.assertIsNone(
            verify_execute._correlation_header(
                None,
                field="request_id",
                name="X-Keel-Request-ID",
            )
        )
        self.assertIsNone(
            verify_execute._correlation_header(
                {"X-Keel-Request-ID": "x" * 257},
                field="request_id",
                name="X-Keel-Request-ID",
            )
        )

    def test_split_secret_in_correlation_headers_is_not_printed(self):
        sentinel = "ks_live_abcdefghijklmnop"
        first, second = sentinel[:12], sentinel[12:]
        allow = {"status": "completed", "governance": {"decision": "allow"}}
        deny = {
            "status": "denied",
            "governance": {"decision": "deny"},
            "error": {"stage": "permit", "code": "policy.rule_denied"},
        }
        injected = {
            "X-Keel-Request-ID": first,
            "X-Keel-Permit-ID": second,
        }
        with server([(200, allow, injected), (403, deny, injected)]) as base_url:
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, {"KEEL_API_KEY": sentinel}, clear=False),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = verify_execute.main(
                    [
                        "--provider",
                        "test",
                        "--allow-model",
                        "small",
                        "--deny-model",
                        "large",
                        "--base-url",
                        base_url,
                    ]
                )
        emitted = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn(first, emitted)
        self.assertNotIn(second, emitted)
        self.assertNotIn(sentinel, emitted)

    def test_response_body_over_the_fixed_limit_is_refused_without_printing_it(self):
        marker = "response-content-must-not-print"
        raw = (marker + "x" * verify_execute.MAX_RESPONSE_BYTES).encode()
        with server([(502, raw)]) as base_url:
            result = verify_execute.execute_attempt(
                base_url=base_url,
                key="redacted-test-value",
                provider="test",
                model="model",
                expectation="allow",
            )
        self.assertEqual(result["classification"], "malformed_response")
        self.assertNotIn(marker, json.dumps(result))

    def test_response_scalars_are_closed_or_bounded_protocol_values(self):
        body = {
            "status": "credential fragment",
            "governance": {"decision": "credential fragment"},
            "error": {"stage": "credential fragment", "code": "UPPERCASE secret"},
        }
        result = verify_execute.classify(500, body)
        self.assertIsNone(result["body_status"])
        self.assertIsNone(result["governance_decision"])
        self.assertIsNone(result["error_stage"])
        self.assertIsNone(result["error_code"])

    def test_provider_refusal_at_401_and_403_is_not_denial(self):
        for status in (401, 403):
            with self.subTest(status=status), server([(status, {"status": "failed", "governance": {"decision": "allow"}, "error": {"stage": "dispatch", "code": "provider_refused"}})]) as base_url:
                result = verify_execute.execute_attempt(base_url=base_url, key="redacted-test-value", provider="test", model="model", expectation="allow")
                self.assertEqual(result["classification"], "provider_dispatch_failed_after_allow")

    def test_non_json_and_transport_failure_are_bounded(self):
        with server([(502, b"not json")]) as base_url:
            result = verify_execute.execute_attempt(base_url=base_url, key="redacted-test-value", provider="test", model="model", expectation="allow")
        self.assertEqual(result["classification"], "malformed_response")
        result = verify_execute.execute_attempt(base_url="http://127.0.0.1:1", key="redacted-test-value", provider="test", model="model", expectation="allow", timeout=0.05)
        self.assertEqual(result["classification"], "transport_failed")

    def test_timeout_and_name_resolution_failure_do_not_print_exception_content(self):
        for failure in (TimeoutError("sensitive details"), urllib.error.URLError("name resolution sensitive details")):
            with self.subTest(kind=type(failure).__name__), mock.patch.object(verify_execute, "_open", side_effect=failure):
                result = verify_execute.execute_attempt(base_url="https://example.invalid", key="redacted-test-value", provider="test", model="model", expectation="allow")
                self.assertEqual(result["classification"], "transport_failed")
                self.assertNotIn("sensitive", json.dumps(result))

    def test_default_execute_timeout_covers_normal_provider_latency(self):
        timeout = inspect.signature(verify_execute.execute_attempt).parameters["timeout"].default
        self.assertEqual(timeout, 30.0)

    def test_transport_failure_leads_with_beginner_message_and_keeps_classification(self):
        sentinel = "unit-test-key-that-must-not-print"
        result = {
            "model": "model",
            "expectation": "allow",
            "request_id": None,
            "permit_id": None,
            "http_status": None,
            "body_status": None,
            "governance_decision": None,
            "error_stage": None,
            "error_code": None,
            "classification": "transport_failed",
        }
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, {"KEEL_API_KEY": sentinel}, clear=False),
            mock.patch.object(verify_execute, "execute_attempt", return_value=result),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = verify_execute.main(
                ["--provider", "test", "--allow-model", "a", "--deny-model", "b"]
            )
        self.assertEqual(code, 1)
        self.assertTrue(err.getvalue().startswith("Could not reach Keel."))
        self.assertIn("Technical details: transport_failed", err.getvalue())
        self.assertEqual(
            [json.loads(line)["classification"] for line in out.getvalue().splitlines()[:2]],
            ["transport_failed", "transport_failed"],
        )
        self.assertNotIn(sentinel, out.getvalue() + err.getvalue())

    def test_missing_key_is_local_precondition_exit_two(self):
        env = dict(os.environ)
        env.pop("KEEL_API_KEY", None)
        result = subprocess.run([sys.executable, str(SCRIPT), "--provider", "test", "--allow-model", "a", "--deny-model", "b"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not set", result.stderr)
        self.assertIn("Runtime key", result.stderr)
        self.assertNotIn("client-scoped", result.stderr)

    def test_cli_has_no_credential_argument(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
        help_text = result.stdout.lower()
        self.assertNotIn("--api-key", help_text)
        self.assertNotIn("--token", help_text)
        self.assertNotIn("--credential", help_text)

    def test_interactive_key_verifies_release_before_prompt_and_never_emits_key(self):
        sentinel = "unit-test-hidden-key-that-must-not-print"
        events: list[str] = []
        allow = {"status": "completed", "governance": {"decision": "allow"}}
        deny = {
            "status": "denied",
            "governance": {"decision": "deny"},
            "error": {"stage": "permit", "code": "policy.rule_denied"},
        }

        def verify_release(bundle_sha: str) -> None:
            self.assertEqual(bundle_sha, "a" * 40)
            events.append("release")

        def read_key() -> str:
            events.append("prompt")
            return sentinel

        profile = verification_profile()
        with server([(200, profile), (200, allow), (403, deny), (200, profile)]) as base_url:
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(verify_execute, "_verify_pinned_release", side_effect=verify_release),
                mock.patch.object(verify_execute, "_read_hidden_runtime_key", side_effect=read_key),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = verify_execute.main(
                    [
                        "--base-url",
                        base_url,
                        "--hidden-input",
                        "--bundle-sha",
                        "a" * 40,
                    ]
                )

        emitted = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertEqual(events, ["release", "prompt"])
        self.assertNotIn(sentinel, emitted)
        self.assertIn("Pinned Keel setup release verified.", err.getvalue())
        self.assertIn("Allowed request: PASS", out.getvalue())
        self.assertIn("Blocked request: PASS", out.getvalue())
        self.assertEqual(
            [item["path"] for item in _Handler.requests],
            [
                "/v1/verification-profile",
                "/v1/execute",
                "/v1/execute",
                "/v1/verification-profile",
            ],
        )
        self.assertEqual(
            [item["body"]["model"] for item in _Handler.requests[1:3]],
            ["gpt-4o-mini", "o4-mini"],
        )
        nonces = [item["headers"]["X-Keel-Nonce"] for item in _Handler.requests]
        self.assertEqual(len(nonces), len(set(nonces)))

    def test_hidden_input_rejects_caller_selected_provider_or_models(self):
        for selector in (
            ["--provider", "openai"],
            ["--allow-model", "gpt-4o-mini"],
            ["--deny-model", "o4-mini"],
        ):
            with self.subTest(selector=selector[0]):
                out, err = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(verify_execute, "_verify_pinned_release") as release,
                    mock.patch.object(verify_execute, "_read_hidden_runtime_key") as prompt,
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    code = verify_execute.main(
                        ["--hidden-input", "--bundle-sha", "a" * 40, *selector]
                    )
                self.assertEqual(code, 2)
                release.assert_not_called()
                prompt.assert_not_called()
                self.assertIn("obtains its model pair from Keel", err.getvalue())

    def test_profile_change_during_proof_invalidates_successful_pair(self):
        sentinel = "unit-test-hidden-key-that-must-not-print"
        allow = {"status": "completed", "governance": {"decision": "allow"}}
        deny = {
            "status": "denied",
            "governance": {"decision": "deny"},
            "error": {"stage": "permit", "code": "policy.rule_denied"},
        }
        with server(
            [
                (200, verification_profile(digest="a" * 64)),
                (200, allow),
                (403, deny),
                (200, verification_profile(digest="d" * 64)),
            ]
        ) as base_url:
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(verify_execute, "_verify_pinned_release"),
                mock.patch.object(
                    verify_execute,
                    "_read_hidden_runtime_key",
                    return_value=sentinel,
                ),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = verify_execute.main(
                    [
                        "--base-url",
                        base_url,
                        "--hidden-input",
                        "--bundle-sha",
                        "a" * 40,
                    ]
                )
        self.assertEqual(code, 1)
        self.assertIn("profile changed during proof", err.getvalue())
        self.assertNotIn("Allowed request: PASS", out.getvalue())
        self.assertNotIn(sentinel, out.getvalue() + err.getvalue())

    def test_profile_parser_rejects_extra_metadata_and_bad_bindings(self):
        extra = verification_profile()
        extra["project_id"] = "not-allowed"
        invalid_digest = verification_profile()
        invalid_digest["profile_digest"] = "not-a-digest"
        same_model = verification_profile()
        same_model["denied_model"] = same_model["allowed_model"]
        for profile in (extra, invalid_digest, same_model):
            with self.subTest(profile=profile):
                with self.assertRaises(verify_execute.VerificationProfileError):
                    verify_execute._parse_verification_profile(json.dumps(profile).encode())

    def test_profile_binding_detects_changed_fields_even_if_digest_is_repeated(self):
        first = verification_profile()
        changed = verification_profile()
        changed["allowed_model"] = "gpt-4o"
        self.assertEqual(first["profile_digest"], changed["profile_digest"])
        self.assertNotEqual(
            verify_execute._profile_binding(first),
            verify_execute._profile_binding(changed),
        )

    def test_profile_failure_is_bounded_and_never_echoes_server_text_or_key(self):
        sentinel = "unit-test-hidden-key-that-must-not-print"
        marker = "server-text-that-must-not-print"
        with server([(409, {"error": {"code": "verification_profile_pending", "message": marker}})]) as base_url:
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(verify_execute, "_verify_pinned_release"),
                mock.patch.object(
                    verify_execute,
                    "_read_hidden_runtime_key",
                    return_value=sentinel,
                ),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = verify_execute.main(
                    [
                        "--base-url",
                        base_url,
                        "--hidden-input",
                        "--bundle-sha",
                        "a" * 40,
                    ]
                )
        emitted = out.getvalue() + err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("verification_profile_pending", emitted)
        self.assertNotIn(marker, emitted)
        self.assertNotIn(sentinel, emitted)

    def test_interactive_key_refuses_non_tty_without_calling_getpass(self):
        with (
            mock.patch.object(sys, "stdin", _NonInteractiveInput()),
            mock.patch.object(verify_execute.getpass, "getpass") as prompt,
            self.assertRaisesRegex(RuntimeError, "requires a TTY"),
        ):
            verify_execute._read_hidden_runtime_key()
        prompt.assert_not_called()

    def test_interactive_key_reads_from_no_echo_tty(self):
        sentinel = "unit-test-hidden-key"
        with (
            mock.patch.object(sys, "stdin", _InteractiveInput()),
            mock.patch.object(verify_execute.getpass, "getpass", return_value=sentinel) as prompt,
        ):
            self.assertEqual(verify_execute._read_hidden_runtime_key(), sentinel)
        prompt.assert_called_once_with("Keel Runtime key: ", stream=sys.stderr)

    def test_interactive_key_refuses_getpass_warning_before_visible_fallback(self):
        def warn_then_fallback(*args, **kwargs):
            warnings.warn("visible fallback", verify_execute.getpass.GetPassWarning)
            self.fail("visible fallback continued")

        with (
            mock.patch.object(sys, "stdin", _InteractiveInput()),
            mock.patch.object(verify_execute.getpass, "getpass", side_effect=warn_then_fallback),
            self.assertRaisesRegex(RuntimeError, "echo could not be disabled"),
        ):
            verify_execute._read_hidden_runtime_key()

    def test_interactive_key_refuses_terminal_failure(self):
        with (
            mock.patch.object(sys, "stdin", _InteractiveInput()),
            mock.patch.object(verify_execute.getpass, "getpass", side_effect=OSError("terminal failure")),
            self.assertRaisesRegex(RuntimeError, "could not be read securely"),
        ):
            verify_execute._read_hidden_runtime_key()

    def test_secret_cli_arguments_are_refused_without_echoing_their_values(self):
        sentinel = "secret-cli-value-that-must-not-print"
        for secret_args in (["--api-key", sentinel], [f"--runtime-key={sentinel}"], ["--token", sentinel]):
            with self.subTest(secret_args=secret_args[0]):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = verify_execute.main(
                        [
                            "--provider",
                            "test",
                            "--allow-model",
                            "a",
                            "--deny-model",
                            "b",
                            *secret_args,
                        ]
                    )
                self.assertEqual(code, 2)
                self.assertNotIn(sentinel, out.getvalue() + err.getvalue())
                self.assertIn("never accepted", err.getvalue())

    def test_interactive_mode_requires_bundle_sha_before_prompt(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(verify_execute, "_read_hidden_runtime_key") as prompt,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = verify_execute.main(["--hidden-input"])
        self.assertEqual(code, 2)
        prompt.assert_not_called()
        self.assertIn("--bundle-sha is required", err.getvalue())

    def test_release_failure_stops_before_credential_prompt(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(
                verify_execute,
                "_verify_pinned_release",
                side_effect=RuntimeError("pinned release verification failed"),
            ),
            mock.patch.object(verify_execute, "_read_hidden_runtime_key") as prompt,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = verify_execute.main(
                [
                    "--hidden-input",
                    "--bundle-sha",
                    "a" * 40,
                ]
            )
        self.assertEqual(code, 2)
        prompt.assert_not_called()
        self.assertEqual(err.getvalue(), "pinned release verification failed\n")

    def test_base_url_accepts_only_production_origin_or_explicit_loopback_port(self):
        accepted = [
            "https://api.keelapi.com", "https://api.keelapi.com/", "https://api.keelapi.com:443",
            "http://127.0.0.1:8123", "http://localhost:8123/", "http://[::1]:8123",
        ]
        for value in accepted:
            with self.subTest(value=value):
                args = verify_execute.parse_args(["--provider", "test", "--allow-model", "a", "--deny-model", "b", "--base-url", value])
                self.assertEqual(args.base_url, value.rstrip("/"))

    def test_base_url_cannot_exfiltrate_the_environment_key(self):
        rejected = [
            "https://example.invalid", "http://api.keelapi.com:80", "https://api.keelapi.com:8443",
            "https://api.keelapi.com/prefix", "https://api.keelapi.com?query=yes",
            "https://api.keelapi.com#fragment", "https://user@api.keelapi.com",
            "https://api.keelapi.com:", "http://127.0.0.1", "http://localhost/prefix",
            "http://127.0.0.1:80",
        ]
        for value in rejected:
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                verify_execute.parse_args(["--provider", "test", "--allow-model", "a", "--deny-model", "b", "--base-url", value])

    def test_redirects_remain_disabled(self):
        request = urllib.request.Request("https://api.keelapi.com/v1/execute")
        redirected = verify_execute._NoRedirectHandler().redirect_request(
            request, None, 302, "Found", {"Location": "https://example.invalid"}, "https://example.invalid"
        )
        self.assertIsNone(redirected)

    def test_unexpected_upstream_echo_is_redacted(self):
        sentinel = "unit-test-value-that-must-stay-redacted"
        record = {field: None for field in verify_execute.OUTPUT_FIELDS}
        record.update({"error_code": f"unexpected-{sentinel}", "classification": "unexpected"})
        redacted = verify_execute.redact_record(record, sentinel)
        self.assertEqual(redacted["error_code"], "[REDACTED]")
        self.assertNotIn(sentinel, json.dumps(redacted))


class PinnedReleaseGateTest(unittest.TestCase):
    """Exercise both credential gates against real disposable Git repositories."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "gate_authoring_context", SCRIPT.with_name("authoring_context.py")
        )
        self.authoring = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.authoring)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = pathlib.Path(self.temp.name)
        scripts = self.bundle / "keel-setup/scripts"
        scripts.mkdir(parents=True)
        self.sibling = scripts / "fast_first_run.py"
        self.sibling.write_text("def verify_release(*args): pass\n")
        (self.bundle / "scripts").mkdir()
        self.checker = self.bundle / "scripts/check_release_bundle.py"
        self.checker.write_text("raise AssertionError('must not execute checker')\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Gate test")
        self.git("config", "user.email", "gate@example.invalid")
        self.git("remote", "add", "origin", "https://github.com/keelapi/keel-setup.git")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.pin = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.bundle), *args], check=True,
            capture_output=True, text=True,
        ).stdout

    def gates(self):
        return (verify_execute, self.authoring)

    def check_gate(self, module, pin=None):
        with mock.patch.object(module, "__file__", str(self.sibling.with_name("trusted.py"))):
            module._verify_pinned_release(self.pin if pin is None else pin)

    def test_clean_pinned_bundle_never_executes_siblings(self):
        for module in self.gates():
            with self.subTest(module=module.__name__):
                self.check_gate(module)

    def test_tampered_sibling_cannot_patch_prompt_or_network(self):
        marker = self.bundle / "executed"
        payload = (
            "import pathlib, getpass, urllib.request\n"
            f"pathlib.Path({str(marker)!r}).touch()\n"
            "getpass.getpass = lambda *a, **k: 'stolen'\n"
            "urllib.request.build_opener = lambda *a: None\n"
            "def verify_release(*args): pass\n"
        )
        original_prompt = verify_execute.getpass.getpass
        original_opener = urllib.request.build_opener
        self.sibling.write_text(payload)
        # Git cleanliness alone misses both of these flags.
        for flag in ("--assume-unchanged", "--skip-worktree"):
            self.git("update-index", flag, "keel-setup/scripts/fast_first_run.py")
            for module in self.gates():
                with self.subTest(flag=flag, module=module.__name__):
                    with mock.patch.object(module, "__file__", str(self.sibling.with_name("trusted.py"))), \
                         mock.patch.object(module, "_read_hidden_runtime_key") as prompt, \
                         mock.patch.object(module, "_open") as network, \
                         contextlib.redirect_stderr(io.StringIO()):
                        args = ["--bundle-sha", self.pin]
                        if module is verify_execute:
                            args.append("--hidden-input")
                        self.assertEqual(module.main(args), 2)
                    prompt.assert_not_called()
                    network.assert_not_called()
                    self.assertFalse(marker.exists())
                    self.assertIs(verify_execute.getpass.getpass, original_prompt)
                    self.assertIs(urllib.request.build_opener, original_opener)
            self.git("update-index", "--no-assume-unchanged", "--no-skip-worktree", "keel-setup/scripts/fast_first_run.py")

    def test_tampered_checker_hidden_from_status_is_rejected(self):
        self.checker.write_text("raise AssertionError('tampered checker executed')\n")
        self.git("update-index", "--assume-unchanged", "scripts/check_release_bundle.py")
        for module in self.gates():
            with self.subTest(module=module.__name__), self.assertRaises(RuntimeError):
                self.check_gate(module)

    def test_symlink_even_to_identical_bytes_is_rejected(self):
        target = self.bundle / "target"
        target.write_bytes(self.sibling.read_bytes())
        self.sibling.unlink()
        self.sibling.symlink_to(target)
        self.git("update-index", "--assume-unchanged", "keel-setup/scripts/fast_first_run.py")
        target.rename(self.bundle / ".git/target")
        self.sibling.unlink()
        self.sibling.symlink_to(self.bundle / ".git/target")
        for module in self.gates():
            with self.subTest(module=module.__name__), self.assertRaises(RuntimeError):
                self.check_gate(module)

    def test_invalid_pin_origin_and_dirty_bundle_are_rejected(self):
        for pin in ("a" * 40, self.pin.upper(), "HEAD", ""):
            for module in self.gates():
                with self.subTest(pin=pin, module=module.__name__), self.assertRaises(RuntimeError):
                    self.check_gate(module, pin)
        self.git("remote", "set-url", "origin", "https://example.invalid/bundle")
        for module in self.gates():
            with self.assertRaises(RuntimeError):
                self.check_gate(module)
        self.git("remote", "set-url", "origin", "https://github.com/keelapi/keel-setup.git")
        (self.bundle / "untracked").touch()
        for module in self.gates():
            with self.assertRaises(RuntimeError):
                self.check_gate(module)

    def test_replacement_objects_cannot_authenticate_modified_files(self):
        original = self.git("rev-parse", "HEAD:keel-setup/scripts/fast_first_run.py").strip()
        self.sibling.write_text("def verify_release(*args): return 'forged'\n")
        replacement = self.git("hash-object", "-w", str(self.sibling)).strip()
        self.git("replace", original, replacement)
        self.git("update-index", "--assume-unchanged", "keel-setup/scripts/fast_first_run.py")
        for module in self.gates():
            with self.subTest(module=module.__name__), self.assertRaises(RuntimeError):
                self.check_gate(module)

    def test_submodule_is_not_an_authenticated_regular_file(self):
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.pin},nested")
        self.git("commit", "-qm", "submodule fixture")
        self.pin = self.git("rev-parse", "HEAD").strip()
        for module in self.gates():
            with self.subTest(module=module.__name__), self.assertRaises(RuntimeError):
                self.check_gate(module)

    def test_local_fsmonitor_hook_is_never_executed(self):
        marker = self.bundle / "hook-executed"
        hook = self.bundle / ".git/fsmonitor"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        self.git("config", "core.fsmonitor", str(hook))
        for module in self.gates():
            with self.subTest(module=module.__name__):
                self.check_gate(module)
        self.assertFalse(marker.exists())

    def test_gate_implementations_remain_identical(self):
        self.assertEqual(inspect.getsource(verify_execute._verify_pinned_release),
                         inspect.getsource(self.authoring._verify_pinned_release))


if __name__ == "__main__":
    unittest.main(verbosity=2)
