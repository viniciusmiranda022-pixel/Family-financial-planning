# Work Order — WA-01: Gateway WhatsApp, webhook e autorização

Issue: #73. Parent: #71. Depends on: #72 / ADR WA-00 merged in PR #102.

## Objective
Implement the secure WhatsApp transport foundation without allowing any real financial mutation.

## Scope
- Decoupled WhatsApp Business/Cloud API gateway following `ADR_WA_00_AI_ASSISTANT_DISCOVERY.md`.
- Webhook verification/authenticity, fail-closed processing and normalized inbound/outbound envelopes.
- Server-side allowlist mapping an authorized phone identity to `User` and `household`; household/user identifiers are never trusted from inbound payloads.
- Minimal metadata persistence for canonical message/event correlation and idempotency. Do not retain raw webhook payloads or message text by default.
- Canonical provider message/event deduplication, replay protection and bounded rate limiting.
- Secrets/tokens only from environment/secret store; never commit, persist in clear text, echo to UI, or log them.
- Fake provider and deterministic tests; CI must not call Meta or any external LLM/API.
- Docker integration consistent with current `main`, but Oracle/OCI implementation/deployment is explicitly out of scope.

## Authority and privacy rules
- WA-01 is transport/authentication only. It MUST NOT create/update/delete `Transaction`, `Obligation`, card/invoice, balance, asset, investment, or any other financial fact.
- Authorized non-admin household members are read/propose only; mutations require the existing typed-action authority boundary and administrative confirmation in later slices. Do not implement mutation execution here.
- Unauthorized numbers receive a generic rejection and must not learn whether a household/user/resource exists.
- Resolve identity and household once at the trusted gateway boundary and pass an internal trusted context downstream.
- Persist only identifiers/hashes/timestamps/status necessary for idempotency/audit. Sensitive message text belongs to transient processing unless a later normative requirement explicitly requires retention.

## Acceptance criteria
- Webhook challenge/verification and signed event validation are covered by tests and fail closed.
- Re-delivery of the same canonical event/message is idempotent and cannot trigger duplicate downstream processing.
- Two authorized identities can map independently to the same household without trusting inbound household data.
- Unauthorized identity cannot query or mutate data and receives no PII-bearing response.
- Replay/rate-limit behavior is deterministic and tested.
- Logs/tests demonstrate no secret, phone number, raw message text or financial PII leakage.
- No financial model/table is mutated by the WA-01 flow; add explicit regression tests for this prohibition.
- Any Alembic migration is additive, downgrade-safe, household-safe and contains no real/user-specific seed data.
- Full applicable CI green, including lint, PostgreSQL integration/migration tests and Docker checks when touched.

## Required engineering review evidence
Return exact HEAD SHA, changed-file inventory, architecture/security decisions, migration upgrade/downgrade analysis, idempotency/replay design, privacy/logging evidence, tests executed and CI run. Flag any divergence from the ADR with concrete evidence.

## Prohibitions
No WA-02+ orchestration, no real financial writes, no static question catalog, no LLM-generated SQL, no second financial engine, no paid API activation, no Oracle/OCI work, no real Meta credentials/numbers in repository/tests, and Claude must not merge.