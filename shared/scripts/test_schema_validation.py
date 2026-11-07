from __future__ import annotations

import json
import pathlib
import unittest

import schema_validation


class SchemaSubsetValidatorTest(unittest.TestCase):
    def test_recursive_schema_and_failure_output_are_bounded(self):
        schema = {"$ref": "#/$defs/cycle", "$defs": {"cycle": {"$ref": "#/$defs/cycle"}}}
        self.assertTrue(schema_validation.validate({}, schema))
        errors = schema_validation.validate(["wrong"] * 1000, {"type": "array", "items": {"type": "integer"}})
        self.assertEqual(len(errors), 50)
    def test_approval_selectors_are_closed_and_bound_to_their_type(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        policy_schema = json.loads((root / "keel-policy/reference/policy-document.schema.json").read_text())
        schema = policy_schema["$defs"]["ApprovalRequirement"]
        identifier = "12345678-1234-4234-8234-123456789abc"
        choices = {"org_role": ("role", "admin"), "approver_group": ("group_id", identifier),
                   "team": ("team_slug", "reviewers"), "user": ("user_id", identifier),
                   "email": ("email", "review@example.invalid"), "service_principal": ("service_principal_id", identifier)}
        for kind, (selector, value) in choices.items():
            base = {"type": kind, "timeout_seconds": 60}
            self.assertTrue(schema_validation.validate(base, schema))
            valid = {**base, selector: value}
            self.assertEqual(schema_validation.validate(valid, schema), [], kind)
            other = "role" if selector != "role" else "email"
            self.assertTrue(schema_validation.validate({**valid, other: "admin"}, schema))
            self.assertTrue(schema_validation.validate({**valid, "external_workflow": {"unexpected": True}}, schema))
        self.assertTrue(schema_validation.validate({"type": "approver_group", "timeout_seconds": 60,
            "group_id": identifier, "group_slug": "reviewers"}, schema))

    def test_date_time_requires_an_unambiguous_rfc3339_instant(self):
        schema = {"type": "string", "format": "date-time"}
        for value in ("2026-11-06T12:00:00", "20261106T120000Z", "2026-11-06T12:00Z",
                      "2026-11-06 12:00:00Z", "2026-11-06T12:00:00+24:00", "2026-02-30T12:00:00Z"):
            self.assertTrue(schema_validation.validate(value, schema), value)
        for value in ("2026-11-06T12:00:00Z", "2026-11-06T12:00:00.123-08:00", "2026-11-06T12:00:00+05:30"):
            self.assertEqual(schema_validation.validate(value, schema), [], value)

    def test_nonfinite_numbers_are_rejected_even_in_open_objects(self):
        for raw in ("NaN", "Infinity", "-Infinity", "1e999"):
            value = json.loads(raw)
            for candidate, schema in ((value, {"type": "number", "exclusiveMinimum": 0}),
                                      ({"open": [value]}, {"type": "object"})):
                with self.subTest(raw=raw):
                    self.assertTrue(schema_validation.validate(candidate, schema))
                    self.assertTrue(schema_validation.validate(candidate, schema, root_schema=schema))
        self.assertEqual(schema_validation.validate(0.5, {"type": "number", "exclusiveMinimum": 0}), [])

    def test_required_types_limits_unknowns_and_uniqueness(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "items"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 3},
                "items": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True, "items": {"type": "integer", "minimum": 1, "maximum": 5}},
            },
        }
        self.assertEqual(schema_validation.validate({"name": "ok", "items": [1, 2]}, schema), [])
        cases = [
            {},
            {"name": "", "items": [1]},
            {"name": "long", "items": [1]},
            {"name": "ok", "items": []},
            {"name": "ok", "items": [1, 1]},
            {"name": "ok", "items": [0]},
            {"name": "ok", "items": [6]},
            {"name": "ok", "items": [1], "unknown": True},
        ]
        for index, candidate in enumerate(cases):
            with self.subTest(index=index):
                self.assertTrue(schema_validation.validate(candidate, schema))

    def test_unimplemented_schema_keyword_fails_closed(self):
        self.assertTrue(schema_validation.validate("value", {"type": "string", "contentEncoding": "base64"}))

    def test_committed_policy_schema_and_references_are_supported(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        schema = json.loads((root / "keel-policy/reference/policy-document.schema.json").read_text())
        policy = json.loads((root / "keel-policy/examples/stripe-refund-approval.json").read_text())
        self.assertEqual(schema_validation.validate(policy, schema), [])
        self.assertTrue(schema_validation.validate({"name": "legacy", "rules": [{"when": {}, "decision": "allow"}]}, schema))


if __name__ == "__main__":
    unittest.main(verbosity=2)
