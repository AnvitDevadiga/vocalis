# Integrations

## Twilio inbound voice

Create a tenant with `python -m vocalis.admin 'Business' --phone-number +15551234567 --transfer-number +15557654321`. Set `TWILIO_AUTH_TOKEN` and `VOCALIS_PUBLIC_BASE_URL`, then configure that number's voice webhook to `POST <base>/twilio/voice`. The response uses `<Gather input="speech">`; each gather action includes a turn number, which is used as a local idempotency key. A configured transfer number is dialed for escalation; otherwise an escalation is recorded as requiring follow-up. The integration has not been live-tested with an account in this environment. Twilio's [webhook signature guidance](https://www.twilio.com/docs/usage/webhooks/webhooks-security) describes the exact-URL requirement.

## Google Calendar

Set `GOOGLE_CALENDAR_TENANT_ID`, `GOOGLE_CALENDAR_ACCESS_TOKEN`, and `GOOGLE_CALENDAR_ID` for one tenant. Booking checks events for a proposed interval, then inserts an event with a deterministic ID derived from the idempotency key. A successful provider response is required before the API says a booking is confirmed. Access tokens need external refresh management. The adapter is not a complete scheduling system: cross-client race conditions, recurring events, resource calendars, business hours, event cancellation/rescheduling, and reconciliation are unfinished. Google documents [event insertion](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert) and [event ID constraints](https://developers.google.com/workspace/calendar/api/v3/reference/events).

## HubSpot

Set `HUBSPOT_TENANT_ID` and `HUBSPOT_ACCESS_TOKEN`. POST `/api/leads` with `sync_hubspot: true` and an email to request a HubSpot upsert keyed by email. The returned HubSpot ID is included in the response, and persisted to the lead row. Phone-only leads remain local. The adapter has not been live-tested and does not yet handle CRM field mapping, OAuth refresh, or conflict reconciliation. See the [HubSpot upsert endpoint](https://developers.hubspot.com/docs/api-reference/legacy/crm/objects/objects/batch/upsert-objects).
