from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_execute_contract.py"
SPEC = importlib.util.spec_from_file_location("check_execute_contract", CHECKER)
check_execute_contract = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(check_execute_contract)


class ExecuteRequestContractTest(unittest.TestCase):
    def test_shipped_helper_matches_the_pinned_api_schema(self):
        check_execute_contract.validate()

    def test_extra_operation_field_is_refused_even_if_the_snapshot_is_rehashed(self):
        source = check_execute_contract.HELPER.read_text(encoding="utf-8")
        mutated = source.replace(
            '            "provider": provider,\n',
            '            "provider": provider,\n            "operation": "generate.text",\n',
            1,
        )
        self.assertNotEqual(source, mutated)
        with tempfile.TemporaryDirectory() as directory:
            helper = pathlib.Path(directory) / "verify_execute.py"
            helper.write_text(mutated, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "helper request keys changed"):
                check_execute_contract.validate(helper_path=helper)

    def test_missing_required_input_is_refused(self):
        source = check_execute_contract.HELPER.read_text(encoding="utf-8")
        mutated = source.replace(
            '            "input": {"messages": [{"role": "user", "content": "Reply with OK."}]},\n',
            "",
            1,
        )
        self.assertNotEqual(source, mutated)
        with tempfile.TemporaryDirectory() as directory:
            helper = pathlib.Path(directory) / "verify_execute.py"
            helper.write_text(mutated, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "helper request keys changed"):
                check_execute_contract.validate(helper_path=helper)

    def test_snapshot_drift_from_a_supplied_openapi_is_refused(self):
        contract = json.loads(check_execute_contract.CONTRACT.read_text(encoding="utf-8"))
        openapi = {
            "components": {
                "schemas": {
                    "UnifiedExecuteRequest": contract["schema"],
                }
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact = pathlib.Path(directory) / "openapi.json"
            artifact.write_text(json.dumps(openapi), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "OpenAPI digest differs"):
                check_execute_contract.validate(openapi_path=artifact)


if __name__ == "__main__":
    unittest.main(verbosity=2)
