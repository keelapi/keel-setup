from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

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
        "denied_model": "o4-mini",
        "updated_at": "2026-08-28T10:00:00Z",
    }
    state.update(overrides)
    return state


def _focused_checks():
    return list(setup_state.REQUIRED_PRE_GATE_CHECKS)


def _waiting_state(**overrides):
    state = _valid(
        stage="waiting_for_human",
        changed_paths=["service.py"],
        pre_gate_checks={
            "status": "passed",
            "checks": _focused_checks(),
            "checked_at": "2026-08-28T10:00:00Z",
        },
    )
    state.update(overrides)
    return state


def _write_prepared(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "def route(payload):\n"
        "    return post('/v1/execute', json=payload)\n",
        encoding="utf-8",
    )


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

    def test_legacy_four_check_waiting_state_remains_valid(self):
        state = _waiting_state(
            pre_gate_checks={
                "status": "passed",
                "checks": list(setup_state.LEGACY_PRE_GATE_CHECKS),
                "checked_at": "2026-08-28T10:00:00Z",
            }
        )
        self.assertEqual(setup_state.validate_state(state), [])
        self.assertNotIn("state_f_verified", setup_state.STATE_D_STAGES)

    def test_waiting_stage_requires_changed_paths_and_passing_focused_checks(self):
        no_paths = _waiting_state(changed_paths=[])
        no_checks = _waiting_state(pre_gate_checks=None)
        incomplete = _waiting_state(
            pre_gate_checks={
                "status": "passed",
                "checks": ["syntax_or_compile"],
                "checked_at": "2026-08-28T10:00:00Z",
            }
        )
        self.assertTrue(any("changed path" in item for item in setup_state.validate_state(no_paths)))
        self.assertTrue(any("focused pre-gate" in item for item in setup_state.validate_state(no_checks)))
        self.assertTrue(any("every focused" in item for item in setup_state.validate_state(incomplete)))


class RefusalTest(unittest.TestCase):
    def test_bearer_value_is_refused(self):
        failures = setup_state.refusals(_valid(pinned_skill_ref="Bearer ks_live_abcdefgh"))
        self.assertTrue(any("bearer value" in item for item in failures), failures)

    def test_credential_assignment_is_refused(self):
        failures = setup_state.refusals(_valid(provider="api_key=sk-abcdefgh"))
        self.assertTrue(any("credential assignment" in item for item in failures), failures)

    def test_known_credential_prefix_is_refused(self):
        for value in (
            "ks_live_abcdefgh",
            "sk-abcdefghijkl",
            "ghp_abcdefghijkl",
            " ks_live_abcdefgh",
            "credential value ks_live_abcdefgh",
            "config/ks_live_abcdefgh",
        ):
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

    def test_changed_paths_must_be_repository_relative(self):
        for candidate in ("/private/service.py", "../outside.py"):
            with self.subTest(candidate=candidate):
                failures = setup_state.validate_state(_valid(changed_paths=[candidate]))
                self.assertTrue(any("repository-relative" in item for item in failures), failures)


class CadenceTest(unittest.TestCase):
    def test_first_invocation_has_no_cadence_due(self):
        self.assertEqual(setup_state.due_reviews(1), [])

    def test_second_invocation_resumes_only(self):
        self.assertEqual(setup_state.due_reviews(2), ["resume_earliest_unmet_milestone"])

    def test_fifth_invocation_adds_the_drift_audit(self):
        self.assertEqual(
            setup_state.due_reviews(5, stage="state_d_verified"),
            ["resume_earliest_unmet_milestone", "drift_audit"],
        )

    def test_twentieth_invocation_adds_maintenance(self):
        self.assertEqual(
            setup_state.due_reviews(20, stage="state_d_verified"),
            ["resume_earliest_unmet_milestone", "drift_audit", "maintenance_review"],
        )

    def test_drift_audit_does_not_repeat_before_five_more_invocations(self):
        for count in (6, 7, 8, 9):
            with self.subTest(count=count):
                self.assertNotIn("drift_audit", setup_state.due_reviews(count, last_drift_audit=5, stage="state_d_verified"))
        self.assertIn("drift_audit", setup_state.due_reviews(10, last_drift_audit=5, stage="state_d_verified"))

    def test_maintenance_recurs_on_a_twenty_invocation_cadence(self):
        self.assertNotIn(
            "maintenance_review", setup_state.due_reviews(39, last_maintenance_review=20, stage="state_d_verified")
        )
        self.assertIn(
            "maintenance_review", setup_state.due_reviews(40, last_maintenance_review=20, stage="state_d_verified")
        )

    def test_cadence_is_independent_of_the_other_marker(self):
        due = setup_state.due_reviews(25, last_drift_audit=25, last_maintenance_review=None, stage="state_d_verified")
        self.assertNotIn("drift_audit", due)
        self.assertIn("maintenance_review", due)

    def test_waiting_for_human_defers_count_based_work(self):
        self.assertEqual(
            setup_state.due_reviews(20, stage="waiting_for_human"),
            ["resume_earliest_unmet_milestone"],
        )

    def test_stage_selects_earliest_unmet_phase(self):
        self.assertEqual(setup_state.next_phase("discovery"), "fast_first_run")
        self.assertEqual(setup_state.next_phase("waiting_for_human"), "first_human_gate")
        self.assertEqual(setup_state.next_phase("integration_ready"), "deterministic_verification")
        self.assertEqual(setup_state.next_phase("state_d_verified"), "deep_assurance")


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
            path = root / ".keel" / "setup-state.json"
            report = setup_state.begin(path, root)
            stored = json.loads(path.read_text())
        self.assertEqual(report["continuity"], "lost")
        self.assertEqual(report["invocation_count"], 1)
        self.assertEqual(report["due"], [])
        self.assertTrue(report["state_persisted"])
        self.assertEqual(stored["invocation_count"], 1)
        self.assertIn("prior_run_success", report["does_not_establish"])
        self.assertEqual(report["evidence_level"], "unresolved")

    def test_invalid_json_reports_lost_continuity_without_inferring_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            path.write_text("{not json")
            report = setup_state.begin(path, root)
            stored = json.loads(path.read_text())
        self.assertEqual(report["continuity"], "lost")
        self.assertEqual(report["invocation_count"], 1)
        self.assertEqual(report["stage"], "discovery")
        self.assertEqual(setup_state.validate_state(stored), [])
        self.assertEqual(stored["invocation_count"], 1)
        self.assertEqual(stored["stage"], "discovery")

    def test_refused_file_reports_lost_continuity_and_the_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            path.write_text(json.dumps(_valid(pinned_skill_ref="Bearer ks_live_abcdefgh")))
            report = setup_state.begin(path, root)
            stored = json.loads(path.read_text())
        self.assertEqual(report["continuity"], "lost")
        self.assertEqual(report["invocation_count"], 1)
        self.assertTrue(report["refusals"])
        self.assertEqual(setup_state.validate_state(stored), [])
        self.assertEqual(stored["invocation_count"], 1)
        self.assertEqual(stored["stage"], "discovery")

    def test_valid_file_increments_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            path.write_text(json.dumps(_valid(invocation_count=4, stage="state_d_verified")))
            report = setup_state.begin(path, root)
            again = setup_state.begin(path, root)
            stored = json.loads(path.read_text())
        self.assertEqual(report["continuity"], "resumed")
        self.assertEqual(report["invocation_count"], 5)
        self.assertIn("drift_audit", report["due"])
        self.assertEqual(again["invocation_count"], 6)
        self.assertEqual(stored["invocation_count"], 6)

    def test_waiting_state_resumes_gate_without_first_run_or_drift_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            _write_prepared(root / "service.py")
            path.write_text(json.dumps(_waiting_state(invocation_count=19)))
            report = setup_state.begin(path, root)
        self.assertEqual(report["next_phase"], "first_human_gate")
        self.assertEqual(report["due"], ["resume_earliest_unmet_milestone"])
        self.assertNotIn("drift_audit", report["due"])
        self.assertNotIn("maintenance_review", report["due"])

    def test_mark_human_gate_persists_only_local_preparation_and_resumes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            _write_prepared(root / "service.py")
            (root / "tests").mkdir()
            (root / "tests" / "test_service.py").write_text("def test_route(): pass\n")
            _write_prepared(root / "adapter.py")
            setup_state.begin(path, root)
            failures = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="openai",
                pinned_skill_ref="a" * 40,
                application_revision="b" * 40,
                changed_paths=["service.py", "tests/test_service.py"],
                focused_checks=_focused_checks(),
            )
            stored = json.loads(path.read_text())
            repeated = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="openai",
                pinned_skill_ref="a" * 40,
                changed_paths=["adapter.py"],
                focused_checks=_focused_checks(),
            )
            stored_after_repeat = json.loads(path.read_text())
            resumed = setup_state.begin(path, root)

        self.assertEqual(failures, [])
        self.assertEqual(repeated, [])
        self.assertEqual(stored["stage"], "waiting_for_human")
        self.assertEqual(stored["provider"], "openai")
        self.assertEqual(stored["changed_paths"], ["service.py", "tests/test_service.py"])
        self.assertEqual(
            stored_after_repeat["changed_paths"],
            ["adapter.py", "service.py", "tests/test_service.py"],
        )
        self.assertNotIn("last_verification", stored)
        self.assertEqual(stored["pre_gate_checks"]["status"], "passed")
        self.assertEqual(set(stored["pre_gate_checks"]["checks"]), set(_focused_checks()))
        self.assertEqual(resumed["next_phase"], "first_human_gate")

    def test_mark_human_gate_fails_with_zero_changed_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            setup_state.begin(path, root)
            before = path.read_text()
            failures = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="openai",
                pinned_skill_ref="a" * 40,
                focused_checks=_focused_checks(),
            )
            after = path.read_text()

        self.assertTrue(any("changed-path" in item for item in failures), failures)
        self.assertEqual(after, before)

    def test_mark_human_gate_fails_for_nonexistent_changed_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            setup_state.begin(path, root)
            before = path.read_text()
            failures = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="openai",
                pinned_skill_ref="a" * 40,
                changed_paths=["src/does_not_exist.py"],
                focused_checks=_focused_checks(),
            )
            after = path.read_text()

        self.assertTrue(any("does not exist" in item for item in failures), failures)
        self.assertEqual(after, before)

    def test_mark_human_gate_fails_without_all_focused_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            _write_prepared(root / "service.py")
            setup_state.begin(path, root)
            before = path.read_text()
            failures = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="openai",
                pinned_skill_ref="a" * 40,
                changed_paths=["service.py"],
                focused_checks=["syntax_or_compile", "module_load"],
            )
            after = path.read_text()

        self.assertTrue(any("all focused checks" in item for item in failures), failures)
        self.assertEqual(after, before)

    def test_mark_human_gate_requires_changed_source_to_contain_execute_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            (root / "service.py").write_text("value = 1\n", encoding="utf-8")
            setup_state.begin(path, root)
            failures = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="openai",
                pinned_skill_ref="a" * 40,
                changed_paths=["service.py"],
                focused_checks=_focused_checks(),
            )

        self.assertTrue(any("/v1/execute integration" in item for item in failures), failures)

    def test_mark_human_gate_refuses_an_unchanged_preexisting_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            _write_prepared(root / "service.py")
            subprocess.run(["git", "-C", str(root), "add", "service.py", ".gitignore"], check=True)
            subprocess.run(
                [
                    "git", "-C", str(root), "-c", "user.name=Keel Test",
                    "-c", "user.email=keel-test@example.invalid", "commit", "-qm", "fixture",
                ],
                check=True,
            )
            setup_state.begin(path, root)
            failures = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="openai",
                pinned_skill_ref="a" * 40,
                changed_paths=["service.py"],
                focused_checks=_focused_checks(),
            )

        self.assertTrue(any("not changed in the working tree" in item for item in failures), failures)

    def test_mark_human_gate_does_not_accept_only_a_test_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            _write_prepared(root / "src" / "service.test.ts")
            setup_state.begin(path, root)
            failures = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="openai",
                pinned_skill_ref="a" * 40,
                changed_paths=["src/service.test.ts"],
                focused_checks=_focused_checks(),
            )

        self.assertTrue(any("/v1/execute integration" in item for item in failures), failures)

    def test_waiting_state_with_missing_prepared_file_loses_continuity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            path.write_text(json.dumps(_waiting_state()))
            report = setup_state.begin(path, root)
            stored = json.loads(path.read_text())

        self.assertEqual(report["continuity"], "lost")
        self.assertEqual(report["next_phase"], "fast_first_run")
        self.assertTrue(any("does not exist" in item for item in report["refusals"]))
        self.assertEqual(stored["stage"], "discovery")

    def test_mark_human_gate_refuses_credential_shaped_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            _write_prepared(root / "service.py")
            setup_state.begin(path, root)
            before = path.read_text()
            failures = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="api_key=not-a-real-value",
                pinned_skill_ref="a" * 40,
                changed_paths=["service.py"],
                focused_checks=_focused_checks(),
            )
            after = path.read_text()

        self.assertTrue(any("credential assignment" in item for item in failures), failures)
        self.assertEqual(after, before)

    def test_mark_human_gate_cannot_overwrite_runtime_milestone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            path.write_text(json.dumps(_valid(stage="state_d_verified")))
            failures = setup_state.mark_waiting_for_human(
                path,
                root,
                provider="openai",
                pinned_skill_ref="a" * 40,
            )

        self.assertTrue(any("cannot record" in item for item in failures), failures)

    def test_persisted_state_is_private_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._repo(directory)
            path = root / ".keel" / "setup-state.json"
            report = setup_state.begin(path, root)
            mode = path.stat().st_mode & 0o777
            temporary_files = list(path.parent.glob(f".{path.name}.*.tmp"))
        self.assertTrue(report["state_persisted"])
        self.assertEqual(mode, 0o600)
        self.assertEqual(temporary_files, [])

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
            stored = json.loads(path.read_text())
        self.assertFalse(report["state_path_git_ignored"])
        self.assertFalse(report["state_persisted"])
        self.assertEqual(stored["invocation_count"], 3)


class CommandLineTest(unittest.TestCase):
    def test_main_persists_each_successful_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / ".keel").mkdir()
            (root / ".gitignore").write_text(".keel/\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            path = root / ".keel" / "setup-state.json"
            reports = []
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()) as stdout:
                    code = setup_state.main(["--state", str(path), "--repo-root", str(root)])
                self.assertEqual(code, 0)
                reports.append(json.loads(stdout.getvalue()))
            stored = json.loads(path.read_text())
        self.assertEqual([item["invocation_count"] for item in reports], [1, 2])
        self.assertTrue(all(item["state_persisted"] for item in reports))
        self.assertEqual(stored["invocation_count"], 2)

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

    def test_write_failure_exits_nonzero_without_claiming_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / ".keel").mkdir()
            (root / ".gitignore").write_text(".keel/\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            path = root / ".keel" / "setup-state.json"
            with (
                mock.patch.object(
                    setup_state,
                    "write_state_atomically",
                    side_effect=OSError("simulated write failure"),
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = setup_state.main(["--state", str(path), "--repo-root", str(root)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["state_persisted"])

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

    def test_mark_human_gate_cli_requires_preparation_and_focused_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / ".keel").mkdir()
            (root / ".gitignore").write_text(".keel/\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            path = root / ".keel" / "setup-state.json"
            _write_prepared(root / "service.py")
            setup_state.begin(path, root)
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = setup_state.main(
                    [
                        "--state", str(path),
                        "--repo-root", str(root),
                        "--mark-waiting-for-human",
                        "--provider", "openai",
                        "--pinned-skill-ref", "a" * 40,
                        "--changed-path", "service.py",
                        "--focused-check", "syntax_or_compile",
                        "--focused-check", "fail_closed_integration_review",
                        "--focused-check", "adjacent_bypass_search",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "recorded local preparation at first human gate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
