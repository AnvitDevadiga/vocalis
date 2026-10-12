import asyncio
import pytest
from concurrent.futures import ThreadPoolExecutor

from vocalis.crm import process_pending
from vocalis.store import Store


class FakeCRM:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id

    async def upsert(self, name, phone, email):
        return f'hs-{self.tenant_id}-{phone}'


@pytest.mark.asyncio
async def test_worker_claims_pending_jobs_and_persists_provider_result(tmp_path):
    store = Store(tmp_path/'crm.sqlite')
    tenant = store.create_tenant('Clinic','hash')
    lead = store.upsert_lead(tenant,'Alex','555-0100','alex@example.test',queue_sync=True)

    results = await process_pending(store,provider_factory=FakeCRM,limit=10)

    assert results == [{'status':'synced','hubspot_id':f'hs-{tenant}-555-0100'}]
    assert store.lead_sync_job(tenant,lead)['status'] == 'synced'
    assert store.leads(tenant)[0]['external_id'] == f'hs-{tenant}-555-0100'


def test_lead_sync_lease_blocks_duplicate_and_fences_stale_worker(tmp_path):
    store = Store(tmp_path/'crm.sqlite')
    tenant = store.create_tenant('Clinic','hash')
    lead = store.upsert_lead(tenant,'Alex','555-0100','',queue_sync=True)

    stale = store.claim_lead_sync(tenant,lead,lease_seconds=0)
    assert stale
    current = store.claim_lead_sync(tenant,lead,lease_seconds=60)
    assert current and current['lease_token'] != stale['lease_token']
    with pytest.raises(ValueError,match='claim was lost'):
        store.record_lead_sync(tenant,lead,stale['lease_token'],stale['payload_hash'],'stale-id')
    store.record_lead_sync(tenant,lead,current['lease_token'],current['payload_hash'],'current-id')
    assert store.leads(tenant)[0]['external_id'] == 'current-id'


def test_concurrent_workers_claim_a_lead_once_across_store_instances(tmp_path):
    path = tmp_path/'crm.sqlite'
    first, second = Store(path), Store(path)
    tenant = first.create_tenant('Clinic','hash')
    lead = first.upsert_lead(tenant,'Alex','555-0100','',queue_sync=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda store: store.claim_lead_sync(tenant,lead), (first,second)))

    assert sum(claim is not None for claim in claims) == 1


def test_failed_job_is_requeued_with_backoff_and_redacted_error(tmp_path):
    store = Store(tmp_path/'crm.sqlite')
    tenant = store.create_tenant('Clinic','hash')
    lead = store.upsert_lead(tenant,'Alex','555-0100','',queue_sync=True)
    claim = store.claim_lead_sync(tenant,lead)

    store.fail_lead_sync(tenant,lead,claim['lease_token'],'ProviderError')

    job = store.lead_sync_job(tenant,lead)
    assert job['status'] == 'pending'
    assert job['attempts'] == 1
    assert job['last_error'] == 'ProviderError'
    assert job['available_at'] > job['updated_at']
    assert store.claim_pending_lead_syncs() == []


@pytest.mark.asyncio
async def test_worker_does_not_mark_updated_lead_synced_with_stale_payload(tmp_path):
    store = Store(tmp_path/'crm.sqlite')
    tenant = store.create_tenant('Clinic','hash')
    lead = store.upsert_lead(tenant,'Alex','555-0100','old@example.test',queue_sync=True)
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowCRM(FakeCRM):
        async def upsert(self, name, phone, email):
            entered.set()
            await release.wait()
            return f'hs-{email}'

    task = asyncio.create_task(process_pending(store,provider_factory=SlowCRM))
    await entered.wait()
    store.upsert_lead(tenant,'Alex Updated','555-0100','new@example.test',queue_sync=True)
    release.set()

    assert await task == [{'status':'pending','hubspot_id':None}]
    assert store.lead_sync_job(tenant,lead)['status'] == 'pending'
    assert store.leads(tenant)[0]['external_id'] is None

    class CurrentCRM(FakeCRM):
        async def upsert(self, name, phone, email):
            return f'hs-{email}'

    assert await process_pending(store,provider_factory=CurrentCRM) == [
        {'status':'synced','hubspot_id':'hs-new@example.test'}]
    assert store.leads(tenant)[0]['external_id'] == 'hs-new@example.test'
