# Implementation plan

## Completed in this change

1. Audit and preserve the prototype.
2. Remove fake production metrics from the served UI and README.
3. Add tenant-scoped persistent records, authenticated APIs, conservative conversation runtime, browser demo, signed inbound Twilio adapter, and deterministic tests.
4. Add local Docker Compose and CI test job.

## Required before production

1. PostgreSQL persistence, Alembic migrations, tenant-composite foreign keys, transactional tenant locks, a leased CRM outbox, and a fail-closed SQLite-to-PostgreSQL transfer tool are implemented and covered by local PostgreSQL integration tests. Private verified backup commands and automatic call-state retention are implemented. Configure off-host backup scheduling, execute restore drills, and define appointment/lead/knowledge retention before production.
2. Replace utterance-at-a-time voice with supported bidirectional telephony streaming, streaming STT/LLM/TTS, VAD, jitter buffering, and measured stage timing. Add acoustic barge-in and concurrency/load tests.
3. Build a complete typed tool registry. Harden external side effects with two-phase confirmation, cross-client calendar race handling, reconciliation for pending operations, CRM sync, and approved messaging. Calendar availability, booking, cancellation, and rescheduling exist as API operations but have not been verified with live provider credentials.
4. Add PDF ingestion, embedding/vector retrieval, citations/grounding verification, deletion, and per-tenant index controls.
5. Build the Next.js administration app with identity, RBAC, call details, knowledge/integration settings, and operational analytics.
6. Add seeded multi-turn simulation, deterministic scoring, optional model judges, human review, and CI thresholds. Collect real provider traces before claiming performance.
7. Add tenant-scoped secret storage, HubSpot OAuth refresh, phone recording/consent controls, PII redaction, distributed per-tenant rate limiting, abuse controls, and security review. Browser demo consent, PII-safe call-retention logs, and single-tenant Google Calendar token refresh are implemented.
8. Validate live Twilio, Google Calendar, HubSpot, Groq, Whisper, TTS, Docker deployment, and failure modes in a staging environment.
