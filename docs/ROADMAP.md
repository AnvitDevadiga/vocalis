# Vocalis roadmap

Vocalis is Anvit Tewarika's personal voice assistant. A visitor can ask for a meeting, Vocalis checks existing calendar events, offers an available time for confirmation, and creates the event only after the visitor confirms. Google Calendar sends reminders one day and two hours before each event. Gmail notifications and paid voice or hosting services are out of scope for the current free portfolio demo.

## Phase 1 — product foundation (implemented)

- Reframed the portfolio and browser demo around one personal appointment assistant.
- Applied a monochrome, grid-based visual system with a voice orb, responsive layouts, motion states, and reduced-motion support.
- Added timezone-aware prompts, explicit booking confirmation, and fail-closed behavior when the public demo calendar is unavailable.
- Updated this roadmap to state implementation status and owner dependencies.

## Phase 2 — calendar correctness (implemented; live authorization pending)

- Check local reservations and every calendar listed in `GOOGLE_CALENDAR_BUSY_IDS` before asking for confirmation. Write events only to `GOOGLE_CALENDAR_ID`.
- Check again before creating an event; use deterministic event IDs and local reservations for retry safety.
- Add event-level Google Calendar popup reminders at 24 hours and 2 hours.
- Test pagination, transparent/cancelled events, conflicts across multiple calendars, idempotency, provider errors, and booking races.
- Live use still needs the Google account grant, a durable refresh token, and backend environment variables. The visitor demo must stay fail closed until these exist.

## Phase 3 — live calendar activation (owner action required)

- Complete the Google OAuth consent once for `anvit.works@gmail.com` with the already approved `calendar.events.owned` scope.
- Add the client ID, client secret, refresh token, demo tenant ID, destination calendar ID, and busy calendar list to Render's environment settings. The client secret and refresh token must remain private.
- Verify that an existing event on the primary calendar blocks a requested time, a free slot creates one event in the Vocalis calendar, repeated requests do not duplicate it, and both reminders are attached.
- Confirm the business timezone and working hours before adding office-hour restrictions. Current timezone default is `Asia/Kolkata`; working hours have not been supplied.

## Phase 4 — voice quality and visitor experience

- Listen to and compare supported feminine-sounding TTS voices; keep the selected voice configurable. The current free pipeline uses Groq transcription and language generation with Edge TTS. It is turn-based, not full-duplex speech-to-speech, and cannot promise the proprietary ChatGPT app voice.
- Add resilient reconnect/error states, an accessible transcript, and natural interruption behavior. Evaluate browser microphone behavior and latency on current mobile and desktop browsers.
- Keep the visitor's name, phone, and meeting purpose to the minimum needed. Add clear booking confirmation and a safe way to report calendar errors.

## Phase 5 — production readiness and evidence

- Verify Google, Groq, Neon, and Render behavior end to end; test simultaneous requests and uncertain provider outcomes.
- Add monitoring and alerting, scheduled backups, restore drills, abuse limits shared across instances, retention for appointment data, and incident procedures.
- Review OAuth access, privacy notice, data retention, and Google app verification requirements for the intended audience.
- Publish truthful architecture, test results, screenshots, and measured latency. Do not describe the free Render preview as a highly available production service.

## Current limits

- No Gmail notification workflow, paid telephony, user login, full duplex audio, streaming TTS, working-hour rules, or visitor reschedule/cancel UI.
- Google Calendar must be authorized and configured before real bookings are enabled. Automated tests use provider fakes and do not prove live Google behavior.
- Render free instances can sleep; the first request after inactivity may take longer.
