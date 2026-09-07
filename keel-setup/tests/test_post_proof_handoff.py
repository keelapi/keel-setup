"""Post-proof presentation contract: beginner default, expert detail on request.

The evidence model is unchanged. These tests pin *where* each part of it is
allowed to appear, so a successful first proof reads as a result rather than as
an audit report, and the rigorous evidence stays reachable and correctly
labelled one question away.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Published artifacts. Present in the source repo and in the public bundle.
SKILL = ROOT / "keel-setup" / "SKILL.md"
VERIFIER = ROOT / "keel-setup" / "scripts" / "verify_execute.py"

#: Private artifact. The normative spec is never published, so the assertions
#: that read it skip in the public bundle. Everything asserted against the
#: shipped SKILL.md and verifier runs in both modes.
SPEC = ROOT / "ONBOARDING_SPEC.md"
NEEDS_SPEC = "requires the private ONBOARDING_SPEC.md, absent in the public bundle"

#: Set in the inner run of the publication-mode check so it does not recurse.
PUBLICATION_MODE_GUARD = "KEEL_PUBLICATION_MODE_CHECK"

#: Published inputs this module reads.
PUBLISHED_INPUTS = (
    "keel-setup/SKILL.md",
    "keel-setup/scripts/verify_execute.py",
)

#: Everything the beginner success response must not carry. None of it leaves
#: the product's evidence model; it belongs to the technical layer.
INTERNAL_EVIDENCE_TOKENS = (
    "request_id",
    "permit_id",
    "http_status",
    "body_status",
    "error_stage",
    "governance_decision",
    "allowed_completed",
    "keel_denied",
    "provider_dispatch_failed_after_allow",
    "runtime-observed",
    "source-inspected",
    "human-asserted",
    "unresolved",
    "does_not_establish",
    "setup-state.json",
    "waiting_for_human",
    "state_d_verified",
    "profile digest",
    "correlation",
    "Evidence separation",
)


def _slice(text: str, start: str, end: str) -> str:
    first = text.index(start)
    return text[first : text.index(end, first) + len(end)]


class PostProofSuccessDefaultTest(unittest.TestCase):
    """Requirement 1 and 2: the default response after Allowed/Blocked PASS."""

    def setUp(self) -> None:
        self.skill = SKILL.read_text(encoding="utf-8")
        self.block = _slice(
            self.skill,
            "**Keel is working.**",
            "Ask for `verification details` if you want the technical evidence.",
        )

    def test_default_response_is_beginner_readable_success(self):
        for required in (
            "**Keel is working.**",
            "✓ An allowed request completed",
            "✓ A blocked request was stopped by Keel",
            "Keel used your first policy for this test.",
        ):
            self.assertIn(required, self.block)

    def test_default_response_carries_one_concise_scope_caveat(self):
        self.assertIn(
            "This proves Keel worked for the test. It does not yet prove that every path in your "
            "application uses\nKeel.",
            self.block,
        )

    def test_default_response_offers_the_three_next_actions(self):
        for required in (
            "**Now make it yours.** What would you like to do next?",
            "**A — Make Keel yours.**",
            "**B — Connect your application.**",
            "**C — Done for now.**",
        ):
            self.assertIn(required, self.block)

        make_yours = self.block.index("**A — Make Keel yours.**")
        connect = self.block.index("**B — Connect your application.**")
        done = self.block.index("**C — Done for now.**")
        self.assertLess(make_yours, connect)
        self.assertLess(connect, done)

    def test_default_response_offers_the_technical_layer_without_showing_it(self):
        self.assertIn(
            "Ask for `verification details` if you want the technical evidence.", self.block
        )

    def test_default_response_carries_no_internal_evidence_taxonomy(self):
        for token in INTERNAL_EVIDENCE_TOKENS:
            self.assertNotIn(token, self.block, f"beginner success block leaks {token!r}")

    def test_skill_forbids_appending_the_evidence_dump_to_the_default(self):
        guidance = _slice(
            self.skill,
            "### Successful proof — default response",
            "### `verification details` — the technical layer",
        )
        self.assertIn("show only the\nfollowing block", guidance)
        self.assertIn("add no preamble, evidence table", guidance)
        for token in ("`request_id`", "`permit_id`", "`does_not_establish`", "`.keel/setup-state.json`"):
            self.assertIn(f"{token}", guidance)
        self.assertIn("success response is not a policy interview", guidance)


class VerificationDetailsLayerTest(unittest.TestCase):
    """Requirement 3: the rigour survives, one explicit request away."""

    def setUp(self) -> None:
        self.skill = SKILL.read_text(encoding="utf-8")
        self.details = _slice(
            self.skill,
            "### `verification details` — the technical layer",
            "### Failure — beginner explanation before classification",
        )

    def test_details_are_produced_on_explicit_or_equivalent_request(self):
        self.assertIn("When the human asks for `verification details`", self.details)
        self.assertIn("asks in any equivalent way", self.details)

    def test_details_retain_classifications_and_allow_deny_semantics(self):
        for required in (
            "`runtime-observed` classifications",
            "`http_status`",
            "`body_status`",
            "`governance_decision`",
            "`error_stage`",
            "`classification`",
            "permit-stage denial from a post-allow dispatch failure",
        ):
            self.assertIn(required, self.details)

    def test_details_retain_correlation_identifiers_and_their_absence(self):
        self.assertIn("`request_id` and `permit_id`", self.details)
        self.assertIn("exact\n  correlation is unavailable when either is null", self.details)

    def test_details_retain_evidence_separation_and_negative_space(self):
        for required in (
            "`human-asserted`",
            "`source-inspected`",
            "`unresolved` application-path",
            "what the proof does not establish",
            "whole-application coverage",
            "bypass\n  absence",
        ):
            self.assertIn(required, self.details)

    def test_details_own_the_local_state_commentary(self):
        self.assertIn("Name the local `.keel/setup-state.json` stage here rather than in the summary", self.details)
        self.assertIn("invocation count is local workflow state, not Keel evidence", self.details)

    def test_layering_is_declared_as_disclosure_order_not_omission(self):
        contract = _slice(self.skill, "## Post-proof handoff", "### Successful proof — default response")
        self.assertIn("`beginner_success_summary`", contract)
        self.assertIn("`technical_verification_details`", contract)
        self.assertIn("Layering is disclosure order, not omission", contract)
        self.assertIn("nothing leaves the evidence model to shorten the summary", contract)
        self.assertIn("keeps its label in the details layer", contract)


class FailurePresentationTest(unittest.TestCase):
    """Requirement 4: failures stay actionable, beginner language first."""

    def setUp(self) -> None:
        self.skill = SKILL.read_text(encoding="utf-8")
        self.failure = _slice(
            self.skill,
            "### Failure — beginner explanation before classification",
            "### Make Keel yours",
        )

    def test_failure_is_not_simplified_into_the_success_shape(self):
        self.assertIn("never present a failure or a partial pair as success", self.failure)
        self.assertIn("state-D failure playbook", self.failure)

    def test_failure_leads_with_plain_language_then_technical_detail(self):
        self.assertIn("Lead with one plain sentence", self.failure)
        self.assertIn("Could not reach Keel.", self.failure)
        self.assertIn("Check that this terminal has internet access, then try again.", self.failure)
        self.assertIn("Technical details: transport_failed", self.failure)

        plain = self.failure.index("Could not reach Keel.")
        technical = self.failure.index("Technical details: transport_failed")
        self.assertLess(plain, technical)

    def test_failure_keeps_the_recovery_action(self):
        self.assertIn("never drop the recovery action to stay brief", self.failure)
        self.assertIn("Never lead a failure with the internal classification", self.failure)

    def test_verifier_already_emits_the_beginner_first_failure_shape(self):
        verifier = VERIFIER.read_text(encoding="utf-8")
        plain = verifier.index("Could not reach Keel. Check that this terminal has internet access")
        technical = verifier.index('"Technical details: transport_failed"')
        self.assertLess(plain, technical)


class NextActionsTest(unittest.TestCase):
    """Requirements 5, 6, and 7: what each choice actually does."""

    def setUp(self) -> None:
        self.skill = SKILL.read_text(encoding="utf-8")

    def test_make_keel_yours_reuses_the_existing_policy_skill(self):
        section = _slice(
            self.skill,
            "### Make Keel yours",
            "### Connect your application — separate, human-controlled runtime step",
        )
        for required in (
            "use the existing `keel-policy` skill",
            "inspect relevant repository actions\nwithout executing them",
            "explain in beginner language",
            "run freely, require approval, or be blocked",
            "Draft only the requested\ncanonical policy and validate it",
            "Hand the inactive\ndraft to [Policies](https://dashboard.keelapi.com/dashboard/policies)",
            "**Paste a policy from your coding agent**",
            "The human reviews and turns it on.",
            "Do not introduce another\nauthoring service or activate the draft.",
        ):
            self.assertIn(required, section)

    def test_connect_application_preserves_runtime_key_custody(self):
        section = _slice(
            self.skill,
            "### Connect your application — separate, human-controlled runtime step",
            "### Done for now",
        )
        for required in (
            "Only when the human chooses this step",
            "Do not request the key",
            "Ask which runtime or deployment environment actually runs the application",
            "specific to that platform's own secret store",
            "The human installs the credential; the agent never handles the value.",
        ):
            self.assertIn(required, section)
        self.assertIn("Do not fall back to\n`launchctl`, a `.env` file, a shell-profile edit", section)
        self.assertIn("restart of the coding agent as the default", section)

    def test_connect_application_discloses_custody_expansion_before_it_happens(self):
        section = _slice(
            self.skill,
            "### Connect your application — separate, human-controlled runtime step",
            "### Done for now",
        )
        self.assertIn(
            "If exercising the live application path would put the Runtime key inside the "
            "coding-agent process, say\nthat before doing it.",
            section,
        )
        self.assertIn("expansion of credential custody", section)
        self.assertIn("application-path verification stays `unresolved`", section)

    def test_done_for_now_closes_without_an_evidence_dump(self):
        section = _slice(self.skill, "### Done for now", "## Post-gate deep assurance")
        self.assertIn("You're set. Keel's first allow/deny proof passed.", section)
        self.assertIn(
            "You can come back anytime to create another policy or connect the application itself.",
            section,
        )
        self.assertIn("no evidence dump, no coverage\nreport, no cadence commentary", section)
        self.assertIn("ending\nhere hides nothing", section)
        self.assertIn("do not treat a clean close as whole-application assurance", section)


class PostProofAuthorityWordingTest(unittest.TestCase):
    """Requirement: no claim of agent authority or whole-application protection."""

    def setUp(self) -> None:
        self.skill = SKILL.read_text(encoding="utf-8")
        self.block = _slice(
            self.skill,
            "**Keel is working.**",
            "Ask for `verification details` if you want the technical evidence.",
        )

    def test_default_response_claims_no_whole_application_protection(self):
        lowered = self.block.lower()
        for banned in (
            "your app is protected",
            "your application is protected",
            "fully protected",
            "end-to-end verified",
            "all ai calls are governed",
            "keel is installed",
            "no bypass",
            "deployed",
        ):
            self.assertNotIn(banned, lowered, f"beginner success block claims {banned!r}")

    def test_policy_line_stays_scoped_to_the_observed_test(self):
        """`active` is not established: no policy identity is printed, and no
        profile is re-read at handoff. Only the test-scoped claim is supported."""
        lowered = self.block.lower()
        for banned in ("policy is active", "is now active", "policy is on", "policy is enabled"):
            self.assertNotIn(banned, lowered, f"beginner success block claims {banned!r}")
        self.assertIn("Keel used your first policy for this test.", self.block)

    def test_default_response_credits_policy_activation_to_the_human(self):
        constraints = _slice(
            self.skill,
            "The policy line is scoped to this test deliberately.",
            "success response is not a policy interview.",
        )
        self.assertIn("The verifier prints no policy identity", constraints)
        self.assertIn("no\nprofile is re-read when this block is rendered", constraints)
        self.assertIn("the Step 2 dashboard status stays `human-asserted`", constraints)
        self.assertIn("never be promoted to a standing claim", constraints)
        self.assertIn("never say Keel or this skill turned the policy on", constraints)
        self.assertIn(
            "Never\nwrite or imply that the application is protected, covered, deployed, verified end "
            "to end, or free of\nbypasses",
            constraints,
        )

    def test_verifier_gives_the_agent_no_policy_identity_to_claim_from(self):
        """The basis for the test-scoped policy wording.

        The agent sees only the redacted result records and PASS/FAIL. The
        verification profile binds a policy id, version, and content digest,
        but none of that is printed, so the agent cannot name which policy
        decided or assert a present-tense status. If policy identity is ever
        added to the verifier's output, revisit the beginner copy rather than
        deleting this test.
        """
        verifier = VERIFIER.read_text(encoding="utf-8")
        fields = _slice(verifier, "OUTPUT_FIELDS = (", ")")
        for absent in ("policy", "profile_digest", "content_digest", "effective_policy_set"):
            self.assertNotIn(absent, fields)
        self.assertIn("profile_digest", _slice(verifier, "def _profile_binding", "def classify"))
        printed = [
            line.strip()
            for line in verifier.splitlines()
            if "print(" in line and "stderr" not in line
        ]
        self.assertFalse(
            [line for line in printed if "profile" in line],
            "verifier prints profile material the beginner copy would then have to account for",
        )

    def test_default_response_asserts_no_credential_custody_by_the_agent(self):
        lowered = self.block.lower()
        for banned in ("keel_api_key", "export ", "paste the key", "your key is"):
            self.assertNotIn(banned, lowered)


@unittest.skipUnless(SPEC.is_file(), NEEDS_SPEC)
class OnboardingSpecContractTest(unittest.TestCase):
    """The normative spec carries the same two-layer contract.

    Skipped in the public bundle: ONBOARDING_SPEC.md is private. The skill-side
    half of this contract is asserted by the classes above, which do run there.
    """

    def setUp(self) -> None:
        self.spec = SPEC.read_text(encoding="utf-8")

    def test_spec_defines_both_layers(self):
        section = _slice(
            self.spec,
            "### 6. Complete project proof; offer the separate application-runtime step",
            "### 6a. Complete the deferred deep assurance work",
        )
        for required in (
            "`beginner_success_summary` is the default response",
            "`technical_verification_details` is the full labelled evidence",
            "Layering is disclosure order, not omission",
            "**Make Keel\nyours** / **Connect your application** / **Done for now**",
            "It\nmust not carry `request_id`, `permit_id`",
            "`verification details` restores full rigour",
            "A failure is not simplified into the success shape.",
        ):
            self.assertIn(required, section)

    def test_spec_keeps_evidence_separation_while_permitting_layered_disclosure(self):
        sentence = (
            "> At every handoff, separate observed evidence from source-only evidence and state what "
            "the evidence\n> does not establish. After a successful first proof, lead with the "
            "plain-language result and the one\n> scope caveat, and give me that full separation when "
            "I ask for verification details."
        )
        self.assertEqual(
            self.spec.count(sentence), 3, "all three Copy Setup variants must carry the same rule"
        )

    def test_spec_retains_the_full_taxonomy_for_the_final_assurance_handoff(self):
        self.assertIn(
            "the complete machine-readable `does_not_establish` taxonomy in the final assurance handoff",
            self.spec,
        )


class PublicationModeTest(unittest.TestCase):
    """This file ships publicly, so it must run where the spec is absent."""

    @unittest.skipIf(os.environ.get(PUBLICATION_MODE_GUARD), "inner publication-mode run")
    def test_suite_runs_with_only_published_artifacts(self):
        """Copy exactly the published inputs into a bare tree and run this module
        there. Proves the public bundle's CI can execute this file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for relative in PUBLISHED_INPUTS:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)
            tests_dir = root / "keel-setup" / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pathlib.Path(__file__), tests_dir / pathlib.Path(__file__).name)
            self.assertFalse((root / "ONBOARDING_SPEC.md").exists())

            environment = dict(os.environ, **{PUBLICATION_MODE_GUARD: "1"})
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", pathlib.Path(__file__).stem, "-v"],
                cwd=tests_dir,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode, 0, f"publication-mode run failed:\n{completed.stderr[-3000:]}"
            )
            self.assertIn(
                "skipped=4",
                completed.stderr,
                f"expected the three spec tests plus this one to skip:\n{completed.stderr[-1500:]}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
