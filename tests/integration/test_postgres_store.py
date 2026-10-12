import os
import json
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
psycopg = pytest.importorskip('psycopg')
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from vocalis.crm import process_pending
from vocalis.migrate_sqlite import TransferRefused, transfer_sqlite_to_postgres
from vocalis.store import Store


@pytest.fixture
def postgres_store(monkeypatch):
    base = os.getenv('VOCALIS_TEST_DATABASE_URL')
    if not base:
        pytest.skip('set VOCALIS_TEST_DATABASE_URL to run PostgreSQL integration tests')
    base_url = make_url(base)
    admin_url = base_url.set(drivername='postgresql').render_as_string(hide_password=False)
    schema = 'vocalis_test_' + uuid.uuid4().hex
    admin = psycopg.connect(admin_url,autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    test_url = base_url.set(drivername='postgresql+psycopg').update_query_dict(
        {'options':f'-csearch_path={schema}'}).render_as_string(hide_password=False)
    config = Config('alembic.ini')
    monkeypatch.setenv('VOCALIS_DATABASE_URL',test_url)
    try:
        command.upgrade(config,'head')
        store = Store(test_url)
        yield store
    finally:
        if 'store' in locals():
            store.engine.dispose()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


class FakeCRM:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    async def upsert(self, name, phone, email):
        return f'crm-{self.tenant_id}-{phone}'


@pytest.mark.asyncio
async def test_postgres_migrations_and_tenant_scoped_store(postgres_store):
    store = postgres_store
    a = store.create_tenant('Clinic A','hash-a')
    b = store.create_tenant('Clinic B','hash-b')
    call_id = store.create_call(a,'browser')
    foreign_call = store.create_call(b,'browser')
    store.event(a,call_id,'turn_completed',{'response_ms':12})
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.event(a,foreign_call,'forbidden',{})
    store.add_document(a,'Hours','Open on Monday')
    assert store.documents(a)[0]['content'] == 'Open on Monday'
    assert store.documents(b) == []
    assert store.events(a,call_id)[-1]['payload']['response_ms'] == 12

    first = store.book(a,'Alex','555-0100','Visit','2027-01-01T10:00:00+00:00',
                       '2027-01-01T10:30:00+00:00','postgres-book-1')
    assert first['ok']
    assert store.book(a,'Alex','555-0100','Visit','2027-01-01T10:00:00+00:00',
                      '2027-01-01T10:30:00+00:00','postgres-book-1') == first
    with pytest.raises(ValueError,match='idempotency key reused'):
        store.book(a,'Alex','555-0100','Visit','2027-01-01T11:00:00+00:00',
                   '2027-01-01T11:30:00+00:00','postgres-book-1')
    assert store.book(a,'Bea','555-0200','Visit','2027-01-01T10:15:00+00:00',
                      '2027-01-01T10:45:00+00:00','postgres-book-2')['error'] == 'slot_unavailable'

    second_store = Store(store.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        competing = list(pool.map(
            lambda item: item[0].book(a,item[1],'555-0300','Visit','2027-01-01T12:00:00+00:00',
                                      '2027-01-01T12:30:00+00:00',item[2]),
            [(store,'Casey','postgres-book-3'),(second_store,'Drew','postgres-book-4')]))
    second_store.engine.dispose()
    assert sum(result['ok'] for result in competing) == 1

    lead_id = store.upsert_lead(a,'Alex','555-0100','alex@example.test',queue_sync=True)
    assert (await process_pending(store,provider_factory=FakeCRM)) == [
        {'status':'synced','hubspot_id':f'crm-{a}-555-0100'}]
    assert store.leads(a)[0]['crm_sync'] == 'synced'
    assert store.leads(b) == []

    claim = store.claim_webhook_turn(a,'twilio:call:1','fingerprint')
    assert claim['status'] == 'claimed'
    completed = store.finish_webhook_turn(a,'twilio:call:1',claim['token'],'<Response/>',call_id,
                                          {'messages':[]},{'response_ms':8})
    assert completed == '<Response/>'
    replay = store.claim_webhook_turn(a,'twilio:call:1','fingerprint')
    assert replay == {'status':'complete','response':'<Response/>'}
    assert store.accept_webhook('twilio','event-1',a)
    assert not store.accept_webhook('twilio','event-1',a)

    assert len(store.appointments(a)) == 2
    assert store.leads(a)[0]['id'] == lead_id


def test_postgres_migration_can_downgrade_and_reapply(postgres_store):
    store = postgres_store
    store.engine.dispose()
    config = Config('alembic.ini')
    command.downgrade(config,'base')
    command.upgrade(config,'head')
    refreshed = Store(store.path)
    assert refreshed.one("SELECT version_num FROM alembic_version")['version_num'] == '0001_postgres'
    refreshed.engine.dispose()


def test_sqlite_transfer_preserves_rows_and_pending_operations(postgres_store,tmp_path):
    source = Store(tmp_path/'source.sqlite')
    tenant = source.create_tenant('Source Clinic','source-key-hash')
    call_id = source.create_call(tenant,'browser','555-0100')
    source.event(tenant,call_id,'transfer_fixture',{'ok':True})
    source.add_document(tenant,'Hours','Monday to Friday')
    booking = source.book(tenant,'Alex','555-0100','Visit','2027-01-01T10:00:00+00:00',
                          '2027-01-01T10:30:00+00:00','transfer-book-1')
    lead_id = source.upsert_lead(tenant,'Alex','555-0100','alex@example.test',queue_sync=True)
    operation = source.reserve_appointment_operation(
        tenant,booking['appointment_id'],'cancel','transfer-cancel-1')
    turn = source.claim_webhook_turn(tenant,'twilio:transfer:1','transfer-fingerprint')
    source.accept_webhook('twilio','transfer-event-1',tenant)

    counts = transfer_sqlite_to_postgres(source.path,postgres_store.path,batch_size=2)

    assert counts['tenants'] == 1
    assert counts['events'] == 2
    assert counts['lead_sync_jobs'] == 1
    assert counts['appointment_operations'] == 1
    assert operation['status'] == 'claimed'
    assert turn['status'] == 'claimed'
    assert postgres_store.tenant(tenant)['name'] == 'Source Clinic'
    assert postgres_store.call(tenant,call_id)['caller'] == '555-0100'
    assert postgres_store.documents(tenant)[0]['content'] == 'Monday to Friday'
    assert postgres_store.leads(tenant)[0]['id'] == lead_id
    assert postgres_store.lead_sync_job(tenant,lead_id)['status'] == 'pending'
    assert postgres_store.one('SELECT status FROM appointment_operations WHERE tenant_id=? AND key=?',
                              (tenant,'transfer-cancel-1'))['status'] == 'pending'
    assert postgres_store.one('SELECT status FROM webhook_turns WHERE tenant_id=? AND key=?',
                              (tenant,'twilio:transfer:1'))['status'] == 'pending'
    previous_event_max = max(event['id'] for event in postgres_store.all('SELECT id FROM events'))
    postgres_store.event(tenant,call_id,'after_transfer',{})
    new_event_max = max(event['id'] for event in postgres_store.all('SELECT id FROM events'))
    assert new_event_max > previous_event_max


def test_sqlite_transfer_refuses_nonempty_postgres_target(postgres_store,tmp_path):
    source = Store(tmp_path/'source.sqlite')
    source.create_tenant('Source Clinic','source-key-hash')
    postgres_store.create_tenant('Existing Clinic','existing-key-hash')

    with pytest.raises(TransferRefused,match='not empty'):
        transfer_sqlite_to_postgres(source.path,postgres_store.path)

    assert postgres_store.one('SELECT COUNT(*) AS count FROM tenants')['count'] == 1


def test_sqlite_transfer_cli_creates_backup_and_reports_rows(postgres_store,tmp_path):
    source = Store(tmp_path/'cli-source.sqlite')
    source.create_tenant('CLI Clinic','cli-key-hash')
    backup = tmp_path/'cli-source.backup.sqlite'
    env = os.environ.copy()
    env['VOCALIS_DATABASE_URL'] = postgres_store.path
    command_result = subprocess.run(
        [sys.executable,'-m','vocalis.migrate_sqlite','--source',source.path,'--backup',str(backup)],
        cwd=Path(__file__).parents[2],env=env,text=True,capture_output=True,check=False)

    assert command_result.returncode == 0, command_result.stderr
    result = json.loads(command_result.stdout)
    assert result['status'] == 'complete'
    assert result['rows_copied']['tenants'] == 1
    assert backup.is_file()


def test_postgres_call_retention_removes_events_before_expired_calls(postgres_store):
    store = postgres_store
    tenant = store.create_tenant('Retention Clinic','retention-key-hash')
    old_call = store.create_call(tenant,'browser',call_id='old-call')
    active_call = store.create_call(tenant,'browser',call_id='stale-active')
    recent_call = store.create_call(tenant,'browser',call_id='recent-call')
    old = (datetime.now(timezone.utc)-timedelta(days=45)).isoformat()
    with store.connect() as db:
        db.execute("UPDATE calls SET status='ended',started_at=?,ended_at=? WHERE tenant_id=? AND id=?",
                   (old,old,tenant,old_call))
        db.execute('UPDATE calls SET started_at=? WHERE tenant_id=? AND id=?',(old,tenant,active_call))
        db.execute("UPDATE calls SET status='ended',ended_at=? WHERE tenant_id=? AND id=?",
                   (datetime.now(timezone.utc).isoformat(),tenant,recent_call))
    cutoff = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    assert store.prune_call_data(cutoff,batch_size=1,apply=False) == 2
    assert store.prune_call_data(cutoff,batch_size=1,apply=True) == 2
    assert store.call(tenant,old_call) is None
    assert store.call(tenant,active_call) is None
    assert store.call(tenant,recent_call) is not None
