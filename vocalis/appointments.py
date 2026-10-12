"""Appointment mutation orchestration for local and external calendars."""

import os

from vocalis.engine import GoogleCalendar


async def reschedule_appointment(store, tenant_id, appointment_id, start_at, end_at, key):
    claim = store.reserve_appointment_operation(tenant_id,appointment_id,'reschedule',key,start_at,end_at)
    if claim['status'] == 'complete':
        return claim['result']
    if claim['status'] == 'conflict':
        return {'ok':False,'error':'slot_unavailable'}
    if claim['status'] == 'not_found':
        return {'ok':False,'error':'appointment_not_found'}
    if claim['status'] != 'claimed':
        return {'ok':False,'error':'appointment_operation_pending'}

    appointment = store.one('SELECT external_id FROM appointments WHERE tenant_id=? AND id=?', (tenant_id,appointment_id))
    external_id = appointment['external_id']
    if external_id:
        if os.getenv('GOOGLE_CALENDAR_TENANT_ID') != tenant_id:
            store.release_appointment_operation(tenant_id,key,claim['token'])
            return {'ok':False,'error':'calendar_not_configured'}
        try:
            result = await GoogleCalendar(tenant_id).reschedule(external_id,start_at,end_at,key)
        except Exception:
            return {'ok':False,'error':'calendar_outcome_unknown'}
        if not result['ok']:
            return store.reject_appointment_operation(tenant_id,key,claim['token'],result['error'])
        external_id = result['external_id']
    return store.finish_appointment_operation(tenant_id,key,claim['token'],external_id)
