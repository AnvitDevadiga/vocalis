import hashlib
import hmac
import base64
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import URL
from starlette.websockets import WebSocketDisconnect
from types import SimpleNamespace
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


def test_process_rate_limit_bounds_distinct_identities(monkeypatch):
    monkeypatch.setattr(module,'_RATE_LIMIT_MAX_KEYS',2)
    module._buckets.clear()
    module.rate_limit('one',maximum=1)
    with pytest.raises(module.HTTPException) as limited:
        module.rate_limit('one',maximum=1)
    assert limited.value.status_code == 429
    module.rate_limit('two',maximum=1)
    module.rate_limit('three',maximum=1)
    assert len(module._buckets) == 2
    # The LRU eviction permits an old identity to begin a new local window.
    module.rate_limit('one',maximum=1)
    assert len(module._buckets) == 2


def test_health_reports_database_readiness(monkeypatch, tmp_path):
    store = Store(tmp_path/'health.sqlite')
    monkeypatch.setattr(module, 'store', store)
    client = TestClient(module.app)
    assert client.get('/api/health').status_code == 200
    assert client.get('/api/health').json()['database'] == 'ok'
    assert client.get('/api/health/live').status_code == 200

    class UnavailableStore:
        def one(self, *_args):
            raise RuntimeError('database unavailable')

    monkeypatch.setattr(module, 'store', UnavailableStore())
    response = client.get('/api/health')
    assert response.status_code == 503
    assert response.json() == {'status': 'unavailable', 'database': 'unavailable'}
    assert client.get('/api/health/live').status_code == 200


def test_application_startup_applies_call_retention(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    store = Store(tmp_path/'retention.sqlite')
    tenant = store.create_tenant('A','a')
    call_id = store.create_call(tenant,'browser')
    expired = (datetime.now(timezone.utc)-timedelta(days=45)).isoformat()
    with store.connect() as db:
        db.execute("UPDATE calls SET status='ended',started_at=?,ended_at=? WHERE id=?",(expired,expired,call_id))
    monkeypatch.setattr(module,'store',store)
    monkeypatch.setenv('VOCALIS_CALL_RETENTION_DAYS','30')

    with TestClient(module.app):
        assert store.call(tenant,call_id) is None


def test_public_demo_tenant_is_bootstrapped_only_when_enabled_and_configured(monkeypatch, tmp_path):
    store = Store(tmp_path/'demo-bootstrap.sqlite')
    monkeypatch.setattr(module,'store',store)
    monkeypatch.delenv('VOCALIS_DEMO_TENANT_ID',raising=False)
    monkeypatch.setenv('VOCALIS_DEMO_TENANT_NAME','Portfolio demo')
    monkeypatch.setenv('VOCALIS_PUBLIC_DEMO','1')
    monkeypatch.delenv('GROQ_API_KEY',raising=False)
    module.ensure_public_demo_tenant()
    assert store.all('SELECT id FROM tenants') == []

    monkeypatch.setenv('GROQ_API_KEY','test-secret')
    with TestClient(module.app):
        tenant_id=module.os.getenv('VOCALIS_DEMO_TENANT_ID')
        assert tenant_id
        assert store.tenant(tenant_id)['name']=='Portfolio demo'
        assert module.demo_tenant()['id']==tenant_id
    module.ensure_public_demo_tenant()
    assert store.all('SELECT id FROM tenants') == [{'id':tenant_id}]


def test_health_check_allows_only_configured_portfolio_origin(monkeypatch, tmp_path):
    monkeypatch.setattr(module,'store',Store(tmp_path/'cors.sqlite'))
    cors=next(item for item in module.app.user_middleware if item.cls.__name__=='CORSMiddleware')
    cors.kwargs['allow_origins']=['https://anvitdevadiga.github.io']
    module.app.middleware_stack=None
    client=TestClient(module.app)
    allowed=client.get('/api/health',headers={'origin':'https://anvitdevadiga.github.io'})
    denied=client.get('/api/health',headers={'origin':'https://attacker.example'})
    assert allowed.headers.get('access-control-allow-origin')=='https://anvitdevadiga.github.io'
    assert denied.headers.get('access-control-allow-origin') is None


def test_websocket_origin_policy(monkeypatch):
    ws = SimpleNamespace(url=URL('ws://api.example.test/ws/demo'), headers={'origin': 'http://api.example.test'})
    assert module.websocket_origin_allowed(ws)

    ws.headers['origin'] = 'https://evil.example'
    assert not module.websocket_origin_allowed(ws)

    monkeypatch.setenv('VOCALIS_ALLOWED_ORIGINS', 'https://anvitdevadiga.github.io, https://portfolio.example')
    ws.headers['origin'] = 'https://anvitdevadiga.github.io'
    assert module.websocket_origin_allowed(ws)
    ws.headers['origin'] = 'https://portfolio.example/path'
    assert not module.websocket_origin_allowed(ws)
    ws.headers['origin'] = 'null'
    assert not module.websocket_origin_allowed(ws)


def test_websocket_rejects_foreign_origin_before_tenant_lookup(monkeypatch):
    monkeypatch.setattr(module, 'ws_tenant', lambda _ws: pytest.fail('tenant lookup must not run'))
    client = TestClient(module.app)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect('/ws/demo', headers={'origin': 'https://evil.example'}):
            pass
    assert closed.value.code == 1008


def test_prometheus_metrics_require_token_and_hide_call_ids(monkeypatch, tmp_path):
    store = Store(tmp_path/'metrics.sqlite')
    api_key = 'tenant-key'
    tenant = store.create_tenant('Metrics test',module.hash_key(api_key))
    call_id = store.create_call(tenant,'browser')
    monkeypatch.setattr(module,'store',store)
    monkeypatch.setenv('VOCALIS_METRICS_TOKEN','scrape-secret')
    client = TestClient(module.app)

    assert client.get('/metrics').status_code == 404
    assert client.get('/metrics',headers={'authorization':'Bearer wrong'}).status_code == 404
    assert client.get(f'/api/calls/{call_id}',headers={'x-api-key':api_key}).status_code == 200
    secret_path = '/unknown/private-sentinel-12345'
    assert client.get(secret_path).status_code == 404
    response = client.get('/metrics',headers={'authorization':'Bearer scrape-secret'})
    assert response.status_code == 200
    assert 'vocalis_http_requests_total' in response.text
    assert 'route="/api/calls/{call_id}"' in response.text
    assert call_id not in response.text
    assert secret_path not in response.text
