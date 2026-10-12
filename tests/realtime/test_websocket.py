import asyncio
import time
from fastapi.testclient import TestClient
import app as module
from vocalis.store import Store


class SlowLLM:
    async def complete(self,messages):
        await asyncio.sleep(.15)
        return 'old response',{}


class NoTTS:
    async def synthesize(self,text):
        return b''


def test_interruption_cancels_stale_response(tmp_path,monkeypatch):
    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('Demo','hash')
    monkeypatch.setattr(module,'store',store)
    monkeypatch.setattr(module,'engine',module.Engine(store,llm=SlowLLM(),tts=NoTTS()))
    monkeypatch.setenv('VOCALIS_PUBLIC_DEMO','1')
    monkeypatch.setenv('GROQ_API_KEY','test-key')
    monkeypatch.setenv('VOCALIS_DEMO_TENANT_ID',tenant)
    with TestClient(module.app).websocket_connect('/ws/demo') as ws:
        notice=ws.receive_json()
        assert notice['type']=='consent_required'
        assert 'Groq for the hosted demo' in notice['text']
        ws.send_json({'type':'consent','accepted':True,'notice_version':notice['notice_version']})
        ready=ws.receive_json()
        assert ready['type']=='ready'
        assert store.events(tenant,ready['call_id'])[1]['kind']=='consent_granted'
        ws.send_json({'type':'text','text':'first'})
        assert ws.receive_json()['type']=='cancel_audio'
        ws.send_json({'type':'interrupt'})
        assert ws.receive_json()['type']=='cancel_audio'
        time.sleep(.25)
        call=store.call(tenant,ready['call_id'])
        assert call['state']=={}


class SlowSTT:
    async def transcribe(self,pcm,sample_rate):
        await asyncio.sleep(.15)
        return 'stale transcription'


def test_interruption_cancels_inflight_transcription(tmp_path,monkeypatch):
    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('Demo','hash')
    monkeypatch.setattr(module,'store',store)
    monkeypatch.setattr(module,'engine',module.Engine(store,llm=SlowLLM(),stt=SlowSTT(),tts=NoTTS()))
    monkeypatch.setenv('VOCALIS_PUBLIC_DEMO','1')
    monkeypatch.setenv('GROQ_API_KEY','test-key')
    monkeypatch.setenv('VOCALIS_DEMO_TENANT_ID',tenant)
    with TestClient(module.app).websocket_connect('/ws/demo') as ws:
        notice=ws.receive_json()
        assert notice['type']=='consent_required'
        ws.send_json({'type':'consent','accepted':True,'notice_version':notice['notice_version']})
        ready=ws.receive_json()
        ws.send_json({'type':'audio','audio':'AAAAAA==','sample_rate':16000})
        assert ws.receive_json()['type']=='cancel_audio'
        ws.send_json({'type':'interrupt'})
        assert ws.receive_json()['type']=='cancel_audio'
        time.sleep(.2)
        assert store.call(tenant,ready['call_id'])['state']=={}
        assert not any(e['kind']=='transcription_completed' for e in store.events(tenant,ready['call_id']))


def test_demo_requires_privacy_consent_before_creating_call(tmp_path,monkeypatch):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('Demo','hash')
    monkeypatch.setattr(module,'store',store)
    monkeypatch.setenv('VOCALIS_PUBLIC_DEMO','1')
    monkeypatch.setenv('GROQ_API_KEY','test-key')
    monkeypatch.setenv('VOCALIS_DEMO_TENANT_ID',tenant)
    client=TestClient(module.app)
    with client.websocket_connect('/ws/demo') as ws:
        assert ws.receive_json()['type']=='consent_required'
        ws.send_json({'type':'text','text':'hello'})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code==1008
    assert store.all('SELECT id FROM calls WHERE tenant_id=?',(tenant,))==[]
