import hashlib
import hmac
import base64
from fastapi.testclient import TestClient
import app as module
from vocalis.store import Store


def test_api_key_scopes_reads(tmp_path, monkeypatch):
    store=Store(tmp_path/'db.sqlite')
    monkeypatch.setattr(module,'store',store)
    a=store.create_tenant('A',module.hash_key('key-a'))
    b=store.create_tenant('B',module.hash_key('key-b'))
    store.add_document(a,'Secret','private')
    client=TestClient(module.app)
    assert client.get('/api/knowledge').status_code == 401
    assert client.get('/api/knowledge',headers={'x-api-key':'key-b'}).json()['documents'] == []
    assert len(client.get('/api/knowledge',headers={'x-api-key':'key-a'}).json()['documents']) == 1
    call=store.create_call(a,'browser')
    assert client.get(f'/api/calls/{call}',headers={'x-api-key':'key-b'}).status_code == 404


def test_twilio_signature(monkeypatch):
    monkeypatch.setenv('TWILIO_AUTH_TOKEN','secret')
    url='https://voice.example.com/twilio/voice'
    params={'CallSid':'abc','To':'+15551234567'}
    content=url+''.join(k+v for k,v in sorted(params.items()))
    sig=base64.b64encode(hmac.new(b'secret',content.encode(),hashlib.sha1).digest()).decode()
    assert module.twilio_valid(url,params,sig)
    assert not module.twilio_valid(url,params,sig+'x')
