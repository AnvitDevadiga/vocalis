# Current state

## Product

Vocalis is Anvit Tewarika's personal voice assistant for arranging meetings. Visitors can talk or type, the assistant checks calendar availability, asks for explicit confirmation, and creates a calendar event. The public experience is a portfolio demo, not a general-purpose multi-tenant phone service.

## Implemented

- FastAPI backend with tenant-scoped PostgreSQL/SQLite storage and authenticated management APIs.
- Groq language and speech transcription, Edge TTS, browser WebSocket conversation, and explicit demo consent.
- Local and Google Calendar booking adapters with idempotency, retry recovery, conflict checks, and cancellation/rescheduling APIs.
- Public demo calendar readiness check. Public appointments fail closed unless the Google Calendar integration is configured.
- Configurable conflict calendars through `GOOGLE_CALENDAR_BUSY_IDS`, with events written only to `GOOGLE_CALENDAR_ID`.
- Event-level reminders at 24 hours and 2 hours before each confirmed booking.
- Responsive black-and-white portfolio and browser demo with an animated voice orb and reduced-motion support.
- Deterministic tests, CI, deployment docs, privacy page, and phased roadmap.

## Needs owner action

- Complete the Google OAuth account grant for `anvit.works@gmail.com` and provide a refresh token to Render through its secret settings. The user has asked us not to modify or inspect Render secrets.
- Set the destination Vocalis calendar plus busy calendars (`primary` and the Vocalis calendar) in Render.
- Confirm work days/hours and the preferred speech sample after hearing the deployed demo.

## Known limits

- The hosted Google integration has not yet been live-tested. Until OAuth and Render environment variables are configured, bookings fail closed.
- Voice is utterance based: the browser records a bounded clip, then transcription, response generation, and speech synthesis run in sequence. It is not low-latency full-duplex speech.
- Current free TTS is configurable Edge TTS. Voice quality varies by browser/network and does not reproduce a proprietary ChatGPT voice.
- No Gmail notification, authentication for public visitors, office-hour enforcement, visitor reschedule/cancel UI, shared distributed rate limiter, or production alerting is implemented.
- Render free hosting may sleep between requests. The project needs live provider verification, load testing, backup restore drills, and retention/privacy review before relying on it for business operations.

## Repository map

| Path | Purpose |
|---|---|
| `app.py` | FastAPI routes, API validation, health/readiness, and WebSocket entry points |
| `vocalis/engine.py` | Conversation runtime and Google/Groq/Whisper/TTS provider adapters |
| `vocalis/booking.py`, `vocalis/appointments.py` | Booking reservations, retries, cancellation, and rescheduling |
| `vocalis/store.py` | Tenant-scoped persistence and concurrency controls |
| `static/demo.html` | Render-hosted visitor voice and text interface |
| `site/` | GitHub Pages portfolio and privacy page |
| `tests/` | Unit, integration, conversation, security, and realtime checks |
| `docs/` | Architecture, integration, deployment, evaluation, security, and roadmap |
| `alembic/` | PostgreSQL schema migrations |
