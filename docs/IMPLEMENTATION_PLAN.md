# Implementation plan

## Completed in this change

1. Audit and preserve the prototype.
2. Remove fake production metrics from the served UI and README.
3. Add tenant-scoped persistent records, authenticated APIs, conservative conversation runtime, browser demo, signed inbound Twilio adapter, and deterministic tests.
4. Add local Docker Compose and CI test job.

## Required before production

1. Move to PostgreSQL with Alembic migrations, transactional outbox, durable distributed idempotency, backups, and retention deletion jobs.
2. Replace utterance-at-a-time voice with supported bidirectional telephony streaming, streaming STT/LLM/TTS, VAD, jitter buffering, and measured stage timing. Add acoustic barge-in and concurrency/load tests.
3. Build a complete typed tool registry. Add two-phase confirmation for external side effects, calendar concurrency control, reconciliation after partial failures, rescheduling, CRM sync, and approved messaging.
4. Add PDF ingestion, embedding/vector retrieval, citations/grounding verification, deletion, and per-tenant index controls.
5. Build the Next.js administration app with identity, RBAC, call details, knowledge/integration settings, and operational analytics.
6. Add seeded multi-turn simulation, deterministic scoring, optional model judges, human review, and CI thresholds. Collect real provider traces before claiming performance.
7. Add secret vault, OAuth refresh, recording/consent controls, PII redaction, per-tenant rate limiting, abuse controls, and security review.
8. Validate live Twilio, Google Calendar, HubSpot, Groq, Whisper, TTS, Docker deployment, and failure modes in a staging environment.
