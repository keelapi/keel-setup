from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "keel-setup" / "scripts" / "fast_first_run.py"
INVENTORY_SCRIPT = ROOT / "keel-setup" / "scripts" / "inventory.py"
FIXTURES_PATH = pathlib.Path(__file__).with_name("fixtures") / "fast_first_run_cases.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fast = _load("fast_first_run", SCRIPT)
inventory = _load("fast_first_run_inventory", INVENTORY_SCRIPT)
fixtures = _load("fast_first_run_fixtures", FIXTURES_PATH)


def _git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class DeterministicFastFirstRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle_temp = tempfile.TemporaryDirectory()
        cls.bundle = pathlib.Path(cls.bundle_temp.name)
        for relative in (
            "keel-setup/scripts/inventory.py",
            "keel-setup/scripts/setup_state.py",
            "keel-setup/reference/setup-state.schema.json",
            "shared/scripts/schema_validation.py",
        ):
            target = cls.bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        (cls.bundle / "scripts").mkdir()
        (cls.bundle / "scripts" / "check_release_bundle.py").write_text(
            "print('PASS: public setup bundle (fixture)')\n", encoding="utf-8"
        )
        (cls.bundle / "SOURCE.json").write_text(
            json.dumps({"public_release_version": "fixture", "product_source_sha256": "a" * 64}),
            encoding="utf-8",
        )
        _git(cls.bundle, "init", "-q")
        _git(cls.bundle, "remote", "add", "origin", "https://github.com/keelapi/keel-setup.git")
        _git(cls.bundle, "add", ".")
        _git(
            cls.bundle, "-c", "user.name=Keel Test", "-c", "user.email=keel-test@example.invalid",
            "commit", "-qm", "fixture bundle",
        )
        cls.bundle_sha = _git(cls.bundle, "rev-parse", "HEAD")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.bundle_temp.cleanup()

    def _repo(self, app_source: str = fixtures.GOLDEN_APP, *, with_test: bool = True) -> pathlib.Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        (root / "app.py").write_text(app_source, encoding="utf-8")
        (root / "requirements.txt").write_text("openai>=1.0.0\n", encoding="utf-8")
        (root / ".gitignore").write_text(".venv/\n__pycache__/\n*.py[cod]\n", encoding="utf-8")
        if with_test:
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests" / "test_app.py").write_text(fixtures.GOLDEN_TEST, encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "add", ".")
        _git(
            root, "-c", "user.name=Keel Test", "-c", "user.email=keel-test@example.invalid",
            "commit", "-qm", "fixture app",
        )
        return root

    def _run(self, root: pathlib.Path) -> dict[str, object]:
        return fast.run_pipeline(self.bundle, self.bundle_sha, root)

    def test_trivial_openai_responses_summarizer_is_ready_for_human(self):
        root = self._repo()
        result = self._run(root)

        self.assertEqual(result["outcome"], "ready_for_human", result)
        self.assertEqual(result["seam"]["sdk_shape"], fast.GOLDEN_SHAPE)
        self.assertEqual(result["seam"]["injection_hook"], "client")
        self.assertEqual(result["setup_state"]["stage"], "waiting_for_human")
        self.assertEqual(json.loads((root / ".keel" / "setup-state.json").read_text())["stage"], "waiting_for_human")

    def test_client_none_signature_is_preserved(self):
        root = self._repo()
        before = ast.parse((root / "app.py").read_text()).body
        before_fn = next(node for node in before if isinstance(node, ast.FunctionDef) and node.name == "summarize")
        result = self._run(root)
        after = ast.parse((root / "app.py").read_text()).body
        after_fn = next(node for node in after if isinstance(node, ast.FunctionDef) and node.name == "summarize")

        self.assertEqual(result["outcome"], "ready_for_human", result)
        self.assertEqual(fast.callable_signature(before_fn), fast.callable_signature(after_fn))
        client = next(item for item in result["seam"]["callable_signature"]["parameters"] if item["name"] == "client")
        self.assertEqual(client, {"name": "client", "kind": "keyword_only", "default": None})

    def test_coupled_protocol_double_is_adapted_and_focused_test_passes(self):
        root = self._repo()
        result = self._run(root)
        test_source = (root / "tests" / "test_app.py").read_text()

        self.assertEqual(result["outcome"], "ready_for_human", result)
        self.assertIn("FakeClient", test_source)
        self.assertIn("https://api.keelapi.com/v1/execute", test_source)
        self.assertEqual(
            result["validation"]["focused_compatibility_test"],
            "tests.test_app.SummarizerTests.test_sends_text_to_the_selected_model",
        )

    def test_retained_if_else_block_is_copied_verbatim_and_compiles(self):
        root = self._repo(fixtures.RETAINED_IF_ELSE_APP)
        retained = '''    if source.startswith("Note: "):
        source = source.removeprefix("Note: ")
    else:
        source = source'''

        result = self._run(root)

        self.assertEqual(result["outcome"], "ready_for_human", result)
        after = (root / "app.py").read_text(encoding="utf-8")
        self.assertIn(retained, after)
        compile(after, "app.py", "exec")

    def test_retained_try_except_block_is_copied_verbatim_and_compiles(self):
        root = self._repo(fixtures.RETAINED_TRY_EXCEPT_APP)
        retained = '''    try:
        too_long = len(source) > MAX_INPUT_CHARACTERS
    except TypeError as exc:
        raise ValueError("Text must support length checks.") from exc'''

        result = self._run(root)

        self.assertEqual(result["outcome"], "ready_for_human", result)
        after = (root / "app.py").read_text(encoding="utf-8")
        self.assertIn(retained, after)
        compile(after, "app.py", "exec")

    def test_virtualenv_tests_do_not_consume_coupled_test_budget(self):
        root = self._repo()
        for index in range(fast.MAX_TEST_FILES + 10):
            path = root / ".venv" / "lib" / f"test_irrelevant_{index:03d}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "def test_irrelevant():\n    summarize('x', client=object())\n",
                encoding="utf-8",
            )

        result = self._run(root)

        self.assertEqual(result["outcome"], "ready_for_human", result)
        self.assertEqual(result["seam"]["directly_coupled_test"], "tests/test_app.py")

    def test_ready_result_emits_exact_bounded_diff_and_stats(self):
        root = self._repo()

        result = self._run(root)

        self.assertEqual(result["outcome"], "ready_for_human", result)
        evidence = result["diff"]
        self.assertFalse(evidence["diff_truncated"])
        self.assertLessEqual(len(evidence["unified_diff"].encode("utf-8")), fast.MAX_DIFF_BYTES)
        self.assertIsInstance(evidence["whitespace_only_changed_lines"], int)
        checked = subprocess.run(
            ["git", "-C", str(root), "apply", "--check", "--reverse", "-"],
            input=evidence["unified_diff"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        numstat = _git(root, "diff", "--numstat", "--", *result["validation"]["changed_paths"])
        expected = {
            path: {"insertions": int(insertions), "deletions": int(deletions)}
            for insertions, deletions, path in (line.split("\t") for line in numstat.splitlines())
        }
        self.assertEqual(evidence["per_file"], expected)

    def test_whitespace_only_changed_line_count_is_exact(self):
        before = "def example():\n    if ready:\n        return True\n"
        after = "def example():\n    if ready:\n            return True\n"

        insertions, deletions, whitespace_only = fast._line_change_stats(before, after)

        self.assertEqual((insertions, deletions, whitespace_only), (1, 1, 2))

    def test_oversized_diff_is_explicitly_truncated(self):
        root = self._repo()
        result = self._run(root)
        allowed = set(result["validation"]["changed_paths"])
        originals = {
            pathlib.PurePosixPath(relative): subprocess.run(
                ["git", "-C", str(root), "show", f"HEAD:{relative}"],
                capture_output=True,
                check=True,
            ).stdout
            for relative in allowed
        }
        with (root / "app.py").open("a", encoding="utf-8") as handle:
            for index in range(5_000):
                handle.write(f"# bounded-diff-fixture-{index:04d}\n")

        evidence = fast.build_diff_evidence(root, allowed, originals)

        self.assertTrue(evidence["diff_truncated"])
        self.assertLessEqual(len(evidence["unified_diff"].encode("utf-8")), fast.MAX_DIFF_BYTES)
        self.assertEqual(evidence["max_bytes"], fast.MAX_DIFF_BYTES)

    def test_custom_base_url_is_safe_fallback(self):
        root = self._repo(fixtures.CUSTOM_BASE_URL_APP)
        result = self._run(root)
        self.assertEqual(result["outcome"], "unsupported_shape")
        self.assertEqual(_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")

    def test_streaming_responses_is_safe_fallback(self):
        root = self._repo(fixtures.STREAMING_APP)
        result = self._run(root)
        self.assertEqual(result["outcome"], "unsupported_shape")
        self.assertEqual(_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")

    def test_two_provider_paths_are_ambiguous(self):
        root = self._repo(fixtures.TWO_PROVIDER_PATHS_APP)
        result = self._run(root)
        self.assertEqual(result["outcome"], "ambiguous", result)
        self.assertEqual(_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")

    def test_unknown_wrapper_requires_model_review(self):
        root = self._repo(fixtures.UNKNOWN_WRAPPER_APP)
        result = self._run(root)
        self.assertEqual(result["outcome"], "model_review_required")
        self.assertEqual(_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")

    def test_repository_instructions_require_model_review_before_editing(self):
        root = self._repo()
        (root / "AGENTS.md").write_text("Repository-specific instructions.\n", encoding="utf-8")
        _git(root, "add", "AGENTS.md")
        _git(
            root, "-c", "user.name=Keel Test", "-c", "user.email=keel-test@example.invalid",
            "commit", "-qm", "add instructions",
        )
        result = self._run(root)
        self.assertEqual(result["outcome"], "model_review_required")
        self.assertIn("repository instructions", result["reason"])
        self.assertEqual(_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")

    def _prepared(self):
        root = self._repo()
        report = inventory.fast_inspect(root)
        seam = fast.discover_golden_seam(root, inventory)
        before = (root / "app.py").read_text()
        after, test_after = fast.generate_adapter(root, seam)
        (root / "app.py").write_text(after, encoding="utf-8")
        assert seam.coupled_test and test_after is not None
        (root / seam.coupled_test).write_text(test_after, encoding="utf-8")
        with (root / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write(".keel/setup-state.json\n")
        self.assertEqual(report["decision"], "single_narrow_seam")
        return root, seam, before, after, {"app.py", "tests/test_app.py", ".gitignore"}

    def _assert_validation_outcome(self, mutation: str, expected: str = "validation_failed") -> None:
        root, seam, before, after, allowed = self._prepared()
        mutated = after + mutation
        (root / "app.py").write_text(mutated, encoding="utf-8")
        with self.assertRaises(fast.PipelineFailure) as raised:
            fast.validate_patch(root, seam, before, mutated, allowed, inventory)
        self.assertEqual(raised.exception.outcome, expected)

    def test_syntax_failure_is_validation_failure(self):
        self._assert_validation_outcome(fixtures.SYNTAX_FAILURE)

    def test_signature_change_is_validation_failure(self):
        root, seam, before, after, allowed = self._prepared()
        mutated = after.replace("client: Any | None = None", "client: Any | None = 'changed'", 1)
        (root / "app.py").write_text(mutated, encoding="utf-8")
        with self.assertRaises(fast.PipelineFailure) as raised:
            fast.validate_patch(root, seam, before, mutated, allowed, inventory)
        self.assertEqual(raised.exception.outcome, "unsafe_contract_change")

    def test_provider_fallback_is_validation_failure(self):
        self._assert_validation_outcome(fixtures.PROVIDER_FALLBACK)

    def test_proxy_route_is_validation_failure(self):
        self._assert_validation_outcome(fixtures.PROXY_ROUTE)

    def test_direct_provider_path_remaining_is_validation_failure(self):
        self._assert_validation_outcome(fixtures.DIRECT_PROVIDER_REMAINS)

    def test_wrong_bundle_sha_is_untrusted_and_does_not_edit(self):
        root = self._repo()
        result = fast.run_pipeline(self.bundle, "0" * 40, root)
        self.assertEqual(result["outcome"], "untrusted_bundle")
        self.assertEqual(_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")

    def test_generated_syntax_failure_rolls_back_every_attempted_edit(self):
        root = self._repo()
        original_generator = fast.generate_adapter

        def invalid_generator(repo, seam):
            application, test = original_generator(repo, seam)
            return application + fixtures.SYNTAX_FAILURE, test

        with mock.patch.object(fast, "generate_adapter", side_effect=invalid_generator):
            result = self._run(root)

        self.assertEqual(result["outcome"], "validation_failed")
        self.assertEqual(_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertFalse((root / ".keel" / "setup-state.json").exists())

    def test_helper_never_reads_environment_credentials(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        attributes = {
            f"{node.value.id}.{node.attr}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        self.assertNotIn("os.environ", attributes)
        self.assertNotIn("os.getenv", attributes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
