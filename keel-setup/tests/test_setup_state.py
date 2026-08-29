from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "setup_state.py"
SPEC = importlib.util.spec_from_file_location("setup_state", SCRIPT)
setup_state = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(setup_state)


def _valid(**overrides):
    state = {
        "schema_version": "1.0",
        "invocation_count": 3,
        "stage": "integration_ready",
        "provider": "openai",
        "allowed_model": "gpt-4o-mini",
        "denied_model": "gpt-4o",
        "updated_at": "2026-08-28T10:00:00Z",
    }
    state.update(overrides)
    return state


class SchemaTest(unittest.TestCase):
    def test_shipped_shape_validates(self):
        self.assertEqual(setup_state.validate_state(_valid()), [])

    def test_unknown_property_is_refused(self):
        failures = setup_state.validate_state(_valid(dashboard_session="abc"))
        self.assertTrue(any("unknown property" in item for item in failures), failures)

    def test_wrong_schema_version_is_refused(self):
        failures = setup_state.validate_state(_valid(schema_version="2.0"))
        self.assertTrue(failures)

    def test_cadence_marker_cannot_lead_the_count(self):
        failures = setup_state.validate_state(
            _valid(invocation_count=3, last_drift_audit_invocation=9)
        )
        self.assertTrue(any("ahead of invocation_count" in item for item in failures), failures)

    def test_state_f_stage_parses_but_is_not_supported_here(self):
        self.assertEqual(setup_state.validate_state(_valid(stage="state_f_verified")), [])
        self.assertNotIn("state_f_verified", setup_state.STATE_D_STAGES)


class RefusalTest(unittest.TestCase):
    def test_bearer_value_is_refused(self):
        failures = setup_state.refusals(_valid(pinned_skill_ref="Bearer ks_live_abcdefgh"))
        self.assertTrue(any("bearer value" in item for item in failures), failures)

    def test_credential_assignment_is_refused(self):
        failures = setup_state.refusals(_valid(provider="api_key=sk-abcdefgh"))
        self.assertTrue(any("credential assignment" in item for item in failures), failures)

    def test_known_credential_prefix_is_refused(self):
        for value in ("ks_live_abcdefgh", "sk-abcdefghijkl", "ghp_abcdefghijkl"):
            with self.subTest(value=value):
                failures = setup_state.refusals(_valid(pinned_skill_ref=value))
                self.assertTrue(any("credential prefix" in item for item in failures), failures)

    def test_raw_prompt_or_response_content_is_refused(self):
        failures = setup_state.refusals(_valid(provider="x" * 600))
        self.assertTrue(any("longer than" in item for item in failures), failures)

    def test_mapping_authority_claim_is_refused_by_name(self):
        failures = setup_state.refusals(_valid(active_mapping_hash="abc"))
        self.assertTrue(any("mapping authority" in item for item in failures), failures)

    def test_ordinary_repository_paths_are_not_false_positives(self):
        # A credential-shaped check that rejected these would make the helper unusable.
        state = _valid(
            changed_paths=[
                "src/api_keys.py",
                "tests/monkeypatch_helpers.py",
                "config/secrets_loader.py",
                "docs/token-bucket.md",
            ]
        )
        self.assertEqual(setup_state.refusals(state), [])


class CadenceTest(unittest.TestCase):
    def test_first_invocation_has_no_cadence_due(self):
        self.assertEqual(setup_state.due_reviews(1), [])

    def test_second_invocation_resumes_only(self):
        self.assertEqual(setup_state.due_reviews(2), ["resume_earliest_unmet_milestone"])

    def test_fifth_invocation_adds_the_drift_audit(self):
        self.assertEqual(
            setup_state.due_reviews(5),
            ["resume_earliest_unmet_milestone", "drift_audit"],
        )

    def test_twentieth_invocation_adds_maintenance(self):
        self.assertEqual(
            setup_state.due_reviews(20),
            ["resume_earliest_unmet_milestone", "drift_audit", "maintenance_review"],
        )

    def test_drift_audit_does_not_repeat_before_five_more_invocations(self):
        for count in (6, 7, 8, 9):
            with self.subTest(count=count):
                self.assertNotIn("drift_audit", setup_state.due_reviews(count, last_drift_audit=5))
        self.assertIn("drift_audit", setup_state.due_reviews(10, last_drift_audit=5))

    def test_maintenance_recurs_on_a_twenty_invocation_cadence(self):
        self.assertNotIn(
            "maintenance_review", setup_state.due_reviews(39, last_maintenance_review=20)
        )
        self.assertIn(
            "maintenance_review", setup_state.due_reviews(40, last_maintenance_review=20)
        )

    def test_cadence_is_independent_of_the_other_marker(self):
        due = setup_state.due_reviews(25, last_drift_audit=25, last_maintenance_review=None)
        self.assertNotIn("drift_audit", due)
        self.assertIn("maintenance_review", due)


class BeginTest(unittest.TestCase):
    def _repo(self, directory: str) -> pathlib.Path:
        root = pathlib.Path(directory)
        (root / ".keel").mkdir()
        (root / ".gitignore").write_text(".keel/\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        return root

    def test_missing_file_reports_lost_continuity_and_starts_at_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            report = setup_state.begin(root / ".keel" / "setup-state.json", root)
        self.assertEqual(report["continuity"], "lost")
        self.assertEqual(report["invocation_count"], 1)
        self.assertEqual(report["due"], [])
        self.assertIn("prior_run_success", report["does_not_establish"])
        self.assertEqual(report["evidence_level"], "unresolved")

    def test_invalid_json_reports_lost_continuity_without_inferring_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            path.write_text("{not json")
            report = setup_state.begin(path, root)
        self.assertEqual(report["continuity"], "lost")
        self.assertEqual(report["invocation_count"], 1)
        self.assertEqual(report["stage"], "discovery")

    def test_refused_file_reports_lost_continuity_and_the_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            path.write_text(json.dumps(_valid(pinned_skill_ref="Bearer ks_live_abcdefgh")))
            report = setup_state.begin(path, root)
        self.assertEqual(report["continuity"], "lost")
        self.assertEqual(report["invocation_count"], 1)
        self.assertTrue(report["refusals"])

    def test_valid_file_increments_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            path.write_text(json.dumps(_valid(invocation_count=4, stage="state_d_verified")))
            report = setup_state.begin(path, root)
            again = setup_state.begin(path, root)
        self.assertEqual(report["continuity"], "resumed")
        self.assertEqual(report["invocation_count"], 5)
        self.assertIn("drift_audit", report["due"])
        # begin() never writes, so the count advances only when the caller stores it.
        self.assertEqual(again["invocation_count"], 5)

    def test_state_f_stage_is_reported_unsupported_on_this_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            path.write_text(json.dumps(_valid(stage="state_f_verified")))
            report = setup_state.begin(path, root)
        self.assertFalse(report["stage_supported_on_this_revision"])

    def test_untracked_ignore_status_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / ".keel").mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            path = root / ".keel" / "setup-state.json"
            path.write_text(json.dumps(_valid()))
            report = setup_state.begin(path, root)
        self.assertFalse(report["state_path_git_ignored"])


class CommandLineTest(unittest.TestCase):
    def test_unignored_state_path_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / ".keel").mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            path = root / ".keel" / "setup-state.json"
            path.write_text(json.dumps(_valid()))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = setup_state.main(["--state", str(path), "--repo-root", str(root)])
        self.assertEqual(code, 1)

    def test_validate_only_rejects_a_refused_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "setup-state.json"
            path.write_text(json.dumps(_valid(active_mapping_hash="abc")))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = setup_state.main(["--validate-only", "--state", str(path)])
        self.assertEqual(code, 1)

    def test_validate_only_accepts_the_shipped_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "setup-state.json"
            path.write_text(json.dumps(_valid()))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = setup_state.main(["--validate-only", "--state", str(path)])
        self.assertEqual(code, 0)

    def test_helper_takes_no_credential_argument(self):
        source = SCRIPT.read_text()
        self.assertNotIn("KEEL_API_KEY", source)
        self.assertNotIn("os.environ", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
