"""Appointment orchestration with a durable local reservation before external effects."""

import os

from vocalis.engine import GoogleCalendar, ProviderError
from vocalis.public_config import is_tenant as is_public_assistant_tenant


async def book_appointment(store, tenant_id, name, phone, service, start_at, end_at, key):
    public_calendar_required = is_public_assistant_tenant(tenant_id)
    if public_calendar_required and os.getenv('GOOGLE_CALENDAR_TENANT_ID') != tenant_id:
        return {'ok': False, 'error': 'calendar_not_configured'}

    prior = store.prior_booking(tenant_id, name, phone, service, start_at, end_at, key)
    if prior is not None:
        if public_calendar_required and prior.get('ok'):
            appointment = store.one('SELECT external_id FROM appointments WHERE tenant_id=? AND id=?',
                                    (tenant_id, prior.get('appointment_id')))
            if not appointment or not appointment['external_id']:
                return {'ok': False, 'error': 'calendar_outcome_unknown'}
        return prior

    if os.getenv('GOOGLE_CALENDAR_TENANT_ID') != tenant_id and not public_calendar_required:
        return store.book(tenant_id, name, phone, service, start_at, end_at, key)

    calendar = GoogleCalendar(tenant_id)
    try:
        calendar.validate_configuration()
    except ProviderError:
        return {'ok': False, 'error': 'calendar_not_configured'}

    reservation = store.reserve_external_booking(tenant_id, name, phone, service, start_at, end_at, key)
    if not reservation['ok']:
        return {'ok': False, 'error': reservation.get('error', 'slot_unavailable')}
    if not reservation['claimed']:
        return {'ok': False, 'error': 'booking_pending'}

    try:
        external = await calendar.book(name, service, start_at, end_at, key)
    except Exception:
        # The provider may have succeeded before a timeout. Keep the reservation;
        # retrying this key later first checks the deterministic provider event ID.
        return {'ok': False, 'error': 'calendar_outcome_unknown'}
    if not external['ok']:
        return store.reject_external_booking(tenant_id, reservation['appointment_id'], key, external['error'])
    return store.finish_external_booking(tenant_id, reservation['appointment_id'], key, external['external_id'], start_at)
