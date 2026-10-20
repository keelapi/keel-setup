# Customer voice gateway preparation

The versioned `integration_profiles.v1.json` is the shared preparation contract. Every new
voice profile is unqualified. Provider key validation, signed gateway evidence, final managed-tool
authorization, provider acceptance, external business effect and reconciliation are separate results.
A passing text-model proof supplies none of the voice results.

Install the maintained customer gateway from the public release’s checksummed gateway archive
and SDK wheel. Customers need no access to private source repositories. The archive contains
`keel-controlled-gateways/fintech-voice/gateway_runtime.py`; it reuses `keel-voice-sidecar` transports
and hosted Keel's existing managed MCP executor. The banking WebRTC broker is a reference path;
OpenAI and ElevenLabs WebSocket gateways are distinct transport profiles. Direct ElevenLabs WebRTC
is unsupported by this release's qualification contract. Never substitute an ngrok or founder-local
address in a customer's production architecture.

1. Pin the API/SDK and gateway to released immutable commits. Verify the setup bundle manifest,
   `VOICE_RELEASE.json`, `VOICE_SHA256SUMS`, SDK version/checksum, gateway revision and profile contract digest.
   Install the verified wheel and archive requirements in a dedicated virtual environment. Stop on drift or unavailable
   artifacts. Preserve the customer's existing work.
2. Prepare a customer-owned processing notice and privacy profile before enabling capture. Freeze
   provider, recipients, jurisdictions, processing, recording, retention/deletion, secondary use,
   notice version/digest and consent method. Counsel determines applicability and adequacy;
   real-person audio requires customer counsel sign-off. Synthetic prerecorded fixtures are the
   first verification input. Do not create default notice wording or infer consent from narration.
3. Keep the permanent provider key, webhook HMAC secret and private evidence signing key in the
   customer gateway's secret store. Keep business credentials in the MCP adapter. The browser and
   agent receive no permanent provider/business credential. Never paste secrets into conversation,
   fixtures, logs or feedback. Diagnostic sinks must exclude media, transcripts, notice wording,
   arguments containing conversational content, temporary provider credentials and webhook bodies.
4. In a human-controlled terminal, `keel-voice-prepare-key --path PRIVATE_KEY_PATH --key-id KEY_ID`
   creates or reloads one private stable Ed25519 key and prints public enrollment material only.
   A separate administrator enrolls that public material using `POST /v1/voice/sidecar-keys`.
   Do not regenerate keys at each restart. Rotation requires new enrollment and a newly bound runtime
   credential/profile; disable the old credential and key through human-owned administration.
5. A human administrator registers the gateway agent and binds its client credential using
   `PUT /v1/voice/gateway-profiles/{api_key_id}`. The frozen profile contains the exact gateway release,
   provider agent identity where applicable, initiation configuration, privacy profile, signing key,
   duration/concurrency limits and a closed list of managed server UUID/tool/schema-hash bindings.
   Compute `tool_catalog_digest` over the sorted RFC 8785 catalog. Each contract must already be
   verified; mutation requires a new runtime credential. Enroll the Stripe test adapter with its
   separate preflight HMAC secret; bearer authentication alone is not dispatch authority.
6. Read back private provider agent settings using explicitly designated sandbox credentials.
   For ElevenLabs, select an existing immutable `agent_version_id`, verify its parent agent and
   read back that exact version; bind it into initiation and signed-URL requests. Setup never enables
   provider versioning or changes customer agents automatically. Compare the tool catalog, voice/model,
   override permissions, recording, retention, secondary use
   and closure controls with the pinned configuration. Zero retention is a customer privacy choice,
   not a universal statutory requirement. Missing readback or configuration drift keeps activation
   disabled. Signed-URL expiry is not proof that the provider stopped processing or billing.
7. Price admission remains fail closed. A customer administrator enrolls an immutable project
   qualification with `POST /v1/voice/pricing-profiles`, reads its digest using
   `GET /v1/voice/pricing-profiles/{profile_id}`, and revokes it using `DELETE` on that path.
   Bind `pricing_profile_id` and `pricing_profile_digest` into the ElevenLabs gateway enrollment.
   Set `risk_budget_total_micros` in the customer runner configuration to cover the qualified
   provider ceiling plus any managed action reservations. No production rates are supplied.
   The profile names one exact provider/model/operation, complete all-inclusive meter coverage,
   contract and qualification evidence digests, a positive decimal USD-micros rate, billing
   increment, maximum units and timezone-aware validity interval. Overlapping live qualifications
   for that scope are rejected; changes require a new identity. Other project or request-supplied
   rates have no authority. Do not qualify a contract with additional uncovered LLM, telephony,
   minimum or other charges as one covered meter.
   Direct TTS measures exact input characters; qualified Direct STT accepts bounded complete PCM
   WAV and measures its duration before dispatch. Compressed/remote STT remains denied. Native
   Agents uses the explicitly qualified `elevenlabs.metadata.cost` units in the authenticated
   callback projection. A signed provider-start receipt binds the pricing identity, digest and
   reservation to the operator's pinned configuration and budget. Native settlement waits for both
   session closure and verified usage. Missing duration/cost units, unknown units, over-ceiling
   usage or outstanding action holds keep USD unavailable and reservations pending. After pending
   child action settlement, the owner may explicitly retry `POST /v1/sessions/{session_id}/voice-reconciliation`;
   that endpoint uses existing verified evidence, not caller-provided rates or usage.
   Contract-calculated USD is distinct from independently verified billed charges. Duration and
   concurrency limits and a reserved estimate do not establish a hard dollar cap or prove billing
   stopped. Keep activation disabled until the full pricing and provider protocol are qualified.
8. Customer capture starts only after authenticated notice acceptance and a single-use local consent
   receipt. Keel admission precedes provider initiation; provider acknowledgement is bound before
   audio or tools. Every tool, including preflights, uses managed MCP `:call`. The test adapter verifies
   Keel's short-lived exact-wire proof and durably consumes it before business calls. Explicit customer
   action confirmation is separate from model arguments such as `confirmed:true`.
9. Verify two concrete sandbox tools: a bounded account read and a bounded test transfer to an
   allowlisted destination. Exercise allow, deny and pending review with independent adapter/provider
   counters and downstream readback. Polling never resumes execution. A human explicitly resumes the
   same frozen request after identity-backed review. Unknown completion requires reconciliation;
   it must not become “nothing happened” or an automatic retry.
10. Withdraw consent and close server-owned media before reporting evidence. Exercise cutoff,
    network loss, restart, stale/ended sessions, forged reports, duplicate/late callbacks and credential
    reuse. Verify one-use admissions, trustworthy provider binding and zero prohibited tool dispatch.
    Inspect hosted requests, WAL, logs, traces, exports and callback ingress for sensitive content.
    Direct TTS/STT has a different media boundary: submitted content passes through hosted Keel.

Record the final tuple (API revision, SDK checksum, gateway revision, setup bundle SHA, Dashboard
revision, provider agent/settings digest, model/voice/transport, tools/schema hashes, verifier keys,
privacy profile and pricing profile). Report source/local/sandbox/deployed/activated evidence separately.
No source, mock, deployment or signed marker establishes universal enforcement or legal compliance.

Upgrades use a new pinned tuple and new runtime profile credential. Rollback closes existing provider
connections, disables customer activation and retains incomplete evidence/outboxes for reconciliation.
Never restart an interrupted action automatically. Health checks may inspect readiness and pending
records without starting capture, acquiring credentials, changing authority or contacting providers.
