# Current state

## Product

Vocalis is Anvit Tewarika's personal voice assistant for arranging meetings. Visitors can talk or type, Vocalis checks calendar availability, asks for explicit confirmation, and creates a calendar event. The public build is a portfolio project focused on meeting requests.

## Implemented

- FastAPI backend with tenant-scoped PostgreSQL/SQLite storage and authenticated management APIs.
- Groq language and speech transcription, Edge TTS, browser WebSocket conversation, and explicit privacy consent.
- Local and Google Calendar booking adapters with idempotency, retry recovery, conflict checks, and cancellation/rescheduling APIs.
- Calendar readiness check. Public appointments fail closed unless the Google Calendar integration is configured.
- Configurable conflict calendars through `GOOGLE_CALENDAR_BUSY_IDS`, with events written only to `GOOGLE_CALENDAR_ID`.
- Event-level reminders at 24 hours and 2 hours before each confirmed booking.
- Responsive black-and-white portfolio and voice interface with an animated voice orb and reduced-motion support.
- Deterministic tests, CI, deployment docs, privacy page, and phased roadmap.

## Needs owner action

- Add the locally secured Google client ID, client secret, and refresh token to Render. The owner should enter these in the service's secret settings.
- Set the destination Vocalis calendar and the busy calendars (`primary` and Vocalis) in Render.
- Confirm work days/hours and choose the preferred speech voice after listening to the deployed service.

## Known limits

- Google OAuth consent is complete and a refresh token is stored in the ignored local credentials file. The hosted integration still needs Render configuration and live booking validation; bookings remain unavailable until then.
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
| `static/assistant.html` | Render-hosted visitor voice and text interface |
| `site/` | GitHub Pages portfolio and privacy page |
| `tests/` | Unit, integration, conversation, security, and realtime checks |
| `docs/` | Architecture, integration, deployment, evaluation, security, and roadmap |
| `alembic/` | PostgreSQL schema migrations |
