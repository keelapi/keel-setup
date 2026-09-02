# Authorable field vocabulary

Generated from the Keel policy-authoring catalog, version `2026-08-14.1`. The catalog is the
table below. The guidance around it is maintained by hand and does **not** regenerate with the
table — re-apply it if you regenerate.

**`CATALOG_VERSION` does not move when a field's provenance does.** Five financial fields were
demoted from `keel_derived` to `caller_asserted` under this same version string, and the stale
table below gave no sign of it. Re-check provenance against the published field-provenance map
for your release rather than trusting the version to signal drift.

Use **only** fields marked authorable = yes. Fields with provenance `caller_asserted` are
supplied by the caller and are not trustworthy evidence — Keel rejects allow/deny rules that
gate on spoofable identity fields.

`context._keel.action_envelope.*` is generally the strongest family available, but **it is not
uniformly Keel-established.** The connector, authority and pricing facts are `keel_derived`.
The financial facts are not: on Keel's canonical Permit payment rail their values come from the
caller's own request attributes, and the catalog labels them `caller_asserted`. Read the
provenance column per field rather than trusting the prefix.

## Authoring against `action_envelope`

`keel_derived` says Keel established the value. It does not by itself say the value is
dependable, and the family splits three ways.

### Establish-anywhere: author freely

- `context._keel.action_envelope.connector.identity.value` — which connector the action went to
  (`stripe`, `github`, …).
- `context._keel.action_envelope.connector.tool_name.value` — which tool on that connector.

Both are facts Keel read off its own registry or the verified decision trace, on every surface
where they are available at all, so their meaning does not move when Keel's classifiers change.
They are the only two envelope paths Keel publishes as a stable authoring contract.

### The financial family: authorable, but caller-derived on the Permit payment rail

`financial.amount_usd_micros`, `financial.amount`, `financial.currency`,
`financial.operation` and `financial.destination_digest` are all `caller_asserted` in the
catalog. That is not a warning to avoid them — a spend threshold has nowhere else to go — but it
changes what they prove:

- **On an MCP payment connector** Keel's semantic adapter reads these out of a schema-validated
  tool call and genuinely derives them.
- **On the canonical Permit payment rail** they come from the caller's own `requested_amount`
  and `requested_currency` attributes. `amount_usd_micros` is `requested_amount × 1,000,000`,
  computed by Keel over two values the caller chose. Keel binds the result to the decision and
  will not accept a different amount at dispatch, but it never checked the amount against
  anything outside the request.

So a threshold on `amount_usd_micros` still fires on an agent that inflates the amount, and the
bound value is the one that must be dispatched. What it is not is a floor: an agent that wants
to stay under a cap can declare a small amount, or a non-USD currency that leaves the field
unestablished entirely. **Always pair the threshold with the companion rule below** — that is
what catches the side-step, and it matters more here than the threshold does.

Say this in the handoff when you author a spend cap. A user who believes Keel measured the
amount is relying on something Keel does not do on that rail.

### `moves_customer_value`: usable, but never `eq true` alone

`financial.moves_customer_value` is `keel_derived` on every surface and reads Keel's action
registry directly. It is the only surface-independent way to ask *"does this move money?"*, and
it is the right field for an absence guard.

The one thing it cannot do is carry a deny on its own. An action Keel could not classify — a
release of hold, a reversal, a credit note, a void — reports `.state` `unknown` with a null
value, which is not `true`, so `deny when … eq true` passes it without firing. Gate on
`.state` alongside the value, exactly as with a fallible amount.

It is also a classifier judgment rather than an extracted fact, so its meaning can be refined as
classification improves. Pin what you depend on with a test.

### Still moving underneath: check before relying on these

Both remain valid fields and Keel accepts rules that use them; the caution is about churn, not
correctness.

- `context._keel.action_envelope.action.access_level.value` — **the classification behind it is
  still being reworked.** Tags have been reclassified in place and tool keys renamed underneath
  it, so a rule written today can change what it matches without the policy changing. Prefer an
  explicit `connector.identity` + `connector.tool_name` pair where you can.
- `context._keel.action_envelope.action.risk_tags.value` — the tag set for any given tool comes
  from the same registry, which is actively being curated. A `contains "spend"` rule does what
  it says today; which tools carry the tag can change as tools are classified. Check a recent
  decision in the dashboard for the tools you care about, and pin the behaviour with a test.

If a restriction can only be expressed with one of these, say so plainly instead of
approximating it — see `../SKILL.md`, *Check expressibility early*.

### The same call does not always produce the same envelope

Which envelope facts are populated depends on the route the call took into Keel, so a rule can
be live on one route and permanently silent on another. `connector.tool_name` is not supplied
at all on realtime/voice, so any rule keyed on it never matches there. Check a recent decision
in the Keel dashboard for the route the application actually uses before relying on a fact
being present.

### `amount_usd_micros` is populated only when the currency is USD

Keel does not convert. A non-USD call leaves the amount unestablished, and a comparison against
an unestablished value is inapplicable — it resolves false, so the rule **stays silent instead
of denying**. A threshold written on `.value` alone therefore does nothing on exactly the
requests it was written to catch, and nothing is observed failing.

Gate on `.state`, and pair the threshold with a companion rule covering the case Keel could not
establish the amount. What should happen to an action Keel could not price is a user policy
decision — see `../SKILL.md`, *Facts that can be unavailable*.

## The catalog

⚠ marks a field whose classification is still moving underneath it — check a recent decision
before relying on it; see *Still moving underneath* above. Provenance `caller_asserted` on a
`financial.*` field is not a defect and not a reason to avoid it; see *The financial family*.

| field | type | authorable | provenance |
|---|---|---|---|
| `action.attributes.trusted_facts.environment` | string | yes | connector_asserted |
| `action.attributes.trusted_facts.max_uses` | integer | yes | connector_asserted |
| `action.attributes.trusted_facts.tool_name` | string | yes | connector_asserted |
| `action.name` | string | yes | caller_asserted |
| `attrs.estimated_input_tokens` | integer | yes | caller_asserted |
| `attrs.estimated_output_tokens` | integer | yes | caller_asserted |
| `attrs.execution_mode` | enum | yes | caller_asserted |
| `attrs.max_output_tokens_requested` | integer | yes | caller_asserted |
| `attrs.model` | string | yes | caller_asserted |
| `attrs.operation` | enum | yes | caller_asserted |
| `attrs.provider` | string | yes | caller_asserted |
| `budget_envelope_id` | uuid | yes | keel_derived |
| `context._keel.action_access_confidence` | enum | no | keel_derived |
| `context._keel.action_access_level` | enum | yes | keel_derived |
| `context._keel.action_access_map_version` | string | no | keel_derived |
| `context._keel.action_access_source` | enum | no | keel_derived |
| `context._keel.action_access_unknown_reason` | enum | no | keel_derived |
| `context._keel.action_envelope.action.access_level.value` ⚠ | enum | yes | keel_derived |
| `context._keel.action_envelope.action.risk_tags.value` ⚠ | enum_array | yes | keel_derived |
| `context._keel.action_envelope.authority.compute_lineage_only.value` | boolean | yes | keel_derived |
| `context._keel.action_envelope.authority.is_mutation.state` | enum | yes | keel_derived |
| `context._keel.action_envelope.authority.is_mutation.value` | boolean | yes | keel_derived |
| `context._keel.action_envelope.connector.identity.value` | string | yes | keel_derived |
| `context._keel.action_envelope.connector.tool_name.value` | string | yes | keel_derived |
| `context._keel.action_envelope.connector.tool_schema_hash.state` | enum | yes | keel_derived |
| `context._keel.action_envelope.financial.amount.state` | enum | yes | keel_derived |
| `context._keel.action_envelope.financial.amount.value` | integer | yes | caller_asserted |
| `context._keel.action_envelope.financial.amount_usd_micros.state` | enum | yes | keel_derived |
| `context._keel.action_envelope.financial.amount_usd_micros.value` | integer | yes | caller_asserted |
| `context._keel.action_envelope.financial.currency.value` | string | yes | caller_asserted |
| `context._keel.action_envelope.financial.destination_digest.state` | enum | yes | keel_derived |
| `context._keel.action_envelope.financial.destination_digest.value` | string | yes | caller_asserted |
| `context._keel.action_envelope.financial.moves_customer_value.state` | enum | yes | keel_derived |
| `context._keel.action_envelope.financial.moves_customer_value.value` | boolean | yes | keel_derived |
| `context._keel.action_envelope.financial.operation.value` | string | yes | caller_asserted |
| `context._keel.action_envelope.pricing.estimated_cost_usd_micros.state` | enum | yes | keel_derived |
| `context._keel.action_envelope.pricing.estimated_cost_usd_micros.value` | integer | yes | keel_derived |
| `context._keel.action_risk_tags` | enum_array | yes | keel_derived |
| `context._keel.agent_identity_verified` | boolean | yes | keel_derived |
| `context._keel.connector_identity` | string | no | keel_derived |
| `context._keel.intent_mismatch` | boolean | yes | keel_derived |
| `context._keel.payment_action_verified` | boolean | yes | keel_derived |
| `context._keel.payment_amount_usd_micros` ‡ | integer | yes | keel_derived |
| `context._keel.project_plan` | enum | yes | keel_derived |
| `context._keel.request_day_of_week` | integer | yes | keel_derived |
| `context._keel.request_hour_utc` | integer | yes | keel_derived |
| `context._keel.verified_agent_principal_id` | string | yes | keel_derived |
| `context.provider_meta.data_retention` | string | yes | keel_derived |
| `context.provider_meta.region` | string | yes | keel_derived |
| `estimated_cost` | number | yes | keel_derived |
| `estimated_cost_usd_micros` | integer | yes | keel_derived |
| `model` | string | yes | keel_derived |
| `org_id` | uuid | yes | keel_derived |
| `project_id` | uuid | yes | keel_derived |
| `provider` | string | yes | keel_derived |
| `token_estimate` | integer | yes | keel_derived |

‡ `context._keel.payment_amount_usd_micros` is the **flat** spelling of the same number as
`action_envelope.financial.amount_usd_micros.value`, and the catalog still declares it
`keel_derived`. That claim was corrected on the envelope field and not on this one, so the two
disagree about one value. It also has no companion `.state`, so the absence rule below cannot
be expressed with it. Prefer the envelope pair.

## Operators

`eq`, `neq`, `in`, `not_in`, `gt`, `gte`, `lt`, `lte`, `contains`, `exists`,
`starts_with`, `ends_with`, `matches_regex`, `len_gt`, `len_gte`, `len_lt`, `len_lte`

Not every operator is valid on every field; the server validates the pairing and says so.
At most 10 `matches_regex` conditions per document; unsafe regex constructs are rejected.
