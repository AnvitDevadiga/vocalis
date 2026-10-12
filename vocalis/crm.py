"""Durable, leased CRM outbox processing."""
import asyncio
import os

from .engine import HubSpotCRM


async def sync_claim(store, claim, provider_factory=None):
    provider_factory = provider_factory or HubSpotCRM
    tenant_id, lead_id, token = claim['tenant_id'], claim['id'], claim['lease_token']
    try:
        external_id = await provider_factory(tenant_id).upsert(
            claim['name'],claim['phone'],claim['email'] or '')
    except Exception as exc:
        store.fail_lead_sync(tenant_id,lead_id,token,type(exc).__name__)
        return {'status':'pending','hubspot_id':None}
    committed = store.record_lead_sync(tenant_id,lead_id,token,claim['payload_hash'],external_id)
    if not committed:
        return {'status':'pending','hubspot_id':None}
    return {'status':'synced','hubspot_id':external_id}


async def process_pending(store, provider_factory=None, limit=25):
    provider_factory = provider_factory or HubSpotCRM
    # HubSpot credentials are currently configured for one tenant per worker process.
    tenant_id = os.getenv('HUBSPOT_TENANT_ID') if provider_factory is HubSpotCRM else None
    if provider_factory is HubSpotCRM and (not tenant_id or not os.getenv('HUBSPOT_ACCESS_TOKEN')):
        return []
    claims = store.claim_pending_lead_syncs(limit=limit,tenant_id=tenant_id)
    semaphore = asyncio.Semaphore(5)

    async def bounded(claim):
        async with semaphore:
            return await sync_claim(store,claim,provider_factory)

    return await asyncio.gather(*(bounded(claim) for claim in claims))
