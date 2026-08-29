# Keel feedback report template

Preparing this report does not authorize transmission. Keep optional context empty unless the human
separately approves each field, validate offline, show the exact final payload, and let the human use
the appropriate manual channel. A security concern must use a private security channel.

```json
{
  "category": "integration_request",
  "summary": "Support automatic threshold authorization for this capability",
  "intended_task": "Allow small actions automatically and review larger actions",
  "expected_behavior": null,
  "observed_behavior": "The required value is not available as a trusted runtime fact",
  "desired_outcome": "Automatic authorization using a verified value",
  "surface": "mcp",
  "blocker": "trusted_fact_unavailable",
  "evidence_level": "source_inspected",
  "coding_agent": null,
  "skill_version": null,
  "keel_release": null,
  "decision_classification": null,
  "optional_context": {
    "provider": null,
    "tool_name": null,
    "source_locations": [],
    "environment_details": null
  }
}
```

Validate and preview locally:

```text
python3 shared/scripts/validate_feedback_report.py report.json --preview
```

For every populated optional-context field, add its separate flag only after the human approves that
specific disclosure, for example `--approve-context provider`. The flag records the operator's claim
for this local check; it does not prove the human approved disclosure. The validator never sends
anything and cannot prove complete redaction or private routing. Inspect the exact preview and choose
the manual destination yourself.
