# Vocalis delivery roadmap

This roadmap tracks the product brief. A phase is complete only after its acceptance checks pass with real providers where stated. The public GitHub Pages URL hosts the portfolio page; it cannot run the voice API.

Current owner preference (12 October 2026): use the existing Groq free-tier account and keep hosting free. The target is a public portfolio voice demo on Render with a separate PostgreSQL database; no paid OpenAI voice, phone service, or hosting is authorized. The frontend visual redesign will follow when the owner provides references.

## Phase 0 — honest pilot baseline (complete)

- Preserve the original prototype and document its limits.
- Provide tenant-scoped SQLite data, authenticated APIs, browser turns, Twilio Gather webhooks, local booking, escalation records, deterministic tests, Docker files, and CI.
- Publish a static project page without claiming that the voice backend is live.

## Phase 1 — reliable backend core (in progress)

- Reject idempotency-key reuse across different booking requests or operations.
- Reserve external calendar slots locally before provider calls; keep unknown outcomes pending and recover by retrying the same key. Add an operator reconciliation view before production.
- PostgreSQL storage, versioned Alembic schema, and a fail-closed SQLite-to-PostgreSQL transfer command are implemented alongside SQLite. CI provisions PostgreSQL for migration-backed integration tests. Verified SQLite/PostgreSQL backup tooling and automatic call-state retention are implemented; automated off-host scheduling, restore drills, non-call PII lifecycle policies, and further tenant-isolation review remain.
- Make external actions recoverable: reserve intent, call provider with an idempotency key, reconcile uncertain outcomes, and confirm only after both provider and local state agree.
- Durable Twilio webhook claims, response caching, and atomic call-state commits are implemented in SQLite. The process-local rate-limit map is memory-bounded, but distributed replay controls and quotas, additional request limits, and tests for live provider failure recovery remain.
- HubSpot lead capture now writes a local outbox atomically, with leased claims, stale-worker fencing, exponential retry backoff, an authenticated retry API, and an optional Compose worker. Live CRM validation and a multi-tenant credential model remain.
- Acceptance: deterministic CI plus database-backed integration tests pass; duplicate requests and concurrent booking cannot create extra appointments.

## Phase 2 — browser voice demo

- Current target: host the existing Groq + Whisper + Edge TTS utterance-based demo on a free backend. Keep the standard API key on the backend. This is not low-latency speech-to-speech; a WebRTC Realtime provider path is deferred because it requires separate provider and billing approval.
- Add VAD, interruption, cancellation, transcripts, state synchronization, and measured latency stages. Keep business tool execution on the trusted server.
- Select a high-quality feminine-sounding voice by listening to available samples; do not promise the exact ChatGPT app voice or voice cloning without a licensed, consented source.
- Acceptance: a public HTTPS browser demo completes a real conversation, can be interrupted naturally, and records actual timing without exposing secrets.

## Phase 3 — business capabilities

- Add tenant-isolated document ingestion, grounded retrieval with source metadata, and uncertainty handling.
- Calendar availability, booking, cancellation, and rescheduling are implemented behind optional Google credentials. Add complete CRM lookup/upsert and transfer or structured human follow-up.
- Add typed tool contracts, permissions, timeouts, retry/reconciliation behavior, and tests with real sandbox accounts.
- Acceptance: end-to-end scenarios modify the expected external accounts once and provide traceable confirmation.

## Phase 4 — phone and operations

- Configure a real phone number and streaming or SIP telephony path; validate inbound calls and escalation.
- Browser demo consent, configurable call-state retention, PII-safe retention logs, verified database backup tooling, health probes, protected request metrics, Docker restart behavior, and a free preview deployment blueprint are implemented. Hosted deployment, automated off-host backup schedule, restore drills, monitoring/alert destination, provider outage validation, phone-call consent, and privacy/legal review remain.
- Acceptance: live test calls and fault drills pass in a hosted staging environment.

## Phase 5 — evaluation, frontend, portfolio

- Run seeded multi-turn simulations for booking, FAQ, interruption, injection, failure, and escalation. Separate deterministic checks from model and human review.
- Build the dashboard and call detail views using real data. Apply the user-provided design direction when available.
- Publish an architecture diagram, real demo, screenshots, reproducible evaluation results, and measured latency with sample sizes.
- Acceptance: CI is green, the public demo works, results are reproducible, and the portfolio claims match observed behavior.

## Inputs required from the owner

1. For the free portfolio demo: provide the existing Groq API key directly in Render's secret field. Do not paste it into chat or commit it. Ensure the Groq account remains on its Free tier.
2. For real phone calls: a Twilio account, a purchased or verified number, and an auth token configured as a deployment secret. Carrier/SIP options can be evaluated before purchase.
3. For business actions: a test Google Calendar and HubSpot test account or alternative providers, with least-privilege credentials and sample business rules (hours, timezone, services, escalation number).
4. For deployment: connect the GitHub repository to Render and create a free Neon PostgreSQL database. GitHub Pages continues to host only the static portfolio site. Render's free instance sleeps when idle, so the first request can be slow.
5. For voice identity and frontend: preferred sample voices and, later, the design references. A custom cloned voice requires rights and explicit speaker consent.

Keys are not needed to implement or test local deterministic backend work. They are needed to verify real provider calls and a public voice demo.
