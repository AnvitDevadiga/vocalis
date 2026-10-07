"""Tenant-scoped SQLite persistence for the local pilot.

Every read and mutation takes a tenant id. SQLite transactions and uniqueness constraints
provide atomic local idempotency; use PostgreSQL before horizontal scaling.
"""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, api_key_hash TEXT NOT NULL UNIQUE, config TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), caller TEXT, channel TEXT NOT NULL, status TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL, ended_at TEXT);
CREATE INDEX IF NOT EXISTS calls_tenant ON calls(tenant_id, started_at);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, call_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_call ON events(tenant_id, call_id, id);
CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS documents_tenant ON documents(tenant_id);
CREATE TABLE IF NOT EXISTS appointments (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, external_id TEXT, name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS appointments_tenant ON appointments(tenant_id, start_at);
CREATE UNIQUE INDEX IF NOT EXISTS appointments_active_slot ON appointments(tenant_id, start_at) WHERE status='confirmed';
CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT, status TEXT NOT NULL, external_id TEXT, UNIQUE(tenant_id, phone));
CREATE TABLE IF NOT EXISTS idempotency (tenant_id TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(tenant_id,key));
CREATE TABLE IF NOT EXISTS webhooks (provider TEXT NOT NULL, event_id TEXT NOT NULL, tenant_id TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(provider,event_id));
"""


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript(SCHEMA)
            schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='appointments'").fetchone()[0]
            if 'UNIQUE(tenant_id, start_at)' in schema:
                # Migrate the first pilot schema, preserving existing appointment rows.
                db.execute('ALTER TABLE appointments RENAME TO appointments_old')
                db.execute('CREATE TABLE appointments (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, external_id TEXT, name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL)')
                db.execute('INSERT INTO appointments SELECT * FROM appointments_old')
                db.execute('DROP TABLE appointments_old')
                db.execute('CREATE INDEX appointments_tenant ON appointments(tenant_id, start_at)')
                db.execute("CREATE UNIQUE INDEX appointments_active_slot ON appointments(tenant_id, start_at) WHERE status='confirmed'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def one(self, sql, args=()):
        with self.connect() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def create_tenant(self, name, key_hash, config=None, tenant_id=None):
        tid = tenant_id or str(uuid.uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO tenants VALUES (?,?,?,?)', (tid, name, key_hash, json.dumps(config or {})))
        return tid

    def tenant_by_key_hash(self, key_hash):
        return self.one('SELECT id,name,config FROM tenants WHERE api_key_hash=?', (key_hash,))

    def tenant(self, tenant_id):
        return self.one('SELECT id,name,config FROM tenants WHERE id=?', (tenant_id,))

    def call(self, tenant_id, call_id):
        row = self.one('SELECT * FROM calls WHERE tenant_id=? AND id=?', (tenant_id, call_id))
        if row:
            row['state'] = json.loads(row['state'])
        return row

    def create_call(self, tenant_id, channel, caller=None, call_id=None):
        cid = call_id or str(uuid.uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?,NULL)', (cid, tenant_id, caller, channel, 'active', '{}', now()))
        self.event(tenant_id, cid, 'call_started', {'channel': channel})
        return cid

    def update_state(self, tenant_id, call_id, state):
        with self.connect() as db:
            cur = db.execute('UPDATE calls SET state=? WHERE tenant_id=? AND id=?', (json.dumps(state), tenant_id, call_id))
            if cur.rowcount != 1:
                raise KeyError('call not found')

    def end_call(self, tenant_id, call_id):
        with self.connect() as db:
            db.execute('UPDATE calls SET status=?, ended_at=? WHERE tenant_id=? AND id=?', ('ended', now(), tenant_id, call_id))
        self.event(tenant_id, call_id, 'call_ended', {})

    def event(self, tenant_id, call_id, kind, payload):
        with self.connect() as db:
            db.execute('INSERT INTO events(tenant_id,call_id,kind,payload,at) VALUES (?,?,?,?,?)', (tenant_id, call_id, kind, json.dumps(payload), now()))

    def events(self, tenant_id, call_id):
        rows = self.all('SELECT kind,payload,at FROM events WHERE tenant_id=? AND call_id=? ORDER BY id', (tenant_id, call_id))
        for row in rows:
            row['payload'] = json.loads(row['payload'])
        return rows

    def add_document(self, tenant_id, title, content):
        did = str(uuid.uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO documents VALUES (?,?,?,?,?)', (did, tenant_id, title, content, now()))
        return did

    def documents(self, tenant_id):
        return self.all('SELECT id,title,content,created_at FROM documents WHERE tenant_id=?', (tenant_id,))

    def book(self, tenant_id, name, phone, service, start_at, end_at, key):
        with self.lock, self.connect() as db:
            prior = db.execute('SELECT result FROM idempotency WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if prior:
                return json.loads(prior['result'])
            conflict = db.execute("SELECT id FROM appointments WHERE tenant_id=? AND status='confirmed' AND start_at<? AND end_at>?",
                                  (tenant_id,end_at,start_at)).fetchone()
            if conflict:
                return {'ok': False, 'error': 'slot_unavailable'}
            aid = str(uuid.uuid4())
            try:
                db.execute('INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?,?)', (aid,tenant_id,None,name,phone,service,start_at,end_at,'confirmed'))
            except sqlite3.IntegrityError:
                return {'ok': False, 'error': 'slot_unavailable'}
            result = {'ok': True, 'appointment_id': aid, 'start_at': start_at}
            db.execute('INSERT INTO idempotency VALUES (?,?,?)', (tenant_id,key,json.dumps(result)))
            return result

    def appointments(self, tenant_id):
        return self.all('SELECT * FROM appointments WHERE tenant_id=? ORDER BY start_at', (tenant_id,))

    def cancel(self, tenant_id, appointment_id, key):
        with self.lock, self.connect() as db:
            prior = db.execute('SELECT result FROM idempotency WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if prior:
                return json.loads(prior['result'])
            cur = db.execute('UPDATE appointments SET status=? WHERE tenant_id=? AND id=? AND status=?', ('cancelled',tenant_id,appointment_id,'confirmed'))
            result = {'ok': bool(cur.rowcount), 'appointment_id': appointment_id}
            db.execute('INSERT INTO idempotency VALUES (?,?,?)', (tenant_id,key,json.dumps(result)))
            return result

    def upsert_lead(self, tenant_id, name, phone, email=''):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT id FROM leads WHERE tenant_id=? AND phone=?', (tenant_id,phone)).fetchone()
            if row:
                db.execute('UPDATE leads SET name=?, email=? WHERE tenant_id=? AND id=?', (name,email,tenant_id,row['id']))
                return row['id']
            lid = str(uuid.uuid4())
            db.execute('INSERT INTO leads VALUES (?,?,?,?,?,?,?)', (lid,tenant_id,name,phone,email,'new',None))
            return lid

    def leads(self, tenant_id):
        return self.all('SELECT * FROM leads WHERE tenant_id=?', (tenant_id,))

    def accept_webhook(self, provider, event_id, tenant_id):
        with self.connect() as db:
            try:
                db.execute('INSERT INTO webhooks VALUES (?,?,?,?)', (provider,event_id,tenant_id,now()))
                return True
            except sqlite3.IntegrityError:
                return False
