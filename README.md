# Keel setup

This is Keel's public, release-only bundle for agent-assisted onboarding. It contains the two skills a
coding agent needs to inspect an application, prepare a bounded Keel integration, and help a human
author a friendly policy without taking authority from that human.

## Skills

- [`keel-setup`](keel-setup/SKILL.md) inventories execution paths, prepares state-D integration, and
  verifies one intended allow and one intended deny through `POST /v1/execute`.
- [`keel-policy`](keel-policy/SKILL.md) translates human intent into Keel's canonical policy document
  and reports where the requested rule is unsupported or unenforceable.

Read [`shared/CONSTITUTION.md`](shared/CONSTITUTION.md), then read the selected `SKILL.md` in full.
Use an immutable 40-character commit SHA. Verify `SHA256SUMS` before running a bundled helper. Do not
use `curl | sh` or reconstruct a missing release from memory.

## Credential boundary

The copied setup prompt and this repository contain no Keel credential or customer identifier. A
coding agent must never ask a human to paste a credential into a conversation. When runtime
verification is reached, the human creates a client-scoped key in the Keel dashboard and installs it
as `KEEL_API_KEY` outside the transcript. An environment variable is transcript hygiene, not process
isolation.

Only `keel-setup/scripts/verify_execute.py` performs network I/O. It makes the two bounded verification
requests described by the setup skill, only after the human-owned key is installed. The remaining
helpers are local, read-only analysis or schema validation.

## Provenance

`SOURCE.json` identifies this public bundle version and the files maintained only by the publication
layer. This repository intentionally excludes internal specifications, production workflows, private
repository history, and unrelated tools.

Apache-2.0. See [`LICENSE`](LICENSE).
