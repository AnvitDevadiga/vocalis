"""Vocalis API and browser/telephony entry points."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode, parse_qsl, urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from vocalis.booking import book_appointment
from vocalis.appointments import reschedule_appointment
from vocalis.crm import sync_claim
from vocalis.engine import Engine, GoogleCalendar, HubSpotCRM
from vocalis.store import Store, request_fingerprint

DB_PATH = os.getenv('VOCALIS_DATABASE_URL') or os.getenv('VOCALIS_DB_PATH', 'data/vocalis.db')
store = Store(DB_PATH)
engine = Engine(store)
logger = logging.getLogger('vocalis.maintenance')
_buckets = OrderedDict()
_bucket_lock = Lock()
_RATE_LIMIT_MAX_KEYS = 10_000
_MAX_AUDIO_BASE64_CHARS = 4_000_000
_DEMO_PRIVACY_NOTICE_VERSION = '2026-10-12-groq-audio-v1'
_HTTP_METRICS_LOCK = Lock()
_HTTP_METRICS_BUCKETS = (0.05,0.1,0.25,0.5,1.0,2.5,5.0,10.0)
_HTTP_METRICS = {}


def call_retention_days():
    try:
        days = int(os.getenv('VOCALIS_CALL_RETENTION_DAYS', '30'))
    except ValueError as exc:
        raise RuntimeError('VOCALIS_CALL_RETENTION_DAYS must be an integer') from exc
    if not 1 <= days <= 3650:
        raise RuntimeError('VOCALIS_CALL_RETENTION_DAYS must be between 1 and 3650')
    return days


async def prune_expired_calls():
    try:
        cutoff = (datetime.now(timezone.utc)-timedelta(days=call_retention_days())).isoformat()
        deleted = await asyncio.to_thread(store.prune_call_data, cutoff, apply=True)
        if deleted:
            logger.info('call_retention_completed deleted_calls=%d', deleted)
    except Exception as exc:
        logger.error('call_retention_failed error_type=%s', type(exc).__name__)


def ensure_public_demo_tenant():
    """Create the one public demo tenant deterministically, with no usable API key."""
    if os.getenv('VOCALIS_PUBLIC_DEMO') != '1' or not os.getenv('GROQ_API_KEY'):
        return
    tenant_id = os.getenv('VOCALIS_DEMO_TENANT_ID')
    if tenant_id:
        return
    name = os.getenv('VOCALIS_DEMO_TENANT_NAME')
    if not name:
        return
    tenant_id = str(uuid5(NAMESPACE_URL, 'https://vocalis.invalid/public-demo/'+name))
    if not store.tenant(tenant_id):
        discarded_api_key = secrets.token_urlsafe(32)
        try:
            store.create_tenant(name, hashlib.sha256(discarded_api_key.encode()).hexdigest(), tenant_id=tenant_id)
        except Exception:
            # Another app process may have initialized the same deterministic tenant.
            if not store.tenant(tenant_id):
                raise
    os.environ['VOCALIS_DEMO_TENANT_ID'] = tenant_id


async def retention_loop():
    while True:
        await asyncio.sleep(24*60*60)
        await prune_expired_calls()


@asynccontextmanager
async def lifespan(_app):
    await asyncio.to_thread(ensure_public_demo_tenant)
    await prune_expired_calls()
    task = asyncio.create_task(retention_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title='Vocalis', version='0.3.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv('VOCALIS_ALLOWED_ORIGINS','').split(',') if origin.strip()],
    allow_methods=['GET'], allow_headers=['accept'], allow_credentials=False)


def _observe_http_request(method, route, status, duration):
    key = (method,route,status)
    with _HTTP_METRICS_LOCK:
        metric = _HTTP_METRICS.setdefault(key, {'count':0,'sum':0.0,'buckets':[0]*len(_HTTP_METRICS_BUCKETS)})
        metric['count'] += 1
        metric['sum'] += duration
        for index,boundary in enumerate(_HTTP_METRICS_BUCKETS):
            if duration <= boundary:
                metric['buckets'][index] += 1


@app.middleware('http')
async def collect_http_metrics(request, call_next):
    started = time.perf_counter()
    status = 500
    route = request.scope.get('route')
    route_name = getattr(route,'path','unmatched')
    try:
        response = await call_next(request)
        status = response.status_code
        route = request.scope.get('route')
        route_name = getattr(route,'path',route_name)
        return response
    finally:
        _observe_http_request(request.method,route_name,status,time.perf_counter()-started)


def _prometheus_escape(value):
    return str(value).replace('\\','\\\\').replace('"','\\"').replace('\n','\\n')


@app.get('/metrics', response_class=PlainTextResponse, include_in_schema=False)
def prometheus_metrics(authorization: str | None = Header(default=None)):
    token = os.getenv('VOCALIS_METRICS_TOKEN')
    supplied = authorization[7:].strip() if authorization and authorization.startswith('Bearer ') else ''
    if not token or not supplied or not hmac.compare_digest(supplied,token):
        raise HTTPException(404,'Not found')
    lines = [
        '# HELP vocalis_http_requests_total Total HTTP requests by normalized route and response status.',
        '# TYPE vocalis_http_requests_total counter',
        '# HELP vocalis_http_request_duration_seconds HTTP request duration in seconds.',
        '# TYPE vocalis_http_request_duration_seconds histogram',
    ]
    with _HTTP_METRICS_LOCK:
        metrics = [(key,dict(value,buckets=list(value['buckets']))) for key,value in _HTTP_METRICS.items()]
    for (method,route,status),metric in sorted(metrics):
        labels = f'method="{_prometheus_escape(method)}",route="{_prometheus_escape(route)}",status="{status}"'
        lines.append(f'vocalis_http_requests_total{{{labels}}} {metric["count"]}')
        for boundary,count in zip(_HTTP_METRICS_BUCKETS,metric['buckets']):
            lines.append(f'vocalis_http_request_duration_seconds_bucket{{{labels},le="{boundary:g}"}} {count}')
        lines.append(f'vocalis_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {metric["count"]}')
        lines.append(f'vocalis_http_request_duration_seconds_sum{{{labels}}} {metric["sum"]:.9f}')
        lines.append(f'vocalis_http_request_duration_seconds_count{{{labels}}} {metric["count"]}')
    return '\n'.join(lines)+'\n'


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
    with _bucket_lock:
        hits = _buckets.pop(identity, [])
        hits = [t for t in hits if now-t < window]
        if len(hits) >= maximum:
            _buckets[identity] = hits
            while len(_buckets) > _RATE_LIMIT_MAX_KEYS:
                _buckets.popitem(last=False)
            raise HTTPException(429, 'Rate limit exceeded')
        hits.append(now)
        _buckets[identity] = hits
        while len(_buckets) > _RATE_LIMIT_MAX_KEYS:
            _buckets.popitem(last=False)


def audio_payload_error(payload):
    encoded = payload.get('audio')
    sample_rate = payload.get('sample_rate',16000)
    if not isinstance(encoded,str) or not encoded:
        return 'audio_required'
    if len(encoded) > _MAX_AUDIO_BASE64_CHARS:
        return 'audio_too_large'
    if isinstance(sample_rate,bool) or not isinstance(sample_rate,int) or not 8000 <= sample_rate <= 48000:
        return 'invalid_sample_rate'
    maximum_pcm_bytes = sample_rate * 2 * 30
    maximum_encoded_chars = 4 * ((maximum_pcm_bytes + 2) // 3)
    if len(encoded) > maximum_encoded_chars:
        return 'audio_exceeds_30_seconds'
    return None


def demo_tenant():
    tenant_id = os.getenv('VOCALIS_DEMO_TENANT_ID')
    if os.getenv('VOCALIS_PUBLIC_DEMO') != '1' or not os.getenv('GROQ_API_KEY') or not tenant_id:
        return None
    return store.tenant(tenant_id)


def websocket_origin_allowed(ws):
    """Allow same-origin browser sockets and explicitly configured site origins."""
    def authority(hostname, port, scheme):
        hostname = hostname.lower()
        if ':' in hostname:
            hostname = f'[{hostname}]'
        if port and not ((scheme == 'http' and port == 80) or (scheme == 'https' and port == 443)):
            return f'{hostname}:{port}'
        return hostname

    origin = ws.headers.get('origin')
    # Non-browser clients do not send Origin; the authenticated /ws endpoint
    # still requires its API key, and public /ws/demo remains rate limited.
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return False
        origin_host = authority(parsed.hostname, parsed.port, parsed.scheme)
        normalized_origin = f'{parsed.scheme}://{origin_host}'

        request_url = urlsplit(str(ws.url))
        request_scheme = {'ws': 'http', 'wss': 'https'}.get(request_url.scheme, request_url.scheme)
        request_host = authority(request_url.hostname or '', request_url.port, request_scheme)
        same_origin = normalized_origin == f'{request_scheme}://{request_host}'

        configured = set()
        for value in os.getenv('VOCALIS_ALLOWED_ORIGINS', '').split(','):
            value = value.strip()
            if not value:
                continue
            allowed = urlsplit(value)
            if allowed.scheme not in ('http', 'https') or not allowed.hostname or allowed.path or allowed.query or allowed.fragment or allowed.username or allowed.password:
                continue
            allowed_host = authority(allowed.hostname, allowed.port, allowed.scheme)
            configured.add(f'{allowed.scheme}://{allowed_host}')
        return same_origin or normalized_origin in configured
    except ValueError:
        return False


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


class AvailabilityIn(BaseModel):
    start_at: datetime
    duration_minutes: int = Field(default=30,ge=15,le=240)


class RescheduleIn(BaseModel):
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
    """Readiness check used by hosting platforms and the local container."""
    try:
        store.one('SELECT 1 AS ready')
    except Exception:
        return JSONResponse({'status':'unavailable','database':'unavailable'},status_code=503)
    return {'status':'ok','database':'ok','llm_configured':bool(os.getenv('GROQ_API_KEY')),'public_demo':bool(demo_tenant())}


@app.get('/api/health/live')
def liveness():
    """Process liveness only; intentionally does not depend on external services."""
    return {'status':'ok'}


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
            'appointments':len(store.all("SELECT id FROM appointments WHERE tenant_id=? AND status='confirmed'",(tenant['id'],))),
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


@app.post('/api/availability')
async def availability(data: AvailabilityIn, tenant=Depends(auth)):
    if data.start_at.tzinfo is None:
        raise HTTPException(422,'start_at must include a timezone')
    start = data.start_at.astimezone(timezone.utc)
    end = start + timedelta(minutes=data.duration_minutes)
    if start <= datetime.now(timezone.utc):
        raise HTTPException(422,'start_at must be in the future')
    if not store.slot_available(tenant['id'],start.isoformat(),end.isoformat()):
        return {'available':False,'start_at':start.isoformat(),'end_at':end.isoformat(),'source':'local'}
    if os.getenv('GOOGLE_CALENDAR_TENANT_ID') == tenant['id']:
        try:
            available = await GoogleCalendar(tenant['id']).available(start.isoformat(),end.isoformat())
        except Exception as exc:
            raise HTTPException(503,'Calendar availability could not be verified') from exc
        source = 'google_calendar'
    else:
        available,source = True,'local'
    return {'available':available,'start_at':start.isoformat(),'end_at':end.isoformat(),'source':source}


@app.post('/api/appointments')
async def book(data: BookingIn, tenant=Depends(auth)):
    if data.start_at.tzinfo is None:
        raise HTTPException(422,'start_at must include a timezone')
    start = data.start_at.astimezone(timezone.utc)
    end = start + timedelta(minutes=data.duration_minutes)
    if data.start_at <= datetime.now(data.start_at.tzinfo):
        raise HTTPException(422,'start_at must be in the future')
    try:
        result = await book_appointment(store,tenant['id'],data.name,data.phone,data.service,start.isoformat(),end.isoformat(),data.idempotency_key)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    if not result['ok']:
        status = 503 if result['error'] in ('calendar_outcome_unknown','booking_pending') else 409
        raise HTTPException(status,result['error'])
    return result


@app.delete('/api/appointments/{appointment_id}')
async def cancel(appointment_id: str, idempotency_key: str = Query(min_length=8,max_length=128), tenant=Depends(auth)):
    try:
        prior = store.prior_cancellation(tenant['id'],appointment_id,idempotency_key)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    if prior is not None:
        result = prior
    else:
        appointment = store.one('SELECT external_id FROM appointments WHERE tenant_id=? AND id=?',(tenant['id'],appointment_id))
        if not appointment:
            raise HTTPException(404,'Appointment not found')
        if appointment['external_id']:
            if os.getenv('GOOGLE_CALENDAR_TENANT_ID') != tenant['id']:
                raise HTTPException(503,'Google Calendar is not configured for this tenant')
            try:
                claim = store.reserve_appointment_operation(tenant['id'],appointment_id,'cancel',idempotency_key)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from exc
            if claim['status'] == 'complete':
                result = claim['result']
            elif claim['status'] == 'not_found':
                raise HTTPException(404,'Appointment not found or no longer active')
            elif claim['status'] != 'claimed':
                raise HTTPException(503,'Another appointment operation is pending; retry later')
            else:
                try:
                    await GoogleCalendar(tenant['id']).cancel(appointment['external_id'])
                    result = store.finish_appointment_operation(tenant['id'],idempotency_key,claim['token'])
                except Exception as exc:
                    raise HTTPException(503,'Calendar cancellation outcome is unknown; retry with the same idempotency key') from exc
        else:
            try:
                result = store.cancel(tenant['id'],appointment_id,idempotency_key)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from exc
    if not result['ok']:
        raise HTTPException(404,'Appointment not found')
    return result


@app.post('/api/appointments/{appointment_id}/reschedule')
async def reschedule(appointment_id: str, data: RescheduleIn, tenant=Depends(auth)):
    if data.start_at.tzinfo is None:
        raise HTTPException(422,'start_at must include a timezone')
    start = data.start_at.astimezone(timezone.utc)
    end = start + timedelta(minutes=data.duration_minutes)
    if start <= datetime.now(timezone.utc):
        raise HTTPException(422,'start_at must be in the future')
    try:
        result = await reschedule_appointment(store,tenant['id'],appointment_id,start.isoformat(),end.isoformat(),data.idempotency_key)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    if not result['ok']:
        if result['error'] == 'appointment_not_found':
            raise HTTPException(404,'Appointment not found or no longer active')
        status = 503 if result['error'] in ('calendar_outcome_unknown','appointment_operation_pending','calendar_not_configured') else 409
        raise HTTPException(status,result['error'])
    return result


@app.get('/api/leads')
def leads(tenant=Depends(auth)):
    return {'leads':store.leads(tenant['id'])}


async def sync_lead_to_hubspot(tenant_id, lead_id, name, phone, email):
    claim = store.claim_lead_sync(tenant_id,lead_id)
    if claim:
        return await sync_claim(store,claim,lambda _tenant_id: HubSpotCRM(_tenant_id))
    job = store.lead_sync_job(tenant_id,lead_id)
    lead = store.one('SELECT external_id FROM leads WHERE tenant_id=? AND id=?',(tenant_id,lead_id))
    if job and job['status'] == 'synced':
        return {'status':'synced','hubspot_id':lead['external_id'] if lead else None}
    return {'status':'pending','hubspot_id':lead['external_id'] if lead else None}


@app.post('/api/leads', status_code=201)
async def create_lead(data: LeadIn, response: Response, tenant=Depends(auth)):
    # The local database is the durable source of truth. CRM downtime must not lose a lead.
    lead_id = store.upsert_lead(tenant['id'],data.name,data.phone,data.email,queue_sync=data.sync_hubspot)
    result = {'id':lead_id,'hubspot_id':None,'crm_sync':'not_requested'}
    if data.sync_hubspot:
        sync = await sync_lead_to_hubspot(tenant['id'],lead_id,data.name,data.phone,data.email)
        result.update(hubspot_id=sync['hubspot_id'],crm_sync=sync['status'])
        if sync['status'] == 'pending':
            response.status_code = 202
    return result


@app.post('/api/leads/{lead_id}/sync')
async def retry_lead_sync(lead_id: str, response: Response, tenant=Depends(auth)):
    lead = next((row for row in store.leads(tenant['id']) if row['id'] == lead_id),None)
    if not lead:
        raise HTTPException(404,'Lead not found')
    job = store.lead_sync_job(tenant['id'],lead_id)
    if job and job['status'] == 'synced':
        return {'id':lead_id,'hubspot_id':lead['external_id'],'crm_sync':'synced'}
    if job and job['status'] == 'processing':
        response.status_code = 202
        return {'id':lead_id,'hubspot_id':lead['external_id'],'crm_sync':'pending'}
    if job:
        store.expedite_lead_sync_retry(tenant['id'],lead_id)
    else:
        store.queue_lead_sync(tenant['id'],lead_id)
    sync = await sync_lead_to_hubspot(tenant['id'],lead_id,lead['name'],lead['phone'],lead['email'] or '')
    if sync['status'] == 'pending':
        response.status_code = 202
    return {'id':lead_id,'hubspot_id':sync['hubspot_id'],'crm_sync':sync['status']}


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
    if not websocket_origin_allowed(ws):
        await ws.close(code=1008)
        return
    is_demo = ws.url.path == '/ws/demo'
    if is_demo:
        try:
            retention = call_retention_days()
        except RuntimeError:
            await ws.close(code=1011)
            return
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
    if is_demo:
        await ws.send_json({'type':'consent_required','notice_version':_DEMO_PRIVACY_NOTICE_VERSION,
                            'text':f'By continuing, you agree that text and microphone audio are sent to Vocalis for this conversation. Audio is processed by the configured transcription provider (Groq for the hosted demo) and is not saved as a recording. Primary call state is deleted after {retention} days. Private database backups, if created, may retain copies under separate retention rules. Configured AI and speech providers may process text to generate replies and speech.'})
        try:
            consent = await asyncio.wait_for(ws.receive_json(), timeout=60)
        except asyncio.TimeoutError:
            await ws.close(code=1008)
            return
        except WebSocketDisconnect:
            return
        if (not isinstance(consent, dict) or consent.get('type') != 'consent'
                or consent.get('accepted') is not True
                or consent.get('notice_version') != _DEMO_PRIVACY_NOTICE_VERSION):
            await ws.close(code=1008)
            return
    call_id = store.create_call(tenant['id'],'browser')
    if is_demo:
        store.event(tenant['id'],call_id,'consent_granted',{'notice_version':_DEMO_PRIVACY_NOTICE_VERSION})
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
            if not isinstance(payload,dict):
                await ws.send_json({'type':'error','text':'Message must be a JSON object.'})
                continue
            kind = payload.get('type')
            if kind not in ('text','audio','interrupt'):
                continue
            if ws.url.path == '/ws/demo':
                try:
                    rate_limit('demo-turn:'+str(ws.client.host),maximum=20,window=60)
                except HTTPException:
                    await ws.send_json({'type':'error','text':'Demo rate limit reached.'})
                    continue
            if kind == 'audio':
                audio_error = audio_payload_error(payload)
                if audio_error:
                    await ws.send_json({'type':'error','text':audio_error})
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
    text = params.get('SpeechResult','').strip()
    request_hash = request_fingerprint(['twilio-gather',sid,turn,params.get('To'),params.get('From'),text])
    claim = store.claim_webhook_turn(tenant['id'],key,request_hash)
    if claim['status'] == 'conflict':
        raise HTTPException(409,'Webhook turn conflicts with a prior delivery')
    if claim['status'] == 'pending':
        raise HTTPException(503,'Webhook turn is still processing',headers={'Retry-After':'2'})
    if claim['status'] == 'complete':
        return Response(claim['response'],media_type='application/xml')
    state = turn_event = None
    if not text:
        body = twiml('I did not catch that. Please try again.',turn+1)
    else:
        result = await engine.turn(tenant['id'],sid,text,commit=False)
        state,turn_event = result['_state'],result['_turn_event']
        config = json.loads(tenant['config'])
        transfer = config.get('transfer_number') if result['escalation'] else None
        body = twiml(result['text'],turn+1,transfer)
    body = store.finish_webhook_turn(tenant['id'],key,claim['token'],body,sid,state,turn_event)
    return Response(body,media_type='application/xml')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('app:app',host='0.0.0.0',port=8000)
