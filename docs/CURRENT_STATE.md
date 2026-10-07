# Current state audit

## Before changes

The repository held one 921-line FastAPI application, one 2,738-line static HTML dashboard, a README, setup scripts, and a reference video. No tests or deployment files existed. `pytest -q` found no tests. The original app could not import in this environment because Groq/Edge TTS/Whisper were absent. The old browser used push-to-talk PCM over WebSocket, blocking per-turn STT, one non-streaming LLM call, then full-file TTS. There was no VAD, interruption, cancellation, telephony, LangGraph, RAG, provider abstraction, persistence, tenant boundary, or authenticated API. Calendar and CRM were in-memory mocks with pre-seeded people. The UI showed a hard-coded initial call count of 28; README latency, pricing, and performance assertions were unmeasured. Booking could silently choose a next-day fallback and the LLM could say a booking was confirmed before the mock calendar succeeded. Global settings and REST endpoints were unauthenticated. Runtime imports loaded Whisper at startup. Temporary audio files were cleaned, but no call records or audit events persisted. There was no Docker, CI, consent, retention, webhook, or deployment configuration.

## After changes

The original source is preserved as `legacy_app.py` and `static/index.html`. The served app is now `app.py`, with `vocalis/store.py` and `vocalis/engine.py`. It adds API-key tenant isolation, SQLite persistence for calls/events/documents/appointments/leads, idempotent local booking, signed Twilio voice webhooks, a browser voice/text demo, cancellation of in-flight browser turns, lexical retrieval, explicit escalation, optional Groq/Whisper/Edge TTS providers, and optional Google Calendar/HubSpot API adapters. Tests cover tenant isolation, API-key scope, cancellation, Twilio signature calculation, and local idempotency.

## Remaining gaps and risks

- **Real-time:** Browser sends a complete utterance; Whisper transcribes after release. Groq completion and Edge TTS synthesis are whole-response. Phone calls use Twilio Gather/Say. This is conversational voice, but not low-latency streaming. There is no server-side VAD or robust acoustic barge-in.
- **Persistence:** SQLite is local and single-node. No PostgreSQL, Alembic, Redis, backup/restore, or retention jobs. Cross-instance webhook serialization is absent.
- **RAG:** Tenant-scoped lexical chunk retrieval exists. No PDF parsing, embeddings, vector index, reranker, or reliable citation enforcement.
- **Tools:** Local booking/cancel and lead create are typed API operations. The voice runtime supports a narrow booking draft/confirmation path; a general typed conversational tool registry, rescheduling, availability, follow-up sending, and full transfer orchestration are not implemented. Google and HubSpot require live credentials and provider verification.
- **Frontend:** New static browser demo exists. No Next.js admin dashboard or call-detail views. The old dashboard is archived because it contains fake metrics and mock data.
- **Observability:** Events and actual server turn durations are stored and exposed. The requested stage-by-stage speech/LLM/TTS timing, tracing, cost accounting, and metric export are incomplete.
- **Security:** API keys are hashed in SQLite, but no key rotation, RBAC, SSO, encrypted credential vault, user identity, or configurable retention. Public demo rate limit is process-local. Signatures depend on a correctly configured public URL.
- **Evaluation:** Four seeded fixture scenarios and deterministic tests exist; no full simulated conversation corpus, judge/human review, hallucination rate, or evaluated live providers.
- **Deployment:** Single-service Compose works in principle; Docker build and actual provider call were not verified here. No production architecture or load validation.
