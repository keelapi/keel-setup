from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
import uuid
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

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
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

    def log_message(self, format, *args):  # noqa: A002
        return


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

    def test_missing_key_is_local_precondition_exit_two(self):
        env = dict(os.environ)
        env.pop("KEEL_API_KEY", None)
        result = subprocess.run([sys.executable, str(SCRIPT), "--provider", "test", "--allow-model", "a", "--deny-model", "b"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not set", result.stderr)

    def test_cli_has_no_credential_argument(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
        help_text = result.stdout.lower()
        self.assertNotIn("--api-key", help_text)
        self.assertNotIn("--token", help_text)
        self.assertNotIn("--credential", help_text)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
