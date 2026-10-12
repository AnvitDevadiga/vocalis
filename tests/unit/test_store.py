import hashlib
import pytest
from datetime import datetime, timedelta, timezone
from vocalis.store import Store


def test_tenant_isolation_and_idempotency(tmp_path):
    store = Store(tmp_path/'db.sqlite')
    a = store.create_tenant('A',hashlib.sha256(b'a').hexdigest())
    b = store.create_tenant('B',hashlib.sha256(b'b').hexdigest())
    did = store.add_document(a,'Policy','A only')
    assert store.documents(b) == []
    call = store.create_call(a,'browser')
    assert store.call(b,call) is None
    store.event(a,call,'turn_completed',{'response_ms':5})
    assert store.events(b,call) == []
    start = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    end = (datetime.now(timezone.utc)+timedelta(days=1,minutes=30)).isoformat()
    first = store.book(a,'Ann','123','Checkup',start,end,'key-0001')
    assert first['ok']
    assert store.book(a,'Ann','123','Checkup',start,end,'key-0001') == first
    assert store.book(a,'Bob','456','Checkup',start,end,'key-0002')['error'] == 'slot_unavailable'
    assert store.book(b,'Bob','456','Checkup',start,end,'key-0003')['ok']
    assert len(store.appointments(a)) == 1
    assert store.cancel(b,first['appointment_id'],'key-0004')['ok'] is False
    assert store.cancel(a,first['appointment_id'],'key-0005')['ok']
    assert store.book(a,'Bob','456','Checkup',start,end,'key-0006')['ok']
    assert store.accept_webhook('twilio','event-1',a)
    assert not store.accept_webhook('twilio','event-1',a)


def test_lead_deduplicates_per_tenant(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    a=store.create_tenant('A','hash-a')
    b=store.create_tenant('B','hash-b')
    lead=store.upsert_lead(a,'Ann','123')
    assert store.upsert_lead(a,'Anne','123') == lead
    assert store.upsert_lead(b,'Bob','123') != lead
    assert store.leads(a)[0]['name'] == 'Anne'


def test_overlapping_booking_is_rejected(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('A','a')
    base=datetime.now(timezone.utc)+timedelta(days=2)
    first=store.book(tenant,'A','123','One',base.isoformat(),(base+timedelta(minutes=60)).isoformat(),'book-0001')
    assert first['ok']
    overlapping=store.book(tenant,'B','456','Two',(base+timedelta(minutes=30)).isoformat(),(base+timedelta(minutes=90)).isoformat(),'book-0002')
    assert overlapping['error']=='slot_unavailable'


def test_idempotency_key_cannot_be_reused_for_different_operation(tmp_path):
    store = Store(tmp_path/'db.sqlite')
    tenant = store.create_tenant('A','a')
    start = datetime.now(timezone.utc)+timedelta(days=2)
    end = start+timedelta(minutes=30)
    result = store.book(tenant,'Ann','123','Visit',start.isoformat(),end.isoformat(),'same-key')
    assert result['ok']
    assert store.prior_booking(tenant,'Ann','123','Visit',start.isoformat(),end.isoformat(),'same-key') == result
    with pytest.raises(ValueError, match='idempotency key reused'):
        store.book(tenant,'Bob','456','Visit',start.isoformat(),end.isoformat(),'same-key')
    with pytest.raises(ValueError, match='idempotency key reused'):
        store.prior_booking(tenant,'Bob','456','Visit',start.isoformat(),end.isoformat(),'same-key')
    with pytest.raises(ValueError, match='idempotency key reused'):
        store.cancel(tenant,result['appointment_id'],'same-key')


def test_audio_duration_limit_uses_actual_sample_rate():
    import asyncio
    from vocalis.engine import WhisperSTT
    with pytest.raises(ValueError, match='30 seconds'):
        asyncio.run(WhisperSTT().transcribe(b'\x00\x00' * (8000 * 31), 8000))


def test_overlapping_bookings_across_store_instances_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path/'db.sqlite'
    first_store = Store(path)
    second_store = Store(path)
    tenant = first_store.create_tenant('A','a')
    start = datetime.now(timezone.utc)+timedelta(days=3)
    barrier = Barrier(2)

    def reserve(store, offset, key):
        barrier.wait()
        begin = start+timedelta(minutes=offset)
        return store.book(tenant,'Alex','123','Visit',begin.isoformat(),(begin+timedelta(minutes=30)).isoformat(),key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(reserve, first_store, 0, 'key-one')
        two = pool.submit(reserve, second_store, 15, 'key-two')
        results = [one.result(), two.result()]
    assert sum(result['ok'] for result in results) == 1
    assert len(first_store.appointments(tenant)) == 1


def test_existing_pilot_database_migrates_without_losing_appointments(tmp_path):
    import sqlite3

    path = tmp_path/'old.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE appointments (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, external_id TEXT, name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL, UNIQUE(tenant_id, start_at))')
        db.execute('CREATE TABLE idempotency (tenant_id TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(tenant_id,key))')
        start = (datetime.now(timezone.utc)+timedelta(days=4)).isoformat()
        end = (datetime.now(timezone.utc)+timedelta(days=4, minutes=30)).isoformat()
        db.execute('INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?,?)', ('old-id','old-tenant',None,'Alex','123','Visit',start,end,'confirmed'))
    store = Store(path)
    assert store.appointments('old-tenant')[0]['id'] == 'old-id'
    assert store.book('old-tenant','Other','456','Visit',start,end,'new-key')['error'] == 'slot_unavailable'


def test_webhook_turn_claim_is_durable_and_state_commit_is_atomic(tmp_path):
    path = tmp_path/'db.sqlite'
    first_store = Store(path)
    second_store = Store(path)
    tenant = first_store.create_tenant('A','a')
    call = first_store.create_call(tenant,'twilio',call_id='CA123')
    first = first_store.claim_webhook_turn(tenant,'twilio:CA123:1','hash-one')
    assert first['status'] == 'claimed'
    assert second_store.claim_webhook_turn(tenant,'twilio:CA123:1','hash-one')['status'] == 'pending'
    assert second_store.claim_webhook_turn(tenant,'twilio:CA123:1','other-hash')['status'] == 'conflict'
    with pytest.raises(KeyError):
        first_store.finish_webhook_turn(tenant,'twilio:CA123:1',first['token'],'<Response/>','wrong-call',{'history':[]},{'response_ms':1})
    assert first_store.call(tenant,call)['state'] == {}
    assert first_store.claim_webhook_turn(tenant,'twilio:CA123:1','hash-one')['status'] == 'pending'
    body = first_store.finish_webhook_turn(tenant,'twilio:CA123:1',first['token'],'<Response/>',call,
                                           {'history':[{'role':'user','content':'Hello'}]},{'response_ms':1})
    assert body == '<Response/>'
    assert second_store.claim_webhook_turn(tenant,'twilio:CA123:1','hash-one') == {'status':'complete','response':'<Response/>'}
    assert first_store.call(tenant,call)['state']['history'][0]['content'] == 'Hello'
    assert sum(e['kind']=='turn_completed' for e in first_store.events(tenant,call)) == 1


def test_stale_webhook_claim_can_be_recovered(tmp_path):
    store = Store(tmp_path/'db.sqlite')
    tenant = store.create_tenant('A','a')
    first = store.claim_webhook_turn(tenant,'twilio:CA123:1','hash-one')
    with store.connect() as db:
        db.execute('UPDATE webhook_turns SET lease_until=? WHERE tenant_id=?',
                   ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),tenant))
    second = store.claim_webhook_turn(tenant,'twilio:CA123:1','hash-one')
    assert second['status'] == 'claimed'
    assert second['token'] != first['token']
    with pytest.raises(ValueError, match='claim was lost'):
        store.finish_webhook_turn(tenant,'twilio:CA123:1',first['token'],'old','CA123')


def test_call_retention_is_dry_run_by_default_and_deletes_only_expired_call_data(tmp_path):
    store = Store(tmp_path/'db.sqlite')
    tenant = store.create_tenant('A','a')
    old_ended = store.create_call(tenant,'browser',call_id='old-ended')
    stale_active = store.create_call(tenant,'browser',call_id='stale-active')
    recent = store.create_call(tenant,'browser',call_id='recent')
    old = (datetime.now(timezone.utc)-timedelta(days=45)).isoformat()
    with store.connect() as db:
        db.execute("UPDATE calls SET status='ended',started_at=?,ended_at=? WHERE id=?", (old,old,old_ended))
        db.execute('UPDATE calls SET started_at=? WHERE id=?', (old,stale_active))
        db.execute("UPDATE calls SET status='ended',ended_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(),recent))
    cutoff = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()

    assert store.prune_call_data(cutoff, batch_size=1, apply=False) == 2
    assert store.call(tenant,old_ended) is not None
    assert store.prune_call_data(cutoff, batch_size=1, apply=True) == 2
    assert store.call(tenant,old_ended) is None
    assert store.call(tenant,stale_active) is None
    assert store.call(tenant,recent) is not None
    assert store.events(tenant,old_ended) == []
