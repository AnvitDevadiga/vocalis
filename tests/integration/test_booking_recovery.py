from datetime import datetime, timedelta, timezone

import pytest

from vocalis import booking
from vocalis.store import Store


@pytest.mark.asyncio
async def test_uncertain_calendar_result_keeps_slot_and_retry_recovers(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    tenant = store.create_tenant('Clinic', 'key')
    monkeypatch.setenv('GOOGLE_CALENDAR_TENANT_ID', tenant)
    start = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=2, minutes=30)).isoformat()
    calls = []

    class FakeCalendar:
        def __init__(self, tenant_id):
            assert tenant_id == tenant

        def validate_configuration(self):
            pass

        async def book(self, name, service, start_at, end_at, key):
            calls.append(key)
            if len(calls) == 1:
                raise TimeoutError('provider response lost')
            return {'ok': True, 'external_id': 'google-event-1', 'start_at': start_at}

    monkeypatch.setattr(booking, 'GoogleCalendar', FakeCalendar)
    args = (store, tenant, 'Alex', '5551234', 'Checkup', start, end)
    first = await booking.book_appointment(*args, 'same-key')
    assert first == {'ok': False, 'error': 'calendar_outcome_unknown'}
    assert store.appointments(tenant)[0]['status'] == 'pending'
    assert store.book(tenant, 'Other', '5559876', 'Checkup', start, end, 'other-key')['error'] == 'slot_unavailable'
    assert await booking.book_appointment(*args, 'same-key') == {'ok': False, 'error': 'booking_pending'}
    assert calls == ['same-key']

    with store.connect() as db:
        db.execute('UPDATE appointments SET reservation_at=? WHERE tenant_id=?',
                   ((datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat(), tenant))
    recovered = await booking.book_appointment(*args, 'same-key')
    assert recovered['ok']
    assert store.appointments(tenant)[0]['status'] == 'confirmed'
    assert store.appointments(tenant)[0]['external_id'] == 'google-event-1'
    assert await booking.book_appointment(*args, 'same-key') == recovered
    assert calls == ['same-key', 'same-key']
    with pytest.raises(ValueError, match='idempotency key reused'):
        await booking.book_appointment(store, tenant, 'Different', '5551234', 'Checkup', start, end, 'same-key')


@pytest.mark.asyncio
async def test_calendar_rejection_releases_slot_and_is_idempotent(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    tenant = store.create_tenant('Clinic', 'key')
    monkeypatch.setenv('GOOGLE_CALENDAR_TENANT_ID', tenant)
    start = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=2, minutes=30)).isoformat()

    class FullCalendar:
        def __init__(self, tenant_id):
            pass

        def validate_configuration(self):
            pass

        async def book(self, *args):
            return {'ok': False, 'error': 'slot_unavailable'}

    monkeypatch.setattr(booking, 'GoogleCalendar', FullCalendar)
    args = (store, tenant, 'Alex', '5551234', 'Checkup', start, end, 'rejected-key')
    first = await booking.book_appointment(*args)
    assert first == {'ok': False, 'error': 'slot_unavailable'}
    assert await booking.book_appointment(*args) == first
    assert store.appointments(tenant)[0]['status'] == 'rejected'
    assert store.book(tenant, 'Other', '5559876', 'Checkup', start, end, 'other-key')['ok']


@pytest.mark.asyncio
async def test_missing_calendar_credentials_do_not_reserve_a_slot(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    tenant = store.create_tenant('Clinic', 'key')
    monkeypatch.setenv('GOOGLE_CALENDAR_TENANT_ID', tenant)
    monkeypatch.setenv('GOOGLE_CALENDAR_ID', 'primary')
    for name in ('GOOGLE_CALENDAR_ACCESS_TOKEN', 'GOOGLE_CALENDAR_CLIENT_ID',
                 'GOOGLE_CALENDAR_CLIENT_SECRET', 'GOOGLE_CALENDAR_REFRESH_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    start = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=2, minutes=30)).isoformat()
    result = await booking.book_appointment(store, tenant, 'Alex', '5551234', 'Checkup', start, end, 'missing-key')
    assert result == {'ok': False, 'error': 'calendar_not_configured'}
    assert store.appointments(tenant) == []


@pytest.mark.asyncio
async def test_public_assistant_never_falls_back_to_local_booking(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    tenant = store.create_tenant('Vocalis', 'key')
    monkeypatch.setenv('VOCALIS_PUBLIC_ASSISTANT','1')
    monkeypatch.setenv('VOCALIS_PUBLIC_TENANT_ID',tenant)
    monkeypatch.delenv('GOOGLE_CALENDAR_TENANT_ID',raising=False)
    start = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=2,minutes=30)).isoformat()
    result = await booking.book_appointment(store,tenant,'Alex','5551234','Meeting',start,end,'public-assistant-key')
    assert result == {'ok':False,'error':'calendar_not_configured'}
    assert store.appointments(tenant) == []
