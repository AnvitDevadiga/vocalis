import hashlib
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
