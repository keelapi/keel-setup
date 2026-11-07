from __future__ import annotations

import ast
import importlib.util
import inspect
import io
import json
import os
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
    def test_fallback_release_checker_rejects_bytecode_and_cache_entries(self):
        checker = _load("fallback_bundle_check", ROOT / "scripts/check_release_bundle.py")
        for relative in ("shared/scripts/__pycache__/schema_validation.pyc", "shared/scripts/schema_validation.pyc",
                         "shared/scripts/__pycache__"):
            with tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.name == "__pycache__":
                    path.mkdir()
                else:
                    path.write_bytes(b"unmeasured executable cache")
                with mock.patch.object(checker, "ROOT", root), self.assertRaisesRegex(ValueError, "caches"):
                    checker._files()

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle_temp = tempfile.TemporaryDirectory()
        cls.bundle = pathlib.Path(cls.bundle_temp.name)
        for relative in (
            "keel-setup/scripts/fast_first_run.py",
            "keel-setup/scripts/authoring_context.py",
            "keel-setup/scripts/inventory.py",
            "keel-setup/scripts/setup_state.py",
            "keel-setup/reference/setup-state.schema.json",
            "keel-policy/scripts/consume_authoring_context.py",
            "keel-policy/scripts/validate_enforceability_report.py",
            "shared/scripts/schema_validation.py",
            "shared/scripts/validate_feedback_report.py",
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

    def test_nested_bundle_is_rejected_before_checker_or_helper_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary) / "release"
            _git(pathlib.Path(temporary), "clone", "-q", str(self.bundle), str(parent))
            _git(parent, "remote", "set-url", "origin", "https://github.com/keelapi/keel-setup.git")
            (parent / ".git/info/exclude").write_text(".worktrees/\n", encoding="utf-8")
            child = parent / ".worktrees/fake"
            (child / "scripts").mkdir(parents=True)
            marker = pathlib.Path(temporary) / "checker-ran"
            (child / "scripts/check_release_bundle.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                "print('PASS: public setup bundle (fake)')\n", encoding="utf-8",
            )
            shutil.copy2(parent / "SOURCE.json", child / "SOURCE.json")
            alias = pathlib.Path(temporary) / "child-alias"
            alias.symlink_to(child, target_is_directory=True)
            self.assertEqual(_git(child, "rev-parse", "HEAD"), self.bundle_sha)
            self.assertEqual(_git(child, "status", "--porcelain=v1", "--untracked-files=all"), "")
            for selected in (child, alias, child / "scripts/.."):
                with self.subTest(selected=selected):
                    with self.assertRaisesRegex(fast.PipelineFailure, "worktree root"):
                        fast.verify_release(selected, self.bundle_sha)
                    with mock.patch.object(fast, "_load_module") as load:
                        result = fast.run_pipeline(selected, self.bundle_sha, parent)
                    self.assertEqual(result["outcome"], "untrusted_bundle", result)
                    load.assert_not_called()
                    self.assertFalse(marker.exists())

    def test_release_root_aliases_remain_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            alias = pathlib.Path(temporary) / "release-alias"
            alias.symlink_to(self.bundle, target_is_directory=True)
            relative = pathlib.Path(os.path.relpath(self.bundle))
            for selected in (self.bundle, alias, relative):
                with self.subTest(selected=selected):
                    self.assertEqual(fast.verify_release(selected, self.bundle_sha)["status"], "trusted")

    def test_linked_worktree_root_remains_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary) / "release"
            _git(pathlib.Path(temporary), "clone", "-q", str(self.bundle), str(parent))
            _git(parent, "remote", "set-url", "origin", "https://github.com/keelapi/keel-setup.git")
            worktree = pathlib.Path(temporary) / "linked"
            _git(parent, "worktree", "add", "--detach", str(worktree), self.bundle_sha)
            self.assertTrue((worktree / ".git").is_file())
            self.assertEqual(fast.verify_release(worktree, self.bundle_sha)["status"], "trusted")

    def test_release_root_with_trailing_whitespace_remains_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / "release \n"
            _git(pathlib.Path(temporary), "clone", "-q", str(self.bundle), str(root))
            _git(root, "remote", "set-url", "origin", "https://github.com/keelapi/keel-setup.git")
            self.assertEqual(fast.verify_release(root, self.bundle_sha)["status"], "trusted")

    def test_trivial_openai_responses_summarizer_is_ready_for_human(self):
        root = self._repo()
        result = self._run(root)

        self.assertEqual(result["outcome"], "ready_for_human", result)
        self.assertEqual(result["seam"]["sdk_shape"], fast.GOLDEN_SHAPE)
        self.assertEqual(result["seam"]["injection_hook"], "client")
        self.assertEqual(result["setup_state"]["stage"], "waiting_for_human")
        self.assertEqual(json.loads((root / ".keel" / "setup-state.json").read_text())["stage"], "waiting_for_human")

    def test_dirty_checkout_stops_before_discovery_or_edits(self):
        for untracked in (False, True):
            with self.subTest(untracked=untracked):
                root = self._repo()
                path = root / ("notes.txt" if untracked else "app.py")
                path.write_text("Uncommitted human work.\n", encoding="utf-8")
                before = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
                with mock.patch.object(fast, "discover_golden_seam") as discover, \
                     mock.patch.object(fast, "generate_adapter") as generate:
                    result = self._run(root)
                self.assertEqual(result["outcome"], "dirty_checkout", result)
                self.assertEqual(result["changed_paths"], [])
                discover.assert_not_called()
                generate.assert_not_called()
                self.assertFalse((root / ".keel").exists())
                self.assertEqual(path.read_text(), "Uncommitted human work.\n")
                self.assertEqual(_git(root, "status", "--porcelain=v1", "--untracked-files=all"), before)
                self.assertIn("total_ms", result["timings"])

    def test_pre_gate_validation_does_not_execute_target_initialization(self):
        for entrypoint in ("tests/__init__.py", "app.py", "sitecustomize.py"):
            with self.subTest(entrypoint=entrypoint):
                root = self._repo()
                marker = root.parent / (root.name + "-host-write")
                self.addCleanup(marker.unlink, missing_ok=True)
                payload = f"\nopen({str(marker)!r}, 'w').write('target code ran')\n"
                path = root / entrypoint
                path.write_text((path.read_text() if path.exists() else "") + payload)
                _git(root, "add", ".")
                _git(root, "-c", "user.name=Keel Test", "-c", "user.email=keel-test@example.invalid",
                     "commit", "-qm", "target initialization fixture")

                with mock.patch.object(fast, "_run", wraps=fast._run) as commands:
                    result = self._run(root)

                self.assertFalse(marker.exists(), entrypoint)
                self.assertEqual(result["outcome"], "ready_for_human", result)
                for command in commands.call_args_list:
                    argv = command.args[0]
                    self.assertNotIn("unittest", argv)
                    if command.kwargs["cwd"] == root:
                        self.assertEqual(argv[0], "git", argv)

    def test_checker_hidden_from_git_status_never_executes(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                bundle = root / "bundle"
                _git(root, "clone", "-q", str(self.bundle), str(bundle))
                _git(bundle, "remote", "set-url", "origin", "https://github.com/keelapi/keel-setup.git")
                marker = root / "checker-executed"
                (bundle / "scripts/check_release_bundle.py").write_text(
                    f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                    "print('PASS: public setup bundle (forged)')\n"
                )
                _git(bundle, "update-index", flag, "scripts/check_release_bundle.py")
                self.assertEqual(_git(bundle, "status", "--porcelain"), "")
                with self.assertRaises(fast.PipelineFailure):
                    fast.verify_release(bundle, self.bundle_sha)
                self.assertFalse(marker.exists())

    def test_documented_orchestrator_ignores_pythonpath_startup_hook(self):
        root = self._repo()
        marker = root.parent / (root.name + "-startup-executed")
        self.addCleanup(marker.unlink, missing_ok=True)
        (root / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        )
        _git(root, "add", ".")
        _git(root, "-c", "user.name=Keel Test", "-c", "user.email=keel-test@example.invalid",
             "commit", "-qm", "startup hook fixture")
        with mock.patch.object(fast, "_run", wraps=fast._run) as calls:
            self.assertEqual(self._run(self._repo())["outcome"], "ready_for_human")
        checker_calls = [call.args[0] for call in calls.call_args_list
                         if any(str(arg).endswith("check_release_bundle.py") for arg in call.args[0])]
        self.assertTrue(checker_calls)
        self.assertTrue(all(command[1] == "-I" for command in checker_calls))
        # Follow the published command shape in a fresh process so Python's own
        # startup, rather than an in-process mock, is under test.
        result = subprocess.run(
            [sys.executable, "-I", str(self.bundle / "keel-setup/scripts/fast_first_run.py"),
             "--bundle", str(self.bundle), "--bundle-sha", self.bundle_sha, "--repo", str(root)],
            cwd=root, env={**os.environ, "PYTHONPATH": str(root)},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["outcome"], "ready_for_human")
        self.assertFalse(marker.exists())

    def test_gate_implementation_matches_standalone_credential_entrypoints(self):
        for name in ("verify_execute.py", "authoring_context.py", "prepare_voice.py"):
            module = _load("fast_gate_" + name.removesuffix(".py"), SCRIPT.with_name(name))
            self.assertEqual(inspect.getsource(fast._verify_pinned_release),
                             inspect.getsource(module._verify_pinned_release))

    def test_local_helpers_leave_the_authenticated_bundle_clean(self):
        for relative in (
            "keel-setup/scripts/inventory.py", "keel-setup/scripts/setup_state.py",
            "keel-policy/scripts/consume_authoring_context.py",
            "keel-policy/scripts/validate_enforceability_report.py",
            "shared/scripts/validate_feedback_report.py",
        ):
            with self.subTest(relative=relative):
                result = subprocess.run(
                    [sys.executable, "-I", str(self.bundle / relative), "--help"],
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(list(self.bundle.rglob("__pycache__")))
                self.assertEqual(fast.verify_release(self.bundle, self.bundle_sha)["status"], "trusted")

    def test_generated_adapter_supplies_required_names_without_target_imports(self):
        # These are controlled source fixtures; the pipeline remains compile-only.
        source = fixtures.GOLDEN_APP.replace("from __future__ import annotations\n", "").replace("import os\n", "").replace("from typing import Any\n", "")
        source = source.replace("client: Any | None", "client: object | None")
        source = source.replace('model=model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)',
                                "model=model or DEFAULT_MODEL")
        for with_test in (False, True):
            with self.subTest(with_test=with_test):
                root = self._repo(source, with_test=with_test)
                result = self._run(root)
                self.assertEqual(result["outcome"], "ready_for_human", result)
                generated = (root / "app.py").read_text()
                namespace = {"__name__": "controlled_fixture"}
                exec(compile(generated, "controlled_fixture.py", "exec"), namespace)
                response = io.BytesIO(json.dumps({
                    "status": "completed", "governance": {"decision": "allow"},
                    "output": {"content": [{"type": "text", "role": "assistant", "text": "summary"}]},
                }).encode())
                response.status = 200
                opener = mock.Mock()
                opener.open.return_value = response
                with mock.patch.dict(os.environ, {"KEEL_API_KEY": "test-only-placeholder"}):
                    self.assertEqual(namespace["summarize"]("test", client=opener), "summary")

    def test_without_coupled_test_runtime_check_is_not_applicable(self):
        result = self._run(self._repo(with_test=False))
        self.assertEqual(result["outcome"], "ready_for_human", result)
        self.assertIsNone(result["validation"]["focused_compatibility_test"])
        self.assertEqual(result["validation"]["focused_compatibility_test_status"], "not_applicable")

    def test_repository_fsmonitor_hook_is_not_executed(self):
        root = self._repo()
        marker = root / ".git" / "hook-ran"
        hook = root / ".git" / "untrusted-monitor"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o700)
        _git(root, "config", "core.fsmonitor", str(hook))

        result = self._run(root)

        self.assertFalse(marker.exists())
        self.assertEqual(result["outcome"], "ready_for_human", result)

    def test_generated_test_syntax_failure_rolls_back(self):
        root = self._repo()
        original_generator = fast.generate_adapter

        def invalid_generator(repo, seam):
            application, test = original_generator(repo, seam)
            return application, test + fixtures.SYNTAX_FAILURE

        with mock.patch.object(fast, "generate_adapter", side_effect=invalid_generator):
            result = self._run(root)
        self.assertEqual(result["outcome"], "validation_failed", result)
        self.assertEqual(_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertFalse((root / ".keel" / "setup-state.json").exists())

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
        self.assertEqual(client, {"name": "client", "kind": "keyword_only", "default": {"required": False}})

    def test_dangling_gitignore_is_refused_without_external_write_or_link_removal(self):
        for existing in (False, True):
            root = self._repo()
            with tempfile.TemporaryDirectory() as outside:
                destination = pathlib.Path(outside) / "target"
                if existing:
                    destination.write_text("preserve")
                ignore = root / ".gitignore"
                ignore.unlink()
                ignore.symlink_to(destination)
                _git(root, "add", ".gitignore")
                _git(root, "commit", "-qm", "symlink fixture")
                before = (root / "app.py").read_bytes()
                result = self._run(root)
                self.assertEqual(result["outcome"], "validation_failed", result)
                self.assertTrue(ignore.is_symlink())
                self.assertEqual(destination.exists(), existing)
                if existing:
                    self.assertEqual(destination.read_text(), "preserve")
                self.assertEqual((root / "app.py").read_bytes(), before)

    def test_coupled_protocol_double_is_adapted_and_runtime_test_is_deferred(self):
        root = self._repo()
        result = self._run(root)
        test_source = (root / "tests" / "test_app.py").read_text()

        self.assertEqual(result["outcome"], "ready_for_human", result)
        self.assertEqual(result["validation"]["focused_compatibility_test_status"], "deferred_requires_isolation")
        self.assertIn("FakeClient", test_source)
        self.assertIn("https://api.keelapi.com/v1/execute", test_source)
        self.assertEqual(
            result["validation"]["focused_compatibility_test"],
            "tests.test_app.SummarizerTests.test_sends_text_to_the_selected_model",
        )

    def test_regression_generated_adapter_accepts_normalized_execute_text(self):
        root = self._repo()
        result = self._run(root)

        self.assertEqual(result["outcome"], "ready_for_human", result)
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_app"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "KEEL_API_KEY": "must-not-be-used-by-the-fixture"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Ran 4 tests", completed.stderr)

    def test_generated_fixture_uses_exact_normalized_execute_response_shape(self):
        root = self._repo()
        result = self._run(root)
        generated_test = (root / "tests" / "test_app.py").read_text(encoding="utf-8")

        self.assertEqual(result["outcome"], "ready_for_human", result)
        self.assertIn('"output": {', generated_test)
        self.assertIn('"content": [{', generated_test)
        self.assertIn('"type": "text"', generated_test)
        self.assertIn('"role": "assistant"', generated_test)
        self.assertIn('"text": text', generated_test)
        self.assertNotIn('"output_text"', generated_test)
        self.assertNotIn('"type": "message"', generated_test)

    def test_generated_parser_rejects_provider_native_and_malformed_output_shapes(self):
        namespace: dict[str, object] = {}
        exec(
            "import json\nimport urllib.error\nimport urllib.request\nfrom typing import Any\n"
            + fast._adapter_support(),
            namespace,
        )
        parser = namespace["_keel_output_text"]
        invalid_outputs = (
            {"output_text": "provider convenience field"},
            {"output": [{"type": "message", "content": []}]},
            {"content": []},
            {"content": [None]},
            {"content": [{"type": "output_text", "role": "assistant", "text": "wrong type"}]},
            {"content": [{"type": "text", "role": "user", "text": "wrong role"}]},
            {"content": [{"type": "text", "role": "assistant", "text": 42}]},
        )

        for output in invalid_outputs:
            with self.subTest(output=output):
                with self.assertRaisesRegex(RuntimeError, "invalid normalized response"):
                    parser({"output": output})

    def test_generated_parser_accepts_only_well_formed_normalized_text_blocks(self):
        namespace: dict[str, object] = {}
        exec(
            "import json\nimport urllib.error\nimport urllib.request\nfrom typing import Any\n"
            + fast._adapter_support(),
            namespace,
        )
        parser = namespace["_keel_output_text"]
        body = {
            "output": {
                "content": [
                    {"type": "text", "role": "assistant", "text": "The answer "},
                    {"type": "text", "role": "assistant", "text": "is 42."},
                ]
            }
        }

        self.assertEqual(parser(body), "The answer is 42.")

    def test_generated_reader_preserves_response_size_bound(self):
        namespace: dict[str, object] = {}
        exec(
            "import json\nimport urllib.error\nimport urllib.request\nfrom typing import Any\n"
            + fast._adapter_support(),
            namespace,
        )
        limit = namespace["KEEL_MAX_RESPONSE_BYTES"]

        class OversizedResponse:
            def read(self, requested: int) -> bytes:
                self.requested = requested
                return b"x" * requested

        response = OversizedResponse()
        with self.assertRaisesRegex(RuntimeError, "oversized response"):
            namespace["_keel_read_json"](response)
        self.assertEqual(response.requested, limit + 1)

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

    def test_unrecognized_security_logic_and_extra_tests_are_preserved_by_refusal(self):
        for source in (
            fixtures.GOLDEN_APP.replace("    summary = response.output_text.strip()", "    audit_request()\n    summary = response.output_text.strip()"),
            fixtures.GOLDEN_APP.replace("        client = OpenAI()", "        authorize_user()\n        client = OpenAI()"),
            fixtures.GOLDEN_APP.replace("    response = client.responses.create(", "    authorize_user()\n    response = client.responses.create("),
        ):
            root = self._repo(source)
            result = self._run(root)
            self.assertEqual(result["outcome"], "model_review_required", result)
            self.assertEqual((root / "app.py").read_text(), source)
            self.assertEqual(_git(root, "status", "--porcelain"), "")
        root = self._repo()
        path = root / "tests/test_app.py"
        source = path.read_text() + "\ndef test_extra_authorization():\n    assert authorized()\n"
        path.write_text(source)
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "extra test")
        result = self._run(root)
        self.assertEqual(result["outcome"], "model_review_required", result)
        self.assertEqual(path.read_text(), source)
        self.assertEqual(_git(root, "status", "--porcelain"), "")

    def test_same_line_response_mapping_and_return_are_refused_without_changes(self):
        source = fixtures.GOLDEN_APP.replace(
            '    summary = response.output_text.strip()\n    if not summary:\n        raise ValueError("The model returned an empty summary.")\n    return summary',
            '    summary = response.output_text.strip(); return summary',
        )
        root = self._repo(source, with_test=False)
        result = self._run(root)
        self.assertEqual(result["outcome"], "model_review_required", result)
        self.assertEqual((root / "app.py").read_text(), source)
        self.assertEqual(_git(root, "status", "--porcelain"), "")

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

    def test_ready_result_emits_only_local_review_metadata(self):
        root = self._repo()

        result = self._run(root)

        self.assertEqual(result["outcome"], "ready_for_human", result)
        evidence = result["diff"]
        self.assertNotIn("unified_diff", evidence)
        self.assertIsInstance(evidence["whitespace_only_changed_lines"], int)
        self.assertEqual(set(evidence["digests"]), set(result["validation"]["changed_paths"]))
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

    def test_repeated_line_statistics_and_skipped_entries_are_bounded(self):
        before = "repeat\n" * 20_000
        after = "repeat\n" * 19_999 + "changed\n"
        with mock.patch.object(fast.difflib, "SequenceMatcher", side_effect=AssertionError("quadratic comparison")):
            self.assertEqual(fast._line_change_stats(before, after), (1, 1, 0))
        root = self._repo()
        for index in range(12):
            (root / f"ignored-{index}.bin").write_bytes(b"ignored")
        with mock.patch.object(fast, "MAX_SCAN_ENTRIES", 10):
            result = self._run(root)
        self.assertEqual(result["outcome"], "model_review_required", result)
        self.assertEqual((root / "app.py").read_text(), fixtures.GOLDEN_APP)

    def test_ignored_compatibility_fifo_is_refused_without_blocking_or_editing(self):
        root = self._repo()
        path = root / "tests/test_fifo.py"
        os.mkfifo(path)
        (root / ".git/info/exclude").write_text("tests/test_fifo.py\n")
        result = self._run(root)
        self.assertEqual(result["outcome"], "validation_failed", result)
        self.assertEqual((root / "app.py").read_text(), fixtures.GOLDEN_APP)

    def test_large_source_is_never_embedded_in_metadata(self):
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

        self.assertNotIn("unified_diff", evidence)
        self.assertNotIn("bounded-diff-fixture", json.dumps(evidence))
        self.assertLess(len(json.dumps(evidence)), 4096)

    def test_opaque_source_and_literal_defaults_never_leave_the_checkout(self):
        sentinel = "unrecognized-opaque-example-93457"
        source = fixtures.GOLDEN_APP.replace("model: str | None = None", f"model: str | None = {sentinel!r}")
        source = source.replace("source = text.strip()", f"source = text.strip()  # {sentinel}")
        root = self._repo(source)
        result = self._run(root)
        self.assertEqual(result["outcome"], "ready_for_human", result)
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertIn(sentinel, (root / "app.py").read_text())

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
