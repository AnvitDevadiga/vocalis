import base64
import hashlib
import hmac
from fastapi.testclient import TestClient
import app as module
from vocalis.store import Store


def signature(url,params):
    data=url+''.join(k+str(v) for k,v in sorted(params.items()))
    return base64.b64encode(hmac.new(b'secret',data.encode(),hashlib.sha1).digest()).decode()


class FakeEngine:
    def __init__(self,store): self.store=store
    async def turn(self,tenant,call,text,commit=True):
        self.store.event(tenant,call,'fake_turn',{'text':text})
        return {'text':'Acknowledged','escalation':None,'_state':{'history':[{'role':'user','content':text}]},'_turn_event':{'response_ms':1}}


def test_retried_gather_is_idempotent(tmp_path,monkeypatch):
    store=Store(tmp_path/'db.sqlite')
    tenant=store.create_tenant('Business','hash',{'phone_number':'+15551234567'})
    monkeypatch.setattr(module,'store',store)
    monkeypatch.setattr(module,'engine',FakeEngine(store))
    monkeypatch.setenv('TWILIO_AUTH_TOKEN','secret')
    monkeypatch.setenv('VOCALIS_PUBLIC_BASE_URL','https://voice.example.com')
    client=TestClient(module.app)
    params={'CallSid':'CA123','To':'+15551234567','From':'+15557654321'}
    url='https://voice.example.com/twilio/voice'
    r=client.post('/twilio/voice',data=params,headers={'x-twilio-signature':signature(url,params)})
    assert r.status_code==200
    assert store.call(tenant,'CA123')
    params['SpeechResult']='Hello'
    url='https://voice.example.com/twilio/gather?turn=1'
    headers={'x-twilio-signature':signature(url,params)}
    first=client.post('/twilio/gather?turn=1',data=params,headers=headers)
    second=client.post('/twilio/gather?turn=1',data=params,headers=headers)
    assert first.status_code==200
    assert second.text==first.text
    assert sum(e['kind']=='fake_turn' for e in store.events(tenant,'CA123'))==1
    assert store.call(tenant,'CA123')['state']['history'][0]['content']=='Hello'
    params['SpeechResult']='Different delivery'
    conflicting={'x-twilio-signature':signature(url,params)}
    assert client.post('/twilio/gather?turn=1',data=params,headers=conflicting).status_code==409
    assert sum(e['kind']=='fake_turn' for e in store.events(tenant,'CA123'))==1
    assert client.post('/twilio/gather?turn=1',data=params).status_code==403
