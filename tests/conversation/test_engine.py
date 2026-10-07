import asyncio
import pytest
from vocalis.engine import Engine
from vocalis.store import Store


class FakeLLM:
    async def complete(self, messages):
        return 'I do not know.', {'total_tokens':4}


@pytest.mark.asyncio
async def test_retrieval_isolation_and_escalation(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    a=store.create_tenant('A','a')
    b=store.create_tenant('B','b')
    doc=store.add_document(a,'Hours','We open Monday at nine.')
    engine=Engine(store,llm=FakeLLM())
    assert engine.retrieve(b,'Monday hours') == []
    assert engine.retrieve(a,'Monday hours')[0]['document_id'] == doc
    call=store.create_call(a,'browser')
    result=await engine.turn(a,call,'I need a human')
    assert result['escalation']['status']=='pending_followup'
    assert store.call(a,call)['state']['escalation']


class SlowLLM:
    async def complete(self, messages):
        await asyncio.sleep(.2)
        return 'stale', {}


@pytest.mark.asyncio
async def test_cancelled_turn_does_not_commit(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('A','a')
    call=store.create_call(tenant,'browser')
    engine=Engine(store,llm=SlowLLM())
    task=asyncio.create_task(engine.turn(tenant,call,'hello'))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.call(tenant,call)['state']=={}


class BookingLLM:
    def __init__(self,start): self.start=start
    async def complete(self,messages):
        import json
        return json.dumps({'reply':'Booked!', 'appointment':{'name':'Alex','phone':'5551234','service':'Checkup','start_at':self.start,'duration_minutes':30}}), {}


@pytest.mark.asyncio
async def test_voice_booking_requires_confirmation_and_provider_success(tmp_path):
    from datetime import datetime, timedelta, timezone
    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('A','a')
    call=store.create_call(tenant,'browser')
    start=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    engine=Engine(store,llm=BookingLLM(start))
    first=await engine.turn(tenant,call,'I want a checkup for Alex, 5551234 at '+start)
    assert 'Please confirm' in first['text']
    assert 'Booked!' not in first['text']
    assert store.appointments(tenant)==[]
    second=await engine.turn(tenant,call,'confirm')
    assert 'confirmed' in second['text']
    assert len(store.appointments(tenant))==1
    assert store.call(tenant,call)['state']['appointment']['ok']


@pytest.mark.asyncio
async def test_voice_does_not_confirm_taken_slot(tmp_path):
    from datetime import datetime, timedelta, timezone
    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('A','a')
    start=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    engine=Engine(store,llm=BookingLLM(start))
    first=store.create_call(tenant,'browser')
    await engine.turn(tenant,first,'Book this for Alex')
    await engine.turn(tenant,first,'confirm')
    second=store.create_call(tenant,'browser')
    await engine.turn(tenant,second,'Book this for Alex')
    result=await engine.turn(tenant,second,'confirm')
    assert 'could not confirm' in result['text']
    assert len(store.appointments(tenant))==1


class FalseBookingLLM:
    async def complete(self,messages):
        return '{"reply":"Your appointment is booked!"}', {}


@pytest.mark.asyncio
async def test_llm_cannot_claim_unverified_booking(tmp_path):
    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('A','a')
    call=store.create_call(tenant,'browser')
    result=await Engine(store,llm=FalseBookingLLM()).turn(tenant,call,'Can you book me?')
    assert 'cannot confirm' in result['text']
    assert store.appointments(tenant)==[]
