from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).with_name("validate_feedback_report.py")
SPEC = importlib.util.spec_from_file_location("validator", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(validator)


def report():
    return {
        "category": "integration_request",
        "summary": "Support a fact-dependent control",
        "intended_task": "Apply an automatic threshold safely",
        "expected_behavior": None,
        "observed_behavior": "The required fact is unavailable",
        "desired_outcome": "Automatic authorization from a verified fact",
        "surface": "mcp",
        "blocker": "trusted_fact_unavailable",
        "evidence_level": "source_inspected",
        "coding_agent": None,
        "skill_version": None,
        "keel_release": None,
        "decision_classification": "review_only",
        "optional_context": {"provider": None, "tool_name": None, "source_locations": [], "environment_details": None},
    }


class FeedbackValidatorTest(unittest.TestCase):
    def test_safe_default_report_passes_without_context_approval(self):
        self.assertEqual(validator.validate(report(), set(), None), [])

    def test_each_optional_field_requires_separate_approval(self):
        candidate = report()
        candidate["optional_context"].update({"provider": "example-provider", "tool_name": "example.tool", "source_locations": ["src/tool.py:9"], "environment_details": "Python runtime"})
        failures = validator.validate(candidate, {"provider"}, None)
        self.assertEqual(sum("without separate" in item for item in failures), 3)
        self.assertEqual(validator.validate(candidate, set(validator.OPTIONAL_FIELDS), None), [])

    def test_architecture_fields_outside_optional_context_and_unknown_fields_are_rejected(self):
        for key in ("provider", "tool_name", "source_locations", "environment_details", "unexpected"):
            candidate = report()
            candidate[key] = "not allowed here"
            self.assertTrue(validator.validate(candidate, set(), None))

    def test_sensitive_structures_and_content_are_rejected(self):
        for key in ("project_id", "raw_logs", "code", "attachment", "request_body", "permit_body"):
            candidate = report()
            candidate["optional_context"][key] = "redacted"
            self.assertTrue(validator.validate(candidate, set(), None))
        candidate = report()
        candidate["observed_behavior"] = "Authorization" + ": Bearer " + "example-sensitive-value"
        self.assertTrue(any("credential" in item for item in validator.validate(candidate, set(), None)))

    def test_absolute_parent_paths_and_security_public_channel_are_rejected(self):
        for location in ("/private/source.py", "../source.py"):
            candidate = report()
            candidate["optional_context"]["source_locations"] = [location]
            self.assertTrue(validator.validate(candidate, {"source_locations"}, None))
        candidate = report()
        candidate["category"] = "security_concern"
        self.assertTrue(validator.validate(candidate, set(), "github_issue"))
        self.assertEqual(validator.validate(candidate, set(), "private_security"), [])

    def test_wrong_types_and_raw_environment_are_rejected(self):
        candidate = report()
        candidate["summary"] = {"not": "text"}
        self.assertTrue(validator.validate(candidate, set(), None))
        candidate = report()
        candidate["observed_behavior"] = "SAMPLE_VARIABLE" + "=example-value"
        self.assertTrue(any("environment variable" in item for item in validator.validate(candidate, set(), None)))

    def test_every_schema_limit_for_source_locations_is_enforced(self):
        candidate = report()
        candidate["optional_context"]["source_locations"] = [f"src/file-{index}.py" for index in range(21)]
        self.assertTrue(any("more than 20" in item for item in validator.validate(candidate, {"source_locations"}, None)))
        candidate = report()
        candidate["optional_context"]["source_locations"] = ["x" * 301]
        self.assertTrue(any("longer than 300" in item for item in validator.validate(candidate, {"source_locations"}, None)))
        candidate = report()
        candidate["optional_context"]["source_locations"] = [42]
        self.assertTrue(any("expected type string" in item for item in validator.validate(candidate, {"source_locations"}, None)))

    def test_common_secret_token_cookie_and_password_shapes_are_rejected(self):
        samples = [
            "password" + "=example-sensitive-value",
            "cookie" + ": session-example-sensitive-value",
            "token" + "=example-sensitive-value",
            "access_token" + "=example-sensitive-value",
            "eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10,
            "sk" + "-" + "a" * 12,
            "AKIA" + "A" * 16,
        ]
        for sample in samples:
            candidate = report()
            candidate["observed_behavior"] = sample
            with self.subTest(sample=sample[:8]):
                self.assertTrue(validator.validate(candidate, set(), None))

    def test_absolute_paths_and_raw_artifact_markers_are_rejected_anywhere(self):
        samples = [
            "/Users/example/private.py",
            "/home/example/private.py",
            "/root/private.py",
            "C:\\Users\\example\\private.py",
            "```python\nprint('example')\n```",
            "diff --git a/a b/a\n@@ -1 +1 @@",
            "Traceback (most recent call last):",
            "[ERROR] example failure",
            "request body: {example}",
            "system prompt: example",
            "raw model output: example",
            "Raw code: def refund(): pass",
            "Source: def refund(): pass",
            "Raw logs: 2026-08-25 ERROR customer_id=123",
            "Logs: ERROR customer_email=example@example.invalid",
        ]
        for sample in samples:
            candidate = report()
            candidate["observed_behavior"] = sample
            with self.subTest(sample=sample[:12]):
                self.assertTrue(validator.validate(candidate, set(), None))

    def test_oversize_payload_is_rejected_before_parsing(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            path = pathlib.Path(handle.name)
            handle.write(b"x" * (validator.MAX_BYTES + 1))
        self.addCleanup(path.unlink)
        self.assertEqual(validator.main([str(path)]), 1)

    def test_preview_disclaims_proof_of_redaction_approval_and_routing(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            path = pathlib.Path(handle.name)
            json.dump(report(), handle)
        self.addCleanup(path.unlink)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(validator.main([str(path), "--preview"]), 0)
        rendered = output.getvalue()
        self.assertIn("do not prove redaction, human approval, or private routing", rendered)
        self.assertIn('"summary": "Support a fact-dependent control"', rendered)

    def test_preview_refuses_raw_artifacts_and_does_not_echo_payload(self):
        candidate = report()
        candidate["observed_behavior"] = "Raw code: def refund(): pass"
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            path = pathlib.Path(handle.name)
            json.dump(candidate, handle)
        self.addCleanup(path.unlink)
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(validator.main([str(path), "--preview"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("def refund", error.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
