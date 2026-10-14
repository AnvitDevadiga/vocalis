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
    booked=await engine.turn(tenant,first,'Book this for Alex')
    assert 'Please confirm' in booked['text']
    await engine.turn(tenant,first,'confirm')
    second=store.create_call(tenant,'browser')
    result=await engine.turn(tenant,second,'Book this for Alex')
    assert 'not available' in result['text']
    assert 'Please confirm' not in result['text']
    assert len(store.appointments(tenant))==1


@pytest.mark.asyncio
async def test_public_demo_does_not_offer_booking_without_calendar(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('Vocalis Portfolio Demo','key')
    call=store.create_call(tenant,'browser')
    start=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    monkeypatch.setenv('VOCALIS_PUBLIC_DEMO','1')
    monkeypatch.setenv('VOCALIS_DEMO_TENANT_ID',tenant)
    monkeypatch.delenv('GOOGLE_CALENDAR_TENANT_ID',raising=False)
    result=await Engine(store,llm=BookingLLM(start)).turn(tenant,call,'Please book this meeting')
    assert 'cannot verify calendar availability' in result['text'].lower()
    assert 'appointment_draft' not in store.call(tenant,call)['state']


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


@pytest.mark.asyncio
async def test_voice_never_confirms_uncertain_calendar_result(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import vocalis.booking as booking

    store = Store(tmp_path/'db.sqlite')
    tenant = store.create_tenant('A','a')
    call = store.create_call(tenant,'browser')
    start = (datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    engine = Engine(store,llm=BookingLLM(start))
    await engine.turn(tenant,call,'Book a checkup')

    async def uncertain(*args):
        return {'ok':False,'error':'calendar_outcome_unknown'}

    monkeypatch.setattr(booking,'book_appointment',uncertain)
    response = await engine.turn(tenant,call,'confirm')
    assert 'cannot confirm' in response['text'].lower()
    assert response['escalation']['reason'] == 'calendar_outcome_unknown'
    assert store.call(tenant,call)['state']['appointment_draft']
    assert store.appointments(tenant) == []


@pytest.mark.asyncio
async def test_groq_stt_sends_bounded_wav_without_logging_key(monkeypatch):
    import io
    import wave
    import httpx
    from vocalis.engine import GroqWhisperSTT

    captured={}
    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {'text':' hello from groq '}
    class FakeClient:
        def __init__(self,**kwargs): captured['timeout']=kwargs['timeout']
        async def __aenter__(self): return self
        async def __aexit__(self,*_args): pass
        async def post(self,url,**kwargs):
            captured.update(url=url,**kwargs)
            return FakeResponse()

    monkeypatch.setattr(httpx,'AsyncClient',FakeClient)
    monkeypatch.setenv('GROQ_API_KEY','private-test-key')
    result=await GroqWhisperSTT().transcribe(b'\x01\x00'*16000,16000)
    assert result=='hello from groq'
    assert captured['url'].endswith('/audio/transcriptions')
    assert captured['headers']=={'Authorization':'Bearer private-test-key'}
    assert captured['data']['model']=='whisper-large-v3-turbo'
    filename,contents,mime=captured['files']['file']
    assert (filename,mime)==('speech.wav','audio/wav')
    with wave.open(io.BytesIO(contents),'rb') as wav:
        assert (wav.getnchannels(),wav.getframerate(),wav.getnframes())==(1,16000,16000)


@pytest.mark.asyncio
async def test_groq_stt_rejects_oversize_audio_before_network(monkeypatch):
    from vocalis.engine import GroqWhisperSTT
    monkeypatch.setenv('GROQ_API_KEY','test-key')
    with pytest.raises(ValueError,match='30 seconds'):
        await GroqWhisperSTT().transcribe(b'\0\0'*(16000*31),16000)


def test_engine_selects_configured_stt_provider(tmp_path,monkeypatch):
    from vocalis.engine import GroqWhisperSTT, WhisperSTT
    store=Store(tmp_path/'db.sqlite')
    monkeypatch.setenv('VOCALIS_STT_PROVIDER','groq')
    assert isinstance(Engine(store).stt,GroqWhisperSTT)
    monkeypatch.setenv('VOCALIS_STT_PROVIDER','local')
    assert isinstance(Engine(store).stt,WhisperSTT)
