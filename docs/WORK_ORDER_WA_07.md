# Work Order — WA-07 Media, Hardening, Observability and E2E (#79)

## Objective
Close the WhatsApp AI Assistant experience with media reuse, operational hardening, sanitized observability and end-to-end validation while preserving the canonical Family Finance domain and October financial semantics.

## Scope
- Reuse existing authorized capture/transcription/OCR pipelines for WhatsApp audio, image, receipt and document inputs; do not create a second extraction or financial engine.
- Route media-derived materially complete facts through the same typed draft → preview → explicit human confirmation → persist flow as text. Ambiguous extraction must clarify and must not write.
- Harden webhook/provider boundaries: authentication/signature verification where supported, secrets handling, bounded retries/backoff, rate limiting, replay/idempotency protection and explicit timeouts/failure isolation.
- Add sanitized stage observability for inbound, orchestration, tool call, draft, confirmation, outbound and error. No raw secrets or unnecessary raw financial content in logs/metrics.
- Add health checks and operational metrics that reveal availability/failure/latency without exposing financial payloads.
- Add controlled E2E tests with fake provider and no real credentials; cover concurrency, redelivery, restart, duplicate delivery, authorization, household isolation, RBAC and prompt injection.
- Produce runbook for setup, operation, troubleshooting, secret rotation and rollback.
- Revalidate recurring cost/external dependencies before production enablement. Any paid recurring dependency not already approved must follow the documented Technical Challenge process and must not be silently enabled.

## Acceptance criteria
- Text, audio and supported media converge on the same canonical financial domain and typed-action contracts.
- No financial write occurs without explicit authorized human confirmation, including media-derived drafts.
- Provider/LLM failure cannot corrupt or block the Family Finance web application.
- Retry, replay, redelivery and process restart cannot duplicate financial facts.
- Observability is useful but sanitized; secrets and raw financial payloads are not leaked.
- Household/user authorization is derived server-side and cannot be overridden by media, LLM output or conversation state.
- Applicable CI, PostgreSQL, security, Docker and E2E gates are green.
- Production enablement remains gated by documented cost/dependency approval where required.

## Financial and assistant invariants
- Confirmed balance remains sovereign at the observed instant; divergence is visible/auditable and never masked.
- Invoice payment is not a new expense; internal transfer is not income/expense; investment application/redemption is not synthetic income/expense.
- REALIZADO, COMPROMETIDO, PREVISTO and HIPÓTESE remain distinct.
- LLM/media extraction understands and proposes; Family Finance validates, calculates and persists.
- Hypothesis/extraction confidence never becomes fact silently.
- Every write preserves user, timestamp, original message/media reference, interpretation, typed action, affected records, before/after where applicable and undo/audit semantics.
- Probable duplicate remains fail-safe/human-mediated; no automatic destructive deduplication.

## Mandatory tests
- Fake-provider E2E for text, audio and representative image/document/receipt media using existing extraction seams.
- Media ambiguity/low-confidence path returns clarification with zero mutation.
- Explicit confirmation required after media draft; replay of confirmation and inbound provider event is idempotent.
- Concurrent/redelivered messages, restart/replay and colliding provider IDs cannot duplicate facts.
- Household/user/RBAC isolation, including malicious attempts to select another household through prompt/media/context.
- Prompt injection/tool-schema fail-closed behavior for text and extracted media content.
- Webhook auth/signature/replay/rate-limit/timeout/retry failure paths.
- Log/metric sanitization tests proving secrets and representative financial payloads are absent.
- LLM/provider unavailable path leaves core web/finance flows operational.
- PostgreSQL integration and migration/rollback safety if schema changes; lint and all applicable GitHub Actions.

## Risks / review gates
Review privacy/PII retention, media lifecycle, provider payload storage, secret rotation, webhook authenticity, SSRF/file handling, MIME/size limits, decompression/resource exhaustion, retry storms, race conditions, idempotency key scope, audit/undo, migration compatibility, Docker health/dependency isolation, observability cardinality and cost.

## Prohibitions
- No second OCR/transcription/parser/financial engine when an existing canonical pipeline can be reused.
- No raw provider secrets, tokens, full financial messages or unnecessary media content in logs/metrics.
- No production credential in repository or CI fixtures.
- No write from media/LLM without explicit authorized confirmation.
- No static question catalog, LLM-generated SQL or free ORM expressions.
- No Oracle/OCI implementation, migration or deployment work. Issue #79 mentions Docker/OCI validation, but repository-level Oracle/OCI #59–65 and any Oracle migration/deploy remain explicitly frozen by owner direction. Validate only non-Oracle Docker/runtime behavior that is already part of main.
- No WA-07 scope expansion into unrelated backlog.
- Claude must not merge.

## Execution
Claude implements on this branch/PR, documents reused media seams, threat model decisions, cost/dependency findings, migrations/rollback if any, and all test evidence. Integration authority reviews and merges only at a stable expected head SHA with green applicable CI and no unresolved technical/documentation blocker.