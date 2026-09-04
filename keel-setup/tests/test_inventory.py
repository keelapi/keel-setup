from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "inventory.py"
SPEC = importlib.util.spec_from_file_location("inventory", SCRIPT)
inventory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(inventory)
SKILL = pathlib.Path(__file__).resolve().parents[1] / "SKILL.md"


def _write_openai_seam(path: pathlib.Path, *, base_url: str | None = None) -> None:
    constructor = "OpenAI()" if base_url is None else f"OpenAI(base_url={base_url!r})"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from openai import OpenAI\n"
        f"client = {constructor}\n"
        "def summarize(text):\n"
        "    return client.responses.create(model='gpt-4o-mini', input=text)\n",
        encoding="utf-8",
    )


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
        missing = report()
        del missing["paths"][0]["line"]
        cases.append(missing)
        unknown = report()
        unknown["paths"][0]["extra"] = True
        cases.append(unknown)
        wrong_type = report()
        wrong_type["paths"][0]["line"] = "one"
        cases.append(wrong_type)
        too_many = report()
        too_many["paths"][0]["uncertainties"] = [str(index) for index in range(21)]
        cases.append(too_many)
        empty_signal = report()
        empty_signal["paths"][0]["signal"] = ""
        cases.append(empty_signal)
        duplicate = report()
        duplicate["paths"].append(dict(duplicate["paths"][0]))
        cases.append(duplicate)
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


class FastFirstRunTest(unittest.TestCase):
    def test_trivial_openai_summarizer_selects_one_narrow_seam(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_openai_seam(root / "service.py")
            result = inventory.fast_inspect(root)

        self.assertEqual(result["decision"], "single_narrow_seam")
        self.assertEqual(
            {key: result["selected_seam"][key] for key in ("path", "provider", "eligibility")},
            {
                "path": "service.py",
                "provider": "openai",
                "eligibility": "eligible_for_local_review",
            },
        )
        self.assertEqual(result["evidence_level"], "source_inspected")
        self.assertIn("whole_application_protection", result["does_not_establish"])
        self.assertIsInstance(result["scan"]["elapsed_ms"], float)

    def test_multiple_application_calls_are_ambiguous_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "summary.py").write_text(
                "from openai import OpenAI\nclient=OpenAI()\n"
                "def one(): return client.responses.create(model='a', input=[])\n"
                "def two(): return client.chat.completions.create(model='b', messages=[])\n",
                encoding="utf-8",
            )
            result = inventory.fast_inspect(root)
            exhaustive = inventory.inspect(root)

        self.assertEqual(result["decision"], "ambiguous_multiple_seams")
        self.assertIsNone(result["selected_seam"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(exhaustive["paths"])
        self.assertTrue(all(item["status"] == "unresolved" for item in exhaustive["paths"]))

    def test_streaming_seam_is_blocked_instead_of_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "service.py").write_text(
                "from openai import OpenAI\nclient=OpenAI()\n"
                "result=client.responses.create(model='a', input=[], stream=True)\n",
                encoding="utf-8",
            )
            result = inventory.fast_inspect(root)

        self.assertEqual(result["decision"], "blocked_structural_condition")
        self.assertIsNone(result["selected_seam"])
        self.assertIn("streaming", " ".join(result["candidates"][0]["reasons"]))

    def test_test_double_does_not_compete_with_application_seam(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "service.py").write_text(
                "from openai import OpenAI\nclient=OpenAI()\n"
                "result=client.responses.create(model='a', input=[])\n",
                encoding="utf-8",
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_service.py").write_text(
                "from openai import OpenAI\nclient=OpenAI()\n"
                "result=client.responses.create(model='fake', input=[])\n",
                encoding="utf-8",
            )
            result = inventory.fast_inspect(root)

        self.assertEqual(result["decision"], "single_narrow_seam")
        self.assertEqual(result["selected_seam"]["path"], "service.py")
        self.assertFalse(any(item["test_only"] for item in result["candidates"]))
        self.assertEqual(result["scan"]["files_considered"], 1)
        self.assertEqual(result["scan"]["test_files_skipped"], 1)

    def test_javascript_openai_call_requires_provider_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "service.ts").write_text(
                "import OpenAI from 'openai';\n"
                "const client = new OpenAI();\n"
                "client.responses.create({model: 'a', input: []});\n",
                encoding="utf-8",
            )
            selected = inventory.fast_inspect(root)
            (root / "service.ts").write_text(
                "const client = internalClient();\n"
                "client.responses.create({model: 'a', input: []});\n",
                encoding="utf-8",
            )
            unrelated = inventory.fast_inspect(root)

        self.assertEqual(selected["decision"], "single_narrow_seam")
        self.assertEqual(selected["selected_seam"]["provider"], "openai")
        self.assertEqual(unrelated["decision"], "no_obvious_seam")

    def test_fast_output_never_echoes_source_or_secret_content(self):
        marker = "not-a-real-secret-value"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "service.py").write_text(
                "from openai import OpenAI\nclient=OpenAI(api_key='" + marker + "')\n"
                "result=client.responses.create(model='a', input=[])\n",
                encoding="utf-8",
            )
            rendered = json.dumps(inventory.fast_inspect(root))

        self.assertNotIn(marker, rendered)
        self.assertNotIn("api_key", rendered)

    def test_fast_output_never_echoes_a_credential_shaped_path(self):
        marker = "sk-not-a-real-secret-value"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_openai_seam(root / f"{marker}.py")
            rendered = json.dumps(inventory.fast_inspect(root))

        self.assertNotIn(marker, rendered)
        self.assertIn("ambiguous_sensitive_path", rendered)

    def test_scan_limit_refuses_selection_and_reports_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "a.py").write_text("value = 1\n", encoding="utf-8")
            (root / "b.py").write_text(
                "from openai import OpenAI\nclient=OpenAI()\n"
                "result=client.responses.create(model='a', input=[])\n",
                encoding="utf-8",
            )
            result = inventory.fast_inspect(root, max_files=1)

        self.assertEqual(result["decision"], "ambiguous_scan_truncated")
        self.assertTrue(result["scan"]["truncated"])
        self.assertIsNone(result["selected_seam"])

    def test_truncated_scan_never_selects_one_candidate_when_second_seam_is_unseen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_openai_seam(root / "aaa_app.py")
            for index in range(inventory.FAST_MAX_FILES - 1):
                (root / f"middle_{index:03d}.py").write_text("value = 1\n", encoding="utf-8")
            _write_openai_seam(root / "zzz_worker" / "batch.py")
            result = inventory.fast_inspect(root)

        self.assertEqual(result["decision"], "ambiguous_scan_truncated")
        self.assertIsNone(result["selected_seam"])
        self.assertTrue(result["scan"]["truncated"])
        self.assertEqual(len(result["candidates"]), 1)

    def test_test_files_do_not_consume_fast_scan_file_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for index in range(25):
                test_path = root / "tests" / f"test_{index:03d}.py"
                test_path.parent.mkdir(parents=True, exist_ok=True)
                test_path.write_text("value = 1\n", encoding="utf-8")
            _write_openai_seam(root / "zapp" / "service.py")
            result = inventory.fast_inspect(root, max_files=1)

        self.assertEqual(result["decision"], "single_narrow_seam")
        self.assertEqual(result["selected_seam"]["path"], "zapp/service.py")
        self.assertEqual(result["scan"]["files_considered"], 1)
        self.assertEqual(result["scan"]["test_files_skipped"], 25)
        self.assertFalse(result["scan"]["truncated"])

    def test_adjacent_raw_http_provider_bypass_blocks_single_seam_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_openai_seam(root / "app" / "service.py")
            (root / "app" / "worker.py").write_text(
                "import httpx\n"
                "def run(payload):\n"
                "    return httpx.post('https://api.openai.com/v1/responses', json=payload)\n",
                encoding="utf-8",
            )
            result = inventory.fast_inspect(root)

        self.assertEqual(result["decision"], "ambiguous_adjacent_bypass")
        self.assertIsNone(result["selected_seam"])
        self.assertIn("raw_http_provider", {item["request_kind"] for item in result["candidates"]})

    def test_non_default_base_url_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_openai_seam(root / "service.py", base_url="http://internal-gateway.corp/v1")
            result = inventory.fast_inspect(root)

        self.assertEqual(result["decision"], "blocked_structural_condition")
        self.assertIsNone(result["selected_seam"])
        self.assertIn("base_url", " ".join(result["candidates"][0]["reasons"]))

    def test_literal_official_provider_base_url_remains_identifiable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_openai_seam(root / "service.py", base_url="https://api.openai.com/v1")
            result = inventory.fast_inspect(root)

        self.assertEqual(result["decision"], "single_narrow_seam")
        self.assertEqual(result["selected_seam"]["provider"], "openai")

    def test_lookalike_provider_hostname_is_not_treated_as_official(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_openai_seam(root / "service.py", base_url="https://api.openai.com.attacker.invalid/v1")
            result = inventory.fast_inspect(root)

        self.assertEqual(result["decision"], "blocked_structural_condition")
        self.assertIsNone(result["selected_seam"])

    def test_demo_or_example_surface_is_not_automatically_selected(self):
        for directory_name in ("examples", "example", "demo", "demos", "samples", "docs", "scripts", "benchmarks", "notebooks"):
            with self.subTest(directory=directory_name), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                _write_openai_seam(root / directory_name / "quickstart.py")
                result = inventory.fast_inspect(root)

            self.assertEqual(result["decision"], "ambiguous_nonproduction_surface")
            self.assertIsNone(result["selected_seam"])
            self.assertEqual(result["candidates"][0]["selection_exclusion"], "non_production_surface")

    def test_provider_signal_in_other_supported_text_language_blocks_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            _write_openai_seam(root / "service.py")
            (root / "worker.go").write_text(
                'package worker\nconst endpoint = "https://api.openai.com/v1/responses"\n',
                encoding="utf-8",
            )
            result = inventory.fast_inspect(root)

        self.assertEqual(result["decision"], "ambiguous_adjacent_bypass")
        self.assertIsNone(result["selected_seam"])

    def test_skill_orders_first_gate_before_exhaustive_inventory(self):
        text = SKILL.read_text(encoding="utf-8")
        fast = text.index("## Fast First Run: zero-credential preparation")
        gate = text.index("## First human gate")
        verify = text.index("## Deterministic state-D verification")
        deep = text.index("## Post-gate deep assurance")
        final = text.index("## Coverage handoff")

        self.assertLess(fast, gate)
        self.assertLess(gate, verify)
        self.assertLess(verify, deep)
        self.assertLess(deep, final)
        fast_section = text[fast:gate]
        self.assertIn("Prepare the smallest fail-closed `/v1/execute` adapter", fast_section)
        self.assertIn("Creating, editing, or requiring a repository protocol-double test is normally", fast_section)
        self.assertIn("Do not perform a web search during Fast First Run", text[:gate])
        self.assertIn("Do not add or edit application\n   tests, README files", fast_section)
        self.assertIn("without importing or executing it", fast_section)
        self.assertIn("Do not block the first gate on unrelated", fast_section)
        post_gate_section = text[gate:final]
        self.assertIn("create or update its narrow protocol-double test", post_gate_section)
        self.assertIn("application setup documentation", post_gate_section)

    def test_fast_first_run_preserves_client_hook_callable_contract(self):
        text = SKILL.read_text(encoding="utf-8")
        fast = text[
            text.index("## Fast First Run: zero-credential preparation") :
            text.index("## First human gate")
        ]

        self.assertIn("record the selected function or method's externally callable signature", fast)
        self.assertIn("`summarize(text, *, client=None, model=None)`", fast)
        self.assertIn("`summarize(text, *, model=None)`", fast)
        self.assertIn("must not silently become", fast)
        self.assertIn("compatibility of the recorded callable parameters and defaults", fast)
        self.assertIn("run that one focused test before the gate", fast)
        self.assertIn("stop for explicit human direction", fast)
        self.assertIn("Do not run broader tests", fast)

    def test_deterministic_golden_path_precedes_model_driven_fallback(self):
        text = SKILL.read_text(encoding="utf-8")
        fast = text[
            text.index("## Fast First Run: zero-credential preparation") :
            text.index("## First human gate")
        ]

        deterministic = fast.index("### Deterministic golden path")
        fallback = fast.index("### Model-driven fallback")
        self.assertLess(deterministic, fallback)
        self.assertIn("fast_first_run.py", fast[deterministic:fallback])
        self.assertIn("When the result is `ready_for_human`", fast[deterministic:fallback])
        self.assertIn("consult or cite saved memory", fast[deterministic:fallback])
        self.assertIn("enumerate bundle files", fast[deterministic:fallback])
        self.assertIn("run the release verifier separately", fast[deterministic:fallback])
        self.assertIn("rediscover the seam", fast[deterministic:fallback])
        self.assertIn("handle setup state separately", fast[deterministic:fallback])
        self.assertIn("repeat\nvalidation or tests", fast[deterministic:fallback])
        self.assertIn("`diff_truncated` is `false`, do not rerun `git diff`", fast[deterministic:fallback])
        self.assertIn("run exactly one targeted\n`git diff`", fast[deterministic:fallback])
        for outcome in (
            "ambiguous",
            "unsupported_shape",
            "model_review_required",
            "unsafe_contract_change",
            "validation_failed",
            "untrusted_bundle",
        ):
            self.assertIn(f"`{outcome}`", fast[deterministic:fallback])

    def test_fresh_golden_path_owns_trust_ignore_and_state_initialization(self):
        text = SKILL.read_text(encoding="utf-8")
        deterministic = text.index("### Deterministic golden path")
        fallback = text.index("### Model-driven fallback")
        direct_state_command = text.index(
            "python3 keel-setup/scripts/setup_state.py --repo-root . --state .keel/setup-state.json"
        )

        self.assertGreater(direct_state_command, fallback)
        before_deterministic = text[:deterministic]
        self.assertIn("do not run `setup_state.py`", before_deterministic)
        self.assertIn("`fast_first_run.py` owns fresh state initialization", before_deterministic)
        self.assertIn("Do not separately run or inspect\n`scripts/check_release_bundle.py`", before_deterministic)

    def test_first_human_gate_is_a_canonical_verbatim_template(self):
        text = SKILL.read_text(encoding="utf-8")
        gate = text[text.index("## First human gate") : text.index("### Client-key custody")]
        canonical = """> I prepared the OpenAI call in `PATH` for Keel. Nothing is using Keel yet.
>
> **Step 1 of 3 — Connect OpenAI**
>
> In the Keel dashboard, open **Set up Keel**.
>
> Under **Connect OpenAI for the first proof**, click **Connect a provider**. On **Connectors**, click
> **Add connector**, select **OpenAI**, and click **Next**. Enter a **Display name** and your
> **API key secret**, click **Review**, then **Save connector**. Click **Test connection**.
>
> Do not paste your OpenAI API key here.
>
> Reply `done` when **Connection test result** shows **healthy** and **Live test** shows **Yes**.
>
> This is local preparation for one call. No live Keel request or check of other application paths has
> happened yet."""

        self.assertIn("Render it verbatim", gate)
        self.assertIn("substituting only", gate)
        self.assertIn("add no preamble", gate)
        self.assertIn(canonical, gate)

    def test_guided_handoff_uses_current_dashboard_terms_one_phase_at_a_time(self):
        text = SKILL.read_text(encoding="utf-8")
        gate = text[text.index("## First human gate") : text.index("### Client-key custody")]

        for required in (
            "**Step 1 of 3 — Connect OpenAI**",
            "**Add connector**, select **OpenAI**, and click **Next**",
            "**Display name**",
            "**API key secret**",
            "**Connection test result** shows **healthy**",
            "**Live test** shows **Yes**",
            "**Step 2 of 3 — Turn on your first Keel policy**",
            "Read **What this setup applies** in step 1",
            "under **Set up Production Governance**",
            "**Apply Production Governance**",
            "**Review and turn it on**",
            "status says **Active**",
            "**Step 3 of 3 — Create your Keel Runtime key**",
            "**Runtime key** selected",
            "make `KEEL_API_KEY` available to this Codex session",
            "`help me install it`",
        ):
            self.assertIn(required, gate)

        first = gate.index("**Step 1 of 3")
        second = gate.index("**Step 2 of 3")
        third = gate.index("**Step 3 of 3")
        self.assertLess(first, second)
        self.assertLess(second, third)
        self.assertIn("After the human replies `done`", gate[first:second])
        self.assertIn("Then show only this block", gate[second:third])
        self.assertNotIn("Quickstart or template", gate)
        self.assertNotIn("client-scoped key", gate)
        self.assertIn("applies only to the currently supported OpenAI", gate)

    def test_runtime_key_help_does_not_overpromise_process_environment(self):
        text = SKILL.read_text(encoding="utf-8")
        gate = text[text.index("## First human gate") : text.index("### Client-key custody")]

        self.assertIn("never imply that exporting a variable in an unrelated shell", gate)
        self.assertIn("already-running Codex process", gate)
        self.assertIn("when the relevant process must be restarted", gate)
        self.assertIn("confirmation of presence", gate)

    def test_resume_never_infers_dashboard_progress_from_local_state(self):
        text = SKILL.read_text(encoding="utf-8")
        resume = text[text.index("- **Invocation 2 — resume.**") : text.index("- **Invocation 5")]

        self.assertIn("local state does not prove which human phase", resume)
        self.assertIn("Use only human assertions retained in the current conversation", resume)
        self.assertIn("restart at Step 1 instead of inferring dashboard progress", resume)

    def test_first_handoff_is_bounded_and_deep_report_is_retained(self):
        text = SKILL.read_text(encoding="utf-8")
        first_handoff = text[
            text.index("## First human gate") : text.index("### Client-key custody")
        ]
        final_handoff = text[
            text.index("## Coverage handoff") : text.index("## Return loop")
        ]

        self.assertIn("Nothing is using Keel yet", first_handoff)
        self.assertIn("No live Keel request", first_handoff)
        self.assertIn("check of other application paths", first_handoff)
        self.assertIn("Do not paste", first_handoff)
        self.assertIn("the key here", first_handoff)
        self.assertNotIn("every discovered execution path", first_handoff)
        self.assertIn("every discovered execution path", final_handoff)
        self.assertIn("machine-readable `does_not_establish`", final_handoff)
        self.assertIn("Also found, not governed", final_handoff)

    def test_skill_retains_execution_and_authority_boundaries(self):
        text = SKILL.read_text(encoding="utf-8")

        for required in (
            "deterministic proof remains only `POST /v1/execute`",
            "never introduce or preserve\n`/v1/proxy/*`",
            "managed MCP `:call` path",
            "MCP `:decide` and `:prepare` are not execution",
            "Never mint, exchange, or install it on their behalf",
            "An environment variable is transcript hygiene, not process isolation",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
