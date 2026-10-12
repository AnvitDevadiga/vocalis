import re

import httpx
import pytest

from vocalis.engine import GoogleCalendar, ProviderError
import app as module
from fastapi.testclient import TestClient
from vocalis.store import Store
from vocalis import appointments as appointment_service


@pytest.mark.asyncio
async def test_google_calendar_refreshes_and_retries_one_unauthorized_request(monkeypatch):
    monkeypatch.setenv('GOOGLE_CALENDAR_TENANT_ID', 'tenant-a')
    monkeypatch.setenv('GOOGLE_CALENDAR_ID', 'business@example.com')
    monkeypatch.setenv('GOOGLE_CALENDAR_CLIENT_ID', 'client-id')
    monkeypatch.setenv('GOOGLE_CALENDAR_CLIENT_SECRET', 'client-secret')
    monkeypatch.setenv('GOOGLE_CALENDAR_REFRESH_TOKEN', 'refresh-token')
    monkeypatch.delenv('GOOGLE_CALENDAR_ACCESS_TOKEN', raising=False)
    seen = {'refreshes': 0, 'api_tokens': []}

    def respond(request):
        if request.url.host == 'oauth2.googleapis.com':
            seen['refreshes'] += 1
            assert b'grant_type=refresh_token' in request.content
            return httpx.Response(200, json={'access_token': f'token-{seen["refreshes"]}', 'expires_in': 3600})
        assert request.url.host == 'www.googleapis.com'
        assert 'business%40example.com' in str(request.url)
        token = request.headers['Authorization']
        seen['api_tokens'].append(token)
        if token == 'Bearer token-1':
            return httpx.Response(401, json={'error': 'expired'})
        return httpx.Response(200, json={'items': []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    calendar = GoogleCalendar('tenant-a')
    assert await calendar.request('GET', '/events') == {'items': []}
    assert await calendar.request('GET', '/events') == {'items': []}
    assert seen == {'refreshes': 2, 'api_tokens': ['Bearer token-1', 'Bearer token-2', 'Bearer token-2']}


@pytest.mark.asyncio
async def test_google_calendar_rejects_partial_refresh_configuration(monkeypatch):
    monkeypatch.setenv('GOOGLE_CALENDAR_TENANT_ID', 'tenant-a')
    monkeypatch.setenv('GOOGLE_CALENDAR_ID', 'primary')
    monkeypatch.setenv('GOOGLE_CALENDAR_CLIENT_ID', 'client-id')
    monkeypatch.delenv('GOOGLE_CALENDAR_CLIENT_SECRET', raising=False)
    monkeypatch.delenv('GOOGLE_CALENDAR_REFRESH_TOKEN', raising=False)
    monkeypatch.setenv('GOOGLE_CALENDAR_ACCESS_TOKEN', 'old-access-token')
    with pytest.raises(ProviderError, match='incomplete'):
        await GoogleCalendar('tenant-a').request('GET', '/events')


@pytest.mark.asyncio
async def test_google_event_id_is_valid_and_retries_reuse_it(monkeypatch):
    calendar = GoogleCalendar('tenant-a')
    created = []

    async def fake_request(method, path, body=None, params=None):
        if method == 'GET' and path.startswith('/events/'):
            if created:
                return created[0]
            request = httpx.Request('GET', 'https://example.test')
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError('missing', request=request, response=response)
        if method == 'GET':
            return {'items': []}
        assert method == 'POST'
        assert re.fullmatch(r'[0-9a-v]{5,1024}', body['id'])
        created.append({'id': body['id'], 'start': body['start']})
        return created[0]

    monkeypatch.setattr(calendar, 'request', fake_request)
    first = await calendar.book('Alex', 'Visit', '2027-01-01T10:00:00+00:00', '2027-01-01T10:30:00+00:00', 'same-key')
    second = await calendar.book('Alex', 'Visit', '2027-01-01T10:00:00+00:00', '2027-01-01T10:30:00+00:00', 'same-key')
    assert first['external_id'] == second['external_id']
    assert len(created) == 1


@pytest.mark.asyncio
async def test_calendar_availability_paginates_and_excludes_current_event(monkeypatch):
    calendar = GoogleCalendar('tenant-a')
    requested = []

    async def fake_request(method, path, body=None, params=None):
        requested.append(dict(params))
        if not params.get('pageToken'):
            return {'items':[{'id':'same-event','status':'confirmed'}],'nextPageToken':'next'}
        return {'items':[{'id':'other-event','status':'confirmed'}]}

    monkeypatch.setattr(calendar,'request',fake_request)
    available = await calendar.available('2027-01-01T10:00:00+00:00','2027-01-01T10:30:00+00:00',exclude_event_id='same-event')
    assert available is False
    assert requested[1]['pageToken'] == 'next'
    assert requested[0]['orderBy'] == 'startTime'


@pytest.mark.asyncio
async def test_calendar_availability_ignores_cancelled_and_transparent_events(monkeypatch):
    calendar = GoogleCalendar('tenant-a')

    async def fake_request(method, path, body=None, params=None):
        return {'items':[{'id':'cancelled','status':'cancelled'},
                         {'id':'free','status':'confirmed','transparency':'transparent'}]}

    monkeypatch.setattr(calendar,'request',fake_request)
    assert await calendar.available('2027-01-01T10:00:00+00:00','2027-01-01T10:30:00+00:00') is True


@pytest.mark.asyncio
async def test_google_reschedule_uses_etag_and_retry_marker(monkeypatch):
    calendar = GoogleCalendar('tenant-a')
    event = {'id':'event-1','etag':'etag-1','status':'confirmed','start':{'dateTime':'2027-01-01T10:00:00+00:00'},
             'extendedProperties':{'private':{'existing':'value'}}}
    calls = []

    async def fake_request(method, path, body=None, params=None, headers=None):
        calls.append((method,path,body,headers))
        if method == 'GET' and path == '/events/event-1':
            return dict(event)
        if method == 'GET' and path == '/events':
            return {'items':[dict(event)]}
        if method == 'PATCH':
            assert headers == {'If-Match':'etag-1'}
            event['start'] = body['start']
            event['extendedProperties'] = body['extendedProperties']
            return {'id':'event-1'}
        raise AssertionError((method,path))

    monkeypatch.setattr(calendar,'request',fake_request)
    first = await calendar.reschedule('event-1','2027-01-01T11:00:00+00:00','2027-01-01T11:30:00+00:00','reschedule-1')
    second = await calendar.reschedule('event-1','2027-01-01T11:00:00+00:00','2027-01-01T11:30:00+00:00','reschedule-1')
    assert first == second == {'ok':True,'external_id':'event-1'}
    assert event['extendedProperties']['private'] == {'existing':'value','vocalis_reschedule_key':'reschedule-1'}
    assert sum(call[0] == 'PATCH' for call in calls) == 1


def test_local_reschedule_is_idempotent_and_protects_slot(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    key = 'tenant-api-key'
    tenant = store.create_tenant('Clinic',module.hash_key(key))
    original = store.book(tenant,'Alex','5551234','Visit','2027-01-01T10:00:00+00:00','2027-01-01T10:30:00+00:00','booking-key-1')
    competing = store.book(tenant,'Bea','5559876','Visit','2027-01-01T11:00:00+00:00','2027-01-01T11:30:00+00:00','booking-key-2')
    monkeypatch.setattr(module,'store',store)
    client = TestClient(module.app)
    headers = {'x-api-key':key}
    url = f"/api/appointments/{original['appointment_id']}/reschedule"
    body = {'start_at':'2027-01-01T13:00:00+01:00','duration_minutes':30,'idempotency_key':'reschedule-key-1'}
    moved = client.post(url,headers=headers,json=body)
    assert moved.status_code == 200
    assert moved.json()['start_at'] == '2027-01-01T12:00:00+00:00'
    assert client.post(url,headers=headers,json=body).json() == moved.json()
    reused = client.post(url,headers=headers,json={**body,'start_at':'2027-01-01T14:00:00+00:00'})
    assert reused.status_code == 409
    conflict = client.post(url,headers=headers,json={**body,'start_at':'2027-01-01T11:15:00+00:00','idempotency_key':'reschedule-key-2'})
    assert conflict.status_code == 409
    by_id = {appointment['id']:appointment for appointment in store.appointments(tenant)}
    assert by_id[original['appointment_id']]['start_at'] == moved.json()['start_at']
    assert by_id[competing['appointment_id']]['start_at'] == '2027-01-01T11:00:00+00:00'


@pytest.mark.asyncio
async def test_external_reschedule_recovers_after_provider_timeout(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    tenant = store.create_tenant('Clinic','key')
    original = store.book(tenant,'Alex','5551234','Visit','2027-01-01T10:00:00+00:00','2027-01-01T10:30:00+00:00','booking-key-1')
    with store.connect() as db:
        db.execute('UPDATE appointments SET external_id=? WHERE id=?',('google-event-1',original['appointment_id']))
    monkeypatch.setenv('GOOGLE_CALENDAR_TENANT_ID',tenant)
    state = {'start_at':'2027-01-01T10:00:00+00:00','key':None,'calls':0}

    class FakeCalendar:
        def __init__(self, tenant_id):
            assert tenant_id == tenant

        def validate_configuration(self):
            pass

        async def reschedule(self, event_id, start_at, end_at, key):
            state['calls'] += 1
            if state['key'] == key:
                return {'ok':True,'external_id':event_id}
            state['start_at'],state['key'] = start_at,key
            raise TimeoutError('provider updated the event but the response was lost')

    monkeypatch.setattr(appointment_service,'GoogleCalendar',FakeCalendar)
    start='2027-01-01T12:00:00+00:00'
    end='2027-01-01T12:30:00+00:00'
    first=await appointment_service.reschedule_appointment(store,tenant,original['appointment_id'],start,end,'reschedule-key-1')
    assert first == {'ok':False,'error':'calendar_outcome_unknown'}
    assert store.appointments(tenant)[0]['start_at']=='2027-01-01T10:00:00+00:00'
    blocked_cancel=store.reserve_appointment_operation(tenant,original['appointment_id'],'cancel','cancel-key-1')
    assert blocked_cancel['status']=='pending'
    retry=await appointment_service.reschedule_appointment(store,tenant,original['appointment_id'],start,end,'reschedule-key-1')
    assert retry == {'ok':True,'appointment_id':original['appointment_id'],'start_at':start,'end_at':end}
    assert store.appointments(tenant)[0]['start_at']==start
    assert state['calls']==2


def test_availability_api_checks_local_slots_across_timezones(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    key = 'tenant-api-key'
    tenant = store.create_tenant('Clinic',module.hash_key(key))
    store.book(tenant,'Alex','5551234','Visit','2027-01-01T10:00:00+00:00','2027-01-01T10:30:00+00:00','booking-key-1')
    monkeypatch.setattr(module,'store',store)
    client = TestClient(module.app)
    response = client.post('/api/availability',headers={'x-api-key':key},json={
        'start_at':'2027-01-01T12:00:00+02:00','duration_minutes':30,
    })
    assert response.status_code == 200
    assert response.json()['available'] is False
    assert response.json()['source'] == 'local'


def test_availability_api_fails_closed_when_calendar_cannot_be_checked(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    key = 'tenant-api-key'
    tenant = store.create_tenant('Clinic',module.hash_key(key))
    monkeypatch.setattr(module,'store',store)
    monkeypatch.setenv('GOOGLE_CALENDAR_TENANT_ID',tenant)

    class UnavailableCalendar:
        def __init__(self, tenant_id):
            pass

        async def available(self, start, end):
            raise TimeoutError('calendar unavailable')

    monkeypatch.setattr(module,'GoogleCalendar',UnavailableCalendar)
    client = TestClient(module.app)
    response = client.post('/api/availability',headers={'x-api-key':key},json={
        'start_at':'2027-01-01T10:00:00+00:00','duration_minutes':30,
    })
    assert response.status_code == 503


def test_external_calendar_cancel_recovers_after_lost_response(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    key = 'tenant-api-key'
    tenant = store.create_tenant('Clinic', module.hash_key(key))
    start = '2027-01-01T10:00:00+00:00'
    result = store.book(tenant,'Alex','5551234','Visit',start,'2027-01-01T10:30:00+00:00','booking-key-1')
    with store.connect() as db:
        db.execute('UPDATE appointments SET external_id=? WHERE tenant_id=? AND id=?',('google-event-1',tenant,result['appointment_id']))
    monkeypatch.setattr(module,'store',store)
    monkeypatch.setenv('GOOGLE_CALENDAR_TENANT_ID',tenant)
    state = {'deleted': False, 'calls': 0}

    class FakeCalendar:
        def __init__(self, tenant_id):
            assert tenant_id == tenant

        def validate_configuration(self):
            pass

        async def cancel(self, event_id):
            assert event_id == 'google-event-1'
            state['calls'] += 1
            if not state['deleted']:
                state['deleted'] = True
                raise TimeoutError('provider deleted the event but the response was lost')
            return {'ok': True}

    monkeypatch.setattr(module,'GoogleCalendar',FakeCalendar)
    client = TestClient(module.app)
    url = f"/api/appointments/{result['appointment_id']}?idempotency_key=cancel-key-1"
    headers = {'x-api-key':key}
    first = client.delete(url,headers=headers)
    assert first.status_code == 503
    assert store.appointments(tenant)[0]['status'] == 'confirmed'
    retry = client.delete(url,headers=headers)
    assert retry.status_code == 200
    assert store.appointments(tenant)[0]['status'] == 'cancelled'
    repeated = client.delete(url,headers=headers)
    assert repeated.status_code == 200
    assert repeated.json() == retry.json()
    assert state['calls'] == 2


def test_cancel_idempotency_key_is_bound_to_appointment(tmp_path, monkeypatch):
    store = Store(tmp_path/'db.sqlite')
    key = 'tenant-api-key'
    tenant = store.create_tenant('Clinic', module.hash_key(key))
    start = '2027-01-01T10:00:00+00:00'
    first = store.book(tenant,'Alex','5551234','Visit',start,'2027-01-01T10:30:00+00:00','booking-key-1')
    second = store.book(tenant,'Alex','5551234','Visit','2027-01-01T11:00:00+00:00','2027-01-01T11:30:00+00:00','booking-key-2')
    monkeypatch.setattr(module,'store',store)
    client = TestClient(module.app)
    headers = {'x-api-key':key}
    first_url = f"/api/appointments/{first['appointment_id']}?idempotency_key=cancel-key-1"
    second_url = f"/api/appointments/{second['appointment_id']}?idempotency_key=cancel-key-1"
    assert client.delete(first_url,headers=headers).status_code == 200
    assert client.delete(second_url,headers=headers).status_code == 409
    assert store.appointments(tenant)[1]['status'] == 'confirmed'
