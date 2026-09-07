"""Every field the `basic` profile advertises must be real, backed, and usable.

The profile previously listed five fields that exist in no catalog, schema, or
provenance artifact. A `basic` policy written to that whitelist was rejected by
the API on save with `unknown_fields`, so the profile advertised controls a
customer could not create. These tests pin the corrected list against the
committed artifacts and fail if it drifts apart again.

Publication mode: this file ships in the public setup bundle, where
``tools/authoring_catalog_snapshot.json`` is deliberately absent — it is the
private extract of the keel-api catalog and is not published. Only the
assertions that genuinely need that snapshot skip when it is missing. Every
assertion that can be made from published artifacts
(``keel-policy/reference/field-provenance.json``, ``tools/public_surface.json``,
``policy-document.schema.json``) runs in both modes, including the drift guard.

Offline scope: catalog presence is the offline proxy for API acceptance. These
tests do not reach a running API and do not prove a live save.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILL = ROOT / "keel-policy" / "SKILL.md"

#: Published artifacts. Present in both the source repo and the public bundle.
PROVENANCE_PATH = ROOT / "keel-policy" / "reference" / "field-provenance.json"
POLICY_SCHEMA_PATH = ROOT / "keel-policy" / "reference" / "policy-document.schema.json"
PUBLIC_SURFACE_PATH = ROOT / "tools" / "public_surface.json"

#: Private artifact. Never published; absent in the public bundle by design.
SNAPSHOT_PATH = ROOT / "tools" / "authoring_catalog_snapshot.json"

PROVENANCE = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))["fields"]
POLICY_SCHEMA = json.loads(POLICY_SCHEMA_PATH.read_text(encoding="utf-8"))
PUBLIC = set(json.loads(PUBLIC_SURFACE_PATH.read_text(encoding="utf-8"))["fields"]["PUBLIC"])


def _optional_catalog_snapshot(path: pathlib.Path = SNAPSHOT_PATH):
    """Return the private catalog snapshot, or None when it is not published."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["fields"]


SNAPSHOT = _optional_catalog_snapshot()
NEEDS_SNAPSHOT = "requires the private keel-api catalog snapshot, absent in the public bundle"

#: Set in the inner run of the publication-mode check so it does not recurse.
PUBLICATION_MODE_GUARD = "KEEL_PUBLICATION_MODE_CHECK"

#: Published inputs this module reads. The publication-mode check copies exactly
#: these, proving the suite runs with the private artifacts missing.
PUBLISHED_INPUTS = (
    "keel-policy/SKILL.md",
    "keel-policy/reference/field-provenance.json",
    "keel-policy/reference/policy-document.schema.json",
    "tools/public_surface.json",
    "shared/scripts/schema_validation.py",
)

_SPEC = importlib.util.spec_from_file_location(
    "schema_validation", ROOT / "shared" / "scripts" / "schema_validation.py"
)
_schema_validation = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader
_SPEC.loader.exec_module(_schema_validation)
validate_schema = _schema_validation.validate

BASIC_FIELDS = (
    "model",
    "provider",
    "estimated_cost_usd_micros",
    "context.provider_meta.region",
    "context.provider_meta.data_retention",
    "context._keel.request_hour_utc",
    "context._keel.request_day_of_week",
)

REMOVED_FIELDS = (
    "context.model",
    "context.provider",
    "context.estimated_cost_usd_micros",
    "context.prompt_token_count",
    "context.time_of_day",
)

BASIC_ACTIONS = (
    "deny",
    "deny_if_cost_exceeds",
    "deny_if_model_not_in",
    "deny_if_projected_monthly_ratio_exceeds",
    "deny_if_rate_exceeds",
    "deny_if_spike_detected",
    "constrain_max_output_tokens",
    "require_human_review",
)


def _basic_section() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("For `basic`, the document must satisfy")
    return text[start : text.index("## What requires a user decision", start)]


class BasicFieldBackingTest(unittest.TestCase):
    """Each advertised field is real in every artifact that must know about it."""

    def test_every_basic_field_has_published_provenance(self):
        for field in BASIC_FIELDS:
            self.assertIn(field, PROVENANCE, f"{field} has no published provenance")

    def test_every_basic_field_is_on_the_public_surface(self):
        for field in BASIC_FIELDS:
            self.assertIn(field, PUBLIC, f"{field} is not published")

    def test_basic_never_exposes_a_caller_asserted_fact(self):
        """A beginner profile carries no trust vocabulary, so it must not offer a
        field a caller can choose. This is why prompt-token count was removed
        rather than mapped onto attrs.estimated_input_tokens."""
        for field in BASIC_FIELDS:
            self.assertEqual(
                PROVENANCE[field], "keel_derived", f"{field} is not keel_derived"
            )

    @unittest.skipUnless(SNAPSHOT is not None, NEEDS_SNAPSHOT)
    def test_every_basic_field_is_in_the_authoritative_catalog_snapshot(self):
        for field in BASIC_FIELDS:
            self.assertIn(field, SNAPSHOT, f"{field} is absent from the keel-api catalog snapshot")

    @unittest.skipUnless(SNAPSHOT is not None, NEEDS_SNAPSHOT)
    def test_published_provenance_agrees_with_the_authoritative_catalog(self):
        for field in BASIC_FIELDS:
            self.assertEqual(
                PROVENANCE[field],
                SNAPSHOT[field]["provenance"],
                f"{field} provenance disagrees with the authoritative catalog",
            )


class BasicWhitelistDriftTest(unittest.TestCase):
    """The skill's prose and this list cannot drift apart, in either direction."""

    def test_skill_lists_exactly_the_supported_fields(self):
        section = _basic_section()
        quoted = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*)`", section))
        for field in BASIC_FIELDS:
            self.assertIn(field, quoted, f"skill no longer advertises {field}")

    def test_skill_does_not_advertise_a_removed_field(self):
        section = _basic_section()
        for field in REMOVED_FIELDS:
            self.assertNotIn(
                f"`{field}`", section, f"skill still advertises unsupported field {field}"
            )

    def test_no_advertised_field_is_absent_from_published_provenance(self):
        """The drift guard: anything the profile names must be a published field.

        Checked against the published provenance artifact rather than the private
        snapshot, so the guard that would have caught the original defect keeps
        running in the public bundle too.
        """
        section = _basic_section()
        quoted = re.findall(r"`((?:context|attrs|action)\.[A-Za-z0-9_.]+)`", section)
        for field in quoted:
            if field in REMOVED_FIELDS:
                continue  # named only to say it is unsupported
            self.assertIn(
                field, PROVENANCE, f"basic profile names {field}, which is not a published field"
            )

    def test_basic_actions_exist_in_the_policy_schema(self):
        mapping = POLICY_SCHEMA["properties"]["rules"]["items"]["discriminator"]["mapping"]
        for action in BASIC_ACTIONS:
            self.assertIn(action, mapping, f"basic action {action} is not in the policy schema")

    def test_utc_semantics_are_stated_not_left_implicit(self):
        section = _basic_section()
        self.assertIn("UTC", section)
        self.assertIn("Monday as 0", section)


class RepresentativeBasicPolicyTest(unittest.TestCase):
    """One policy per field, and one composed policy, all schema-valid."""

    def _validate(self, document):
        return validate_schema(document, POLICY_SCHEMA)

    def test_one_representative_rule_per_basic_field_validates(self):
        samples = {
            "model": {"field": "model", "op": "in", "value": ["gpt-4o", "o1"]},
            "provider": {"field": "provider", "op": "eq", "value": "openai"},
            "estimated_cost_usd_micros": {
                "field": "estimated_cost_usd_micros", "op": "gt", "value": 100000000
            },
            "context.provider_meta.region": {
                "field": "context.provider_meta.region", "op": "neq", "value": "us-east-1"
            },
            "context.provider_meta.data_retention": {
                "field": "context.provider_meta.data_retention", "op": "eq", "value": "none"
            },
            "context._keel.request_hour_utc": {
                "field": "context._keel.request_hour_utc", "op": "gte", "value": 22
            },
            "context._keel.request_day_of_week": {
                "field": "context._keel.request_day_of_week", "op": "in", "value": [5, 6]
            },
        }
        self.assertEqual(set(samples), set(BASIC_FIELDS))
        for field, condition in samples.items():
            document = {
                "name": f"Basic smoke test for {field}",
                "rules": [{"if": condition, "action": "deny"}],
            }
            self.assertEqual(self._validate(document), [], f"{field} rule failed validation")

    def test_composed_basic_policy_within_profile_limits_validates(self):
        """Business-hours restriction — the shape that replaces time_of_day."""
        document = {
            "name": "Review costly weekend requests",
            "rules": [
                {
                    "if": {
                        "all": [
                            {"field": "context._keel.request_day_of_week", "op": "in", "value": [5, 6]},
                            {"field": "estimated_cost_usd_micros", "op": "gt", "value": 50000000},
                        ]
                    },
                    "action": "require_human_review",
                    "require_attestation": {
                        "attestor": "project_owner",
                        "timeout_seconds": 3600,
                    },
                }
            ],
        }
        self.assertEqual(self._validate(document), [])
        self.assertLessEqual(len(document["rules"]), 10)


class PublicationModeTest(unittest.TestCase):
    """This file ships publicly, so it must run where private artifacts are absent."""

    def test_optional_snapshot_loader_returns_none_when_absent(self):
        """Mode-agnostic: holds in the source repo and in the public bundle."""
        with tempfile.TemporaryDirectory() as tmp:
            absent = pathlib.Path(tmp) / "authoring_catalog_snapshot.json"
            self.assertIsNone(_optional_catalog_snapshot(absent))

    @unittest.skipIf(
        os.environ.get(PUBLICATION_MODE_GUARD), "inner publication-mode run"
    )
    def test_suite_runs_with_only_published_artifacts(self):
        """Copy exactly the published inputs into a bare tree and run this module
        there. Proves the public bundle's CI can execute this file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for relative in PUBLISHED_INPUTS:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)
            tests_dir = root / "keel-policy" / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pathlib.Path(__file__), tests_dir / pathlib.Path(__file__).name)
            self.assertFalse((root / "tools" / "authoring_catalog_snapshot.json").exists())

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
                "skipped=3",
                completed.stderr,
                f"expected the two snapshot tests plus this one to skip:\n{completed.stderr[-1500:]}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
