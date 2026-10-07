"""Vocalis API and browser/telephony entry points."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlencode, parse_qsl
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from vocalis.engine import Engine, GoogleCalendar, HubSpotCRM, ProviderError
from vocalis.store import Store

DB_PATH = os.getenv('VOCALIS_DB_PATH', 'data/vocalis.db')
store = Store(DB_PATH)
engine = Engine(store)
app = FastAPI(title='Vocalis', version='0.3.0')
_buckets = defaultdict(list)
_webhook_locks = {}


def hash_key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def auth(x_api_key: str | None = Header(default=None)):
    if not x_api_key:
        raise HTTPException(401, 'API key required')
    tenant = store.tenant_by_key_hash(hash_key(x_api_key))
    if not tenant:
        raise HTTPException(401, 'Invalid API key')
    return tenant


def rate_limit(identity, maximum=30, window=60):
    now = time.monotonic()
    hits = _buckets[identity]
    hits[:] = [t for t in hits if now-t < window]
    if len(hits) >= maximum:
        raise HTTPException(429, 'Rate limit exceeded')
    hits.append(now)


def demo_tenant():
    tenant_id = os.getenv('VOCALIS_DEMO_TENANT_ID')
    if os.getenv('VOCALIS_PUBLIC_DEMO') != '1' or not tenant_id:
        return None
    return store.tenant(tenant_id)


class DocumentIn(BaseModel):
    title: str = Field(min_length=1,max_length=200)
    content: str = Field(min_length=1,max_length=100_000)


class BookingIn(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    phone: str = Field(min_length=3,max_length=40)
    service: str = Field(min_length=1,max_length=120)
    start_at: datetime
    duration_minutes: int = Field(default=30,ge=15,le=240)
    idempotency_key: str = Field(min_length=8,max_length=128)


class LeadIn(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    phone: str = Field(min_length=3,max_length=40)
    email: str = Field(default='',max_length=250)
    sync_hubspot: bool = False


@app.get('/')
def home():
    return FileResponse(Path(__file__).parent / 'static' / 'demo.html')


@app.get('/api/health')
def health():
    return {'status':'ok','llm_configured':bool(os.getenv('GROQ_API_KEY')),'public_demo':bool(demo_tenant())}


@app.get('/api/calls')
def calls(tenant=Depends(auth)):
    return {'calls':store.all('SELECT id,caller,channel,status,started_at,ended_at FROM calls WHERE tenant_id=? ORDER BY started_at DESC LIMIT 100',(tenant['id'],))}


@app.get('/api/calls/{call_id}')
def call_details(call_id: str, tenant=Depends(auth)):
    call = store.call(tenant['id'],call_id)
    if not call:
        raise HTTPException(404,'Call not found')
    return {'call':call,'events':store.events(tenant['id'],call_id)}


@app.get('/api/analytics')
def analytics(tenant=Depends(auth)):
    rows = store.all('SELECT kind,payload FROM events WHERE tenant_id=?',(tenant['id'],))
    durations = sorted(json.loads(r['payload']).get('response_ms') for r in rows if r['kind']=='turn_completed')
    def percentile(p):
        return durations[int((len(durations)-1)*p)] if durations else None
    return {'calls':len(store.all('SELECT id FROM calls WHERE tenant_id=?',(tenant['id'],))),
            'appointments':len(store.appointments(tenant['id'])),
            'latency_ms':{'samples':len(durations),'p50':percentile(.5),'p95':percentile(.95)}}


@app.get('/api/knowledge')
def documents(tenant=Depends(auth)):
    return {'documents':[{'id':r['id'],'title':r['title'],'created_at':r['created_at']} for r in store.documents(tenant['id'])]}


@app.post('/api/knowledge',status_code=201)
def add_document(data: DocumentIn, tenant=Depends(auth)):
    return {'id':store.add_document(tenant['id'],data.title,data.content)}


@app.get('/api/appointments')
def appointments(tenant=Depends(auth)):
    return {'appointments':store.appointments(tenant['id'])}


@app.post('/api/appointments')
async def book(data: BookingIn, tenant=Depends(auth)):
    if data.start_at.tzinfo is None:
        raise HTTPException(422,'start_at must include a timezone')
    start = data.start_at.astimezone(timezone.utc)
    end = start + timedelta(minutes=data.duration_minutes)
    if data.start_at <= datetime.now(data.start_at.tzinfo):
        raise HTTPException(422,'start_at must be in the future')
    prior = store.one('SELECT result FROM idempotency WHERE tenant_id=? AND key=?',(tenant['id'],data.idempotency_key))
    if prior:
        return json.loads(prior['result'])
    external_id = None
    if os.getenv('GOOGLE_CALENDAR_TENANT_ID') == tenant['id']:
        try:
            result = await GoogleCalendar(tenant['id']).book(data.name,data.service,start.isoformat(),end.isoformat(),data.idempotency_key)
        except Exception:
            raise HTTPException(503,'Calendar provider unavailable; booking not confirmed')
        if not result['ok']:
            raise HTTPException(409,result['error'])
        external_id = result['external_id']
        # External confirmation precedes local record. Local persistence failure is surfaced.
    result = store.book(tenant['id'],data.name,data.phone,data.service,start.isoformat(),end.isoformat(),data.idempotency_key)
    if not result['ok']:
        raise HTTPException(409,result['error'])
    if external_id:
        with store.connect() as db:
            db.execute('UPDATE appointments SET external_id=? WHERE tenant_id=? AND id=?',(external_id,tenant['id'],result['appointment_id']))
    return result


@app.delete('/api/appointments/{appointment_id}')
def cancel(appointment_id: str, idempotency_key: str, tenant=Depends(auth)):
    appointment = store.one('SELECT external_id FROM appointments WHERE tenant_id=? AND id=?',(tenant['id'],appointment_id))
    if appointment and appointment['external_id']:
        raise HTTPException(501,'External calendar cancellation is not implemented')
    result = store.cancel(tenant['id'],appointment_id,idempotency_key)
    if not result['ok']:
        raise HTTPException(404,'Appointment not found')
    return result


@app.get('/api/leads')
def leads(tenant=Depends(auth)):
    return {'leads':store.leads(tenant['id'])}


@app.post('/api/leads')
async def create_lead(data: LeadIn, tenant=Depends(auth)):
    external_id = None
    if data.sync_hubspot:
        try:
            external_id = await HubSpotCRM(tenant['id']).upsert(data.name,data.phone,data.email)
        except Exception:
            raise HTTPException(503,'CRM provider unavailable; lead not confirmed')
    lead_id = store.upsert_lead(tenant['id'],data.name,data.phone,data.email)
    if external_id:
        with store.connect() as db:
            db.execute('UPDATE leads SET external_id=? WHERE tenant_id=? AND id=?',(external_id,tenant['id'],lead_id))
    return {'id':lead_id,'hubspot_id':external_id}


def ws_tenant(ws):
    key = ws.headers.get('x-api-key')
    if key:
        return store.tenant_by_key_hash(hash_key(key))
    if ws.url.path == '/ws/demo':
        return demo_tenant()
    return None


@app.websocket('/ws')
@app.websocket('/ws/demo')
async def websocket_call(ws: WebSocket):
    tenant = ws_tenant(ws)
    if not tenant:
        await ws.close(code=1008)
        return
    if ws.url.path == '/ws/demo':
        try:
            rate_limit('demo:'+str(ws.client.host),maximum=8,window=60)
        except HTTPException:
            await ws.close(code=1013)
            return
    await ws.accept()
    call_id = store.create_call(tenant['id'],'browser')
    await ws.send_json({'type':'ready','call_id':call_id,'text':f"Hello, I'm the assistant for {tenant['name']}. How can I help?"})
    active = None
    generation = 0

    async def respond(text, turn_id):
        nonlocal generation
        try:
            result = await engine.turn(tenant['id'],call_id,text,is_current=lambda: turn_id == generation)
            if turn_id != generation:
                return
            await ws.send_json({'type':'response','text':result['text'],'metrics':result['metrics'],'evidence':result['evidence'],'escalation':result['escalation'],'turn_id':turn_id})
            try:
                started = time.perf_counter()
                audio = await asyncio.wait_for(engine.tts.synthesize(result['text']),timeout=20)
                if turn_id == generation:
                    store.event(tenant['id'],call_id,'tts_completed',{'duration_ms':round((time.perf_counter()-started)*1000,2)})
                    await ws.send_json({'type':'audio','audio':base64.b64encode(audio).decode(),'turn_id':turn_id})
            except Exception as exc:
                store.event(tenant['id'],call_id,'provider_error',{'provider':'tts','type':type(exc).__name__})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            store.event(tenant['id'],call_id,'turn_error',{'type':type(exc).__name__})
            if turn_id == generation:
                await ws.send_json({'type':'error','text':'This turn could not be completed.'})

    async def audio_turn(payload, turn_id):
        try:
            pcm = base64.b64decode(payload.get('audio',''),validate=True)
            started = time.perf_counter()
            text = await engine.stt.transcribe(pcm,int(payload.get('sample_rate',16000)))
            if turn_id != generation:
                return
            store.event(tenant['id'],call_id,'transcription_completed',{'length':len(text),'duration_ms':round((time.perf_counter()-started)*1000,2)})
            await ws.send_json({'type':'transcription','text':text})
            if text:
                await respond(text,turn_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            store.event(tenant['id'],call_id,'provider_error',{'provider':'stt','type':type(exc).__name__})
            if turn_id == generation:
                await ws.send_json({'type':'error','text':'Audio could not be transcribed.'})

    try:
        while True:
            payload = await ws.receive_json()
            kind = payload.get('type')
            if kind not in ('text','audio','interrupt'):
                continue
            if ws.url.path == '/ws/demo':
                try:
                    rate_limit('demo-turn:'+str(ws.client.host),maximum=20,window=60)
                except HTTPException:
                    await ws.send_json({'type':'error','text':'Demo rate limit reached.'})
                    continue
            generation += 1
            if active:
                active.cancel()
                try:
                    await active
                except asyncio.CancelledError:
                    pass
                store.event(tenant['id'],call_id,'interrupted',{'turn_id':generation-1})
            await ws.send_json({'type':'cancel_audio','turn_id':generation})
            if kind == 'interrupt':
                continue
            if kind == 'audio':
                active = asyncio.create_task(audio_turn(payload,generation))
                continue
            text = str(payload.get('text',''))[:2000].strip()
            if text:
                active = asyncio.create_task(respond(text,generation))
    except WebSocketDisconnect:
        pass
    finally:
        if active:
            active.cancel()
            try:
                await active
            except asyncio.CancelledError:
                pass
        store.end_call(tenant['id'],call_id)


def twilio_valid(request_url, params, signature):
    token = os.getenv('TWILIO_AUTH_TOKEN')
    if not token or not signature:
        return False
    content = request_url + ''.join(k+str(v) for k,v in sorted(params.items()))
    digest = base64.b64encode(hmac.new(token.encode(),content.encode(),hashlib.sha1).digest()).decode()
    return hmac.compare_digest(digest,signature)


def twiml(text, turn, transfer=None):
    if transfer:
        return f'<Response><Say>{escape(text)}</Say><Dial>{escape(transfer)}</Dial></Response>'
    action = '/twilio/gather?' + urlencode({'turn':turn})
    return f'<Response><Gather input="speech" action="{escape(action,quote=True)}" method="POST" speechTimeout="auto"><Say>{escape(text)}</Say></Gather><Redirect>/twilio/voice</Redirect></Response>'


async def signed_form(request):
    if request.headers.get('content-type','').split(';')[0].lower() != 'application/x-www-form-urlencoded':
        raise HTTPException(415,'Form data required')
    raw = await request.body()
    if len(raw) > 32768:
        raise HTTPException(413,'Webhook too large')
    try:
        pairs = parse_qsl(raw.decode('utf-8'),keep_blank_values=True,strict_parsing=True,max_num_fields=100)
    except (UnicodeDecodeError,ValueError):
        raise HTTPException(400,'Malformed webhook')
    if len({key for key,_ in pairs}) != len(pairs):
        raise HTTPException(400,'Duplicate webhook parameter')
    params = dict(pairs)
    base = os.getenv('VOCALIS_PUBLIC_BASE_URL','').rstrip('/')
    if not base:
        raise HTTPException(503,'Public URL is not configured')
    url = base + request.url.path + ('?' + request.url.query if request.url.query else '')
    if not twilio_valid(url,params,request.headers.get('x-twilio-signature')):
        raise HTTPException(403,'Invalid webhook signature')
    return params


def phone_tenant(number):
    for row in store.all('SELECT id,name,config FROM tenants'):
        if json.loads(row['config']).get('phone_number') == number:
            return row
    return None


@app.post('/twilio/voice')
async def twilio_voice(request: Request):
    params = await signed_form(request)
    tenant = phone_tenant(params.get('To'))
    if not tenant:
        raise HTTPException(404,'Unassigned number')
    sid = params.get('CallSid','')
    if not sid:
        raise HTTPException(422,'Missing CallSid')
    if not store.call(tenant['id'],sid):
        try:
            store.create_call(tenant['id'],'twilio',params.get('From'),sid)
        except sqlite3.IntegrityError:
            pass
    return Response(twiml(f"Hello, you have reached {tenant['name']}. How can I help?",1),media_type='application/xml')


@app.post('/twilio/gather')
async def twilio_gather(request: Request, turn: int = 1):
    params = await signed_form(request)
    tenant = phone_tenant(params.get('To'))
    if not tenant:
        raise HTTPException(404,'Unassigned number')
    sid = params.get('CallSid','')
    if not store.call(tenant['id'],sid):
        raise HTTPException(404,'Call not found')
    if turn < 1 or turn > 100:
        raise HTTPException(422,'Invalid turn')
    key = f'twilio:{sid}:{turn}'
    lock = _webhook_locks.setdefault((tenant['id'],key),asyncio.Lock())
    async with lock:
        prior = store.one('SELECT result FROM idempotency WHERE tenant_id=? AND key=?',(tenant['id'],key))
        if prior:
            return Response(prior['result'],media_type='application/xml')
        text = params.get('SpeechResult','').strip()
        if not text:
            body = twiml('I did not catch that. Please try again.',turn+1)
        else:
            result = await engine.turn(tenant['id'],sid,text)
            config = json.loads(tenant['config'])
            transfer = config.get('transfer_number') if result['escalation'] else None
            body = twiml(result['text'],turn+1,transfer)
        with store.connect() as db:
            db.execute('INSERT OR IGNORE INTO idempotency VALUES (?,?,?)',(tenant['id'],key,body))
        return Response(body,media_type='application/xml')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('app:app',host='0.0.0.0',port=8000)
