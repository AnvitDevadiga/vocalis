import pytest
from fastapi.testclient import TestClient

import app as module
from vocalis.store import Store


def configure_client(monkeypatch, tmp_path):
    store = Store(tmp_path / 'leads.sqlite')
    key = 'lead-test-key'
    tenant = store.create_tenant('Clinic', module.hash_key(key))
    monkeypatch.setattr(module, 'store', store)
    return TestClient(module.app), store, tenant, {'x-api-key': key}


def test_crm_failure_keeps_lead_and_creates_retryable_job(monkeypatch, tmp_path):
    client, store, tenant, headers = configure_client(monkeypatch,tmp_path)

    async def unavailable(*args):
        raise RuntimeError('provider response included private contact details')

    monkeypatch.setattr(module.HubSpotCRM,'upsert',unavailable)
    response = client.post('/api/leads',headers=headers,json={
        'name':'Alex','phone':'555-0100','email':'alex@example.test','sync_hubspot':True,
    })

    assert response.status_code == 202
    payload = response.json()
    assert payload['crm_sync'] == 'pending'
    assert len(store.leads(tenant)) == 1
    job = store.lead_sync_job(tenant,payload['id'])
    assert job['status'] == 'pending'
    assert job['attempts'] == 1
    assert 'example.test' not in job['last_error']

    async def recovered(*args):
        return 'hs-contact-1'

    monkeypatch.setattr(module.HubSpotCRM,'upsert',recovered)
    retry = client.post(f"/api/leads/{payload['id']}/sync",headers=headers)
    assert retry.status_code == 200
    assert retry.json()['crm_sync'] == 'synced'
    assert retry.json()['hubspot_id'] == 'hs-contact-1'
    assert store.lead_sync_job(tenant,payload['id'])['attempts'] == 2
    assert store.leads(tenant)[0]['external_id'] == 'hs-contact-1'
    listed = client.get('/api/leads',headers=headers).json()['leads'][0]
    assert listed['crm_sync'] == 'synced'
    assert listed['crm_sync_attempts'] == 2

    # Updating an existing phone record creates a fresh pending delivery atomically.
    updated = client.post('/api/leads',headers=headers,json={
        'name':'Alex Updated','phone':'555-0100','email':'alex@example.test','sync_hubspot':True,
    })
    assert updated.status_code == 201
    assert updated.json()['id'] == payload['id']
    assert store.lead_sync_job(tenant,payload['id'])['status'] == 'synced'


def test_lead_sync_retry_is_tenant_scoped(monkeypatch, tmp_path):
    client, store, tenant, headers = configure_client(monkeypatch,tmp_path)
    lead_id = store.upsert_lead(tenant,'Alex','555-0100','alex@example.test')
    store.create_tenant('Other',module.hash_key('other-key'))
    response = client.post(f'/api/leads/{lead_id}/sync',headers={'x-api-key':'other-key'})
    assert response.status_code == 404
    assert store.lead_sync_job(tenant,lead_id) is None
