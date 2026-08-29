from __future__ import annotations

import json
import pathlib
import unittest

import schema_validation


class SchemaSubsetValidatorTest(unittest.TestCase):
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
