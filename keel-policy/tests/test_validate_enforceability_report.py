from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "validate_enforceability_report.py"
SPEC = importlib.util.spec_from_file_location("validator", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(validator)
ROOT = pathlib.Path(__file__).parents[2]
MANIFEST = json.loads((ROOT / "tools" / "public_surface.json").read_text())
PROVENANCE = json.loads((ROOT / "keel-policy" / "reference" / "field-provenance.json").read_text())
SCHEMA = json.loads((ROOT / "keel-policy" / "reference" / "enforceability-report.schema.json").read_text())
POLICY_SCHEMA = json.loads((ROOT / "keel-policy" / "reference" / "policy-document.schema.json").read_text())


def report(field="action.name", status="untrusted", replacement=validator.BOUND_TOOL_NAME):
    return {
        "schema_version": "1.0",
        "intent": ["Require review for this exact MCP tool."],
        "policy": None,
        "readback": ["The rule requires human review."],
        "enforceability": [{
            "rule_reference": "rule 1", "required_fact": "exact MCP tool identity", "field": field,
            "surface": "managed_mcp", "provenance": PROVENANCE["fields"].get(field, "unresolved"), "status": status,
            "safe_outcome": "review", "reason": "The caller selects action.name.", "replacement_field": replacement,
        }],
        "activation_effect": "unresolved",
        "blocked_requests": [{"request": "review this tool", "reason": "must use a server-derived field"}],
        "human_next_step": "Review the draft and import it in the dashboard; the agent does not activate it.",
    }


def validate(candidate):
    return validator.validate(candidate, MANIFEST, PROVENANCE, SCHEMA, POLICY_SCHEMA)


def add_policy(candidate, field, action="allow"):
    candidate["policy"] = {
        "name": "Policy under analysis",
        "rules": [{"if": {"field": field, "op": "eq", "value": "example"}, "action": action}],
    }
    return candidate


class EnforceabilityValidatorTest(unittest.TestCase):
    def test_action_name_requires_untrusted_status_and_exact_replacement(self):
        self.assertEqual(validate(report()), [])
        self.assertTrue(validate(report(status="trusted")))
        self.assertTrue(validate(report(replacement=None)))

    def test_every_required_report_field_and_item_constraint_is_enforced(self):
        for key in ("intent", "policy", "readback"):
            candidate = report()
            del candidate[key]
            self.assertTrue(any("missing required" in item for item in validate(candidate)), key)
        candidate = report()
        del candidate["enforceability"][0]["reason"]
        self.assertTrue(any("missing required" in item for item in validate(candidate)))
        candidate = report()
        candidate["enforceability"][0]["unexpected"] = True
        self.assertTrue(any("unknown property" in item for item in validate(candidate)))
        candidate = report()
        candidate["intent"] = ["x" * 1001]
        self.assertTrue(any("longer" in item for item in validate(candidate)))

    def test_every_policy_condition_field_requires_one_enforceability_entry(self):
        candidate = add_policy(report(), "action.name", "require_human_review")
        candidate["enforceability"] = []
        self.assertTrue(any("no enforceability entry" in item for item in validate(candidate)))
        candidate = add_policy(report(), "action.name", "require_human_review")
        candidate["policy"]["rules"][0]["if"] = {"all": [
            {"field": "action.name", "op": "eq", "value": "example.tool"},
            {"field": "provider", "op": "eq", "value": "example"},
        ]}
        self.assertTrue(any("provider" in item and "no enforceability" in item for item in validate(candidate)))

    def test_embedded_policy_must_match_the_canonical_policy_document_schema(self):
        candidate = report()
        candidate["policy"] = {"name": "Legacy", "rules": [{"when": {"field": "provider"}, "decision": "allow"}]}
        self.assertTrue(any("$.policy.rules[0]" in item for item in validate(candidate)))
        valid = add_policy(report(field="provider", status="trusted", replacement=None), "provider", "preview")
        self.assertFalse(any(item.startswith("$.policy") for item in validate(valid)), validate(valid))

    def test_caller_asserted_financial_amount_cannot_be_trusted_or_auto_allow(self):
        field = "context._keel.action_envelope.financial.amount_usd_micros.value"
        candidate = report(field=field, status="trusted", replacement=None)
        candidate["enforceability"][0]["safe_outcome"] = "auto_allow"
        add_policy(candidate, field, "allow")
        failures = validate(candidate)
        self.assertTrue(any("caller_asserted field cannot be trusted" in item for item in failures))
        self.assertTrue(any("automatic action uses field" in item for item in failures))

    def test_connector_asserted_value_cannot_be_trusted(self):
        candidate = report(field="action.attributes.trusted_facts.max_uses", status="trusted", replacement=None)
        failures = validate(candidate)
        self.assertTrue(any("connector_asserted field cannot be trusted" in item for item in failures))

    def test_automatic_allow_with_empty_enforceability_is_rejected(self):
        candidate = report(field="provider", status="trusted", replacement=None)
        add_policy(candidate, "provider", "allow")
        candidate["enforceability"] = []
        self.assertTrue(any("no enforceability entry" in item for item in validate(candidate)))
        del candidate["enforceability"]
        self.assertTrue(any("missing required property 'enforceability'" in item for item in validate(candidate)))

    def test_untrusted_fact_cannot_drive_review_either(self):
        candidate = add_policy(report(), "action.name", "require_human_review")
        self.assertTrue(any("without independently trusted" in item for item in validate(candidate)))

    def test_pinned_provenance_cannot_be_overridden_by_report(self):
        candidate = report()
        candidate["enforceability"][0]["provenance"] = "keel_derived"
        self.assertTrue(any("pinned field artifact" in item for item in validate(candidate)))
        artifact = json.loads(json.dumps(PROVENANCE))
        artifact["fields"].pop("action.name")
        self.assertTrue(any("exactly cover" in item for item in validator.validate(report(), MANIFEST, artifact, SCHEMA, POLICY_SCHEMA)))

    def test_governance_action_cannot_claim_certification_or_auto_allow(self):
        candidate = report(field="context._keel.action_mapping.governance_action_id.value", status="trusted", replacement=None)
        candidate["enforceability"][0]["safe_outcome"] = "auto_allow"
        failures = validate(candidate)
        self.assertTrue(any("interpretation-only" in item for item in failures))
        candidate = report()
        candidate["certified_action_contract_id"] = "forbidden"
        self.assertTrue(any("certified-contract" in item for item in validate(candidate)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
