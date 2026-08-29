# Examples

These are illustrations of the JSON shape, not a menu of protections.

| Example | Plan | Review mechanism |
|---|---|---|
| `stripe-refund-approval.json` | Production | `require_attestation` by the project owner |
| `stripe-refund-approval-enterprise.json` | Enterprise | typed `approval_requirement` for the `admin` organization role |

Both refund examples pair every fallible `amount_usd_micros.value` comparison with
`amount_usd_micros.state == "present"` and route unavailable amounts to review. A non-USD or
unpriced refund therefore cannot miss both amount rules and fall through to the policy default.

**Every restriction in every example here was explicitly requested by the user in that
scenario.** The thresholds, denies, approver roles and timeouts are that user's policy
decisions — none of them are schema defaults, and none of them carry over.

Copying a restriction from an example into a policy whose user did not ask for it is invention,
and it is the specific failure the skill is written to prevent. See `../SKILL.md` — *The five
invariants*.
