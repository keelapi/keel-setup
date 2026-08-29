from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "inventory.py"
SPEC = importlib.util.spec_from_file_location("inventory", SCRIPT)
inventory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(inventory)


def report():
    return {
        "schema_version": "1.0",
        "report_basis": "source_inventory",
        "application_revision": None,
        "paths": [{
            "path": "server.py", "line": 1, "surface": "mcp", "signal": "MCP registration",
            "status": "unresolved", "evidence_level": "source_inspected", "confidence": "medium",
            "uncertainties": ["source signal is not trusted runtime semantics"],
        }],
        "does_not_establish": ["deployment", "runtime_frequency", "trusted_semantics", "downstream_effect", "bypass_absence", "whole_application_protection", "independent_verification"],
    }


class InventoryTest(unittest.TestCase):
    def test_inventory_is_source_only_and_reports_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "server.py").write_text("from mcp import FastMCP\nstripe.refunds.create(amount=amount)\n")
            result = inventory.inspect(root)
        self.assertEqual(result["report_basis"], "source_inventory")
        self.assertTrue({item["surface"] for item in result["paths"]} >= {"mcp", "payment"})
        self.assertTrue(all(item["status"] == "unresolved" and item["evidence_level"] == "source_inspected" for item in result["paths"]))
        self.assertIn("bypass_absence", result["does_not_establish"])
        self.assertEqual(inventory.validate_coverage(result), [])

    def test_source_only_entry_cannot_claim_protected_or_governed_routed(self):
        for status in ("protected", "governed_routed"):
            candidate = report()
            candidate["paths"][0]["status"] = status
            failures = inventory.validate_coverage(candidate)
            self.assertTrue(any("requires per-entry runtime_observed" in item for item in failures))
            candidate["paths"][0]["evidence_level"] = "runtime_observed"
            self.assertEqual(inventory.validate_coverage(candidate), [])

    def test_intentionally_unprotected_requires_human_assertion(self):
        candidate = report()
        candidate["paths"][0]["status"] = "intentionally_unprotected"
        self.assertTrue(inventory.validate_coverage(candidate))
        candidate["paths"][0]["evidence_level"] = "human_asserted"
        self.assertEqual(inventory.validate_coverage(candidate), [])

    def test_verified_protected_is_not_in_schema(self):
        candidate = report()
        candidate["paths"][0]["status"] = "verified_protected"
        self.assertTrue(any("allowed enum" in item for item in inventory.validate_coverage(candidate)))

    def test_full_schema_constraints_and_duplicate_identity_are_enforced(self):
        cases = []
        missing = report(); del missing["paths"][0]["line"]; cases.append(missing)
        unknown = report(); unknown["paths"][0]["extra"] = True; cases.append(unknown)
        wrong_type = report(); wrong_type["paths"][0]["line"] = "one"; cases.append(wrong_type)
        too_many = report(); too_many["paths"][0]["uncertainties"] = [str(index) for index in range(21)]; cases.append(too_many)
        empty_signal = report(); empty_signal["paths"][0]["signal"] = ""; cases.append(empty_signal)
        duplicate = report(); duplicate["paths"].append(dict(duplicate["paths"][0])); cases.append(duplicate)
        for index, candidate in enumerate(cases):
            with self.subTest(index=index):
                self.assertTrue(inventory.validate_coverage(candidate))

    def test_absolute_parent_paths_and_missing_limits_are_rejected(self):
        for path in ("/private/example.py", "../outside.py"):
            candidate = report()
            candidate["paths"][0]["path"] = path
            self.assertTrue(inventory.validate_coverage(candidate))
        candidate = report()
        candidate["does_not_establish"].remove("bypass_absence")
        self.assertTrue(any("missing required evidence boundaries" in item for item in inventory.validate_coverage(candidate)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
