"""Appointment orchestration with a durable local reservation before external effects."""

import os

from vocalis.engine import GoogleCalendar


async def book_appointment(store, tenant_id, name, phone, service, start_at, end_at, key):
    prior = store.prior_booking(tenant_id, name, phone, service, start_at, end_at, key)
    if prior is not None:
        return prior

    if os.getenv('GOOGLE_CALENDAR_TENANT_ID') != tenant_id:
        return store.book(tenant_id, name, phone, service, start_at, end_at, key)

    reservation = store.reserve_external_booking(tenant_id, name, phone, service, start_at, end_at, key)
    if not reservation['ok']:
        return {'ok': False, 'error': reservation.get('error', 'slot_unavailable')}
    if not reservation['claimed']:
        return {'ok': False, 'error': 'booking_pending'}

    try:
        external = await GoogleCalendar(tenant_id).book(name, service, start_at, end_at, key)
    except Exception:
        # The provider may have succeeded before a timeout. Keep the reservation;
        # retrying this key later first checks the deterministic provider event ID.
        return {'ok': False, 'error': 'calendar_outcome_unknown'}
    if not external['ok']:
        return store.reject_external_booking(tenant_id, reservation['appointment_id'], key, external['error'])
    return store.finish_external_booking(tenant_id, reservation['appointment_id'], key, external['external_id'], start_at)
