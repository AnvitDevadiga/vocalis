"""Tenant-scoped SQLite persistence for the local pilot.

Every read and mutation takes a tenant id. SQLite transactions and uniqueness constraints
provide atomic local idempotency; use PostgreSQL before horizontal scaling.
"""
import json
import hashlib
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import create_engine

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, api_key_hash TEXT NOT NULL UNIQUE, config TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), caller TEXT, channel TEXT NOT NULL, status TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}', started_at TEXT NOT NULL, ended_at TEXT);
CREATE INDEX IF NOT EXISTS calls_tenant ON calls(tenant_id, started_at);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, call_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_call ON events(tenant_id, call_id, id);
CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS documents_tenant ON documents(tenant_id);
CREATE TABLE IF NOT EXISTS appointments (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, external_id TEXT, name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL, request_key TEXT, request_hash TEXT, reservation_at TEXT);
CREATE INDEX IF NOT EXISTS appointments_tenant ON appointments(tenant_id, start_at);
CREATE UNIQUE INDEX IF NOT EXISTS appointments_active_slot ON appointments(tenant_id, start_at) WHERE status='confirmed';
CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL, email TEXT, status TEXT NOT NULL, external_id TEXT, UNIQUE(tenant_id, phone));
CREATE TABLE IF NOT EXISTS lead_sync_jobs (tenant_id TEXT NOT NULL, lead_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT NOT NULL, lease_token TEXT, lease_until TEXT, available_at TEXT NOT NULL DEFAULT '', payload_hash TEXT NOT NULL DEFAULT '', PRIMARY KEY(tenant_id,lead_id), FOREIGN KEY(lead_id) REFERENCES leads(id));
CREATE TABLE IF NOT EXISTS idempotency (tenant_id TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL, request_hash TEXT, PRIMARY KEY(tenant_id,key));
CREATE TABLE IF NOT EXISTS webhooks (provider TEXT NOT NULL, event_id TEXT NOT NULL, tenant_id TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(provider,event_id));
CREATE TABLE IF NOT EXISTS webhook_turns (tenant_id TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL, status TEXT NOT NULL, token TEXT NOT NULL, lease_until TEXT NOT NULL, response TEXT, PRIMARY KEY(tenant_id,key));
CREATE TABLE IF NOT EXISTS appointment_operations (tenant_id TEXT NOT NULL, key TEXT NOT NULL, appointment_id TEXT NOT NULL, kind TEXT NOT NULL, request_hash TEXT NOT NULL, status TEXT NOT NULL, token TEXT NOT NULL, lease_until TEXT NOT NULL, new_start TEXT, new_end TEXT, result TEXT, PRIMARY KEY(tenant_id,key));
CREATE UNIQUE INDEX IF NOT EXISTS appointment_operations_active ON appointment_operations(tenant_id,appointment_id) WHERE status='pending';
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def request_fingerprint(parts):
    return hashlib.sha256(json.dumps(parts, separators=(',', ':')).encode()).hexdigest()


def _as_utc(value):
    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_slot_conflict(db, tenant_id, start_at, end_at, exclude_id=None):
    start, end = _as_utc(start_at), _as_utc(end_at)
    rows = db.execute("SELECT id,start_at,end_at FROM appointments WHERE tenant_id=? AND status IN ('pending','confirmed')", (tenant_id,)).fetchall()
    for row in rows:
        if row['id'] == exclude_id:
            continue
        if start < _as_utc(row['end_at']) and end > _as_utc(row['start_at']):
            return True
    operations = db.execute("SELECT appointment_id,new_start,new_end FROM appointment_operations WHERE tenant_id=? AND status='pending' AND new_start IS NOT NULL", (tenant_id,)).fetchall()
    for operation in operations:
        if operation['appointment_id'] == exclude_id:
            continue
        if start < _as_utc(operation['new_end']) and end > _as_utc(operation['new_start']):
            return True
    return False


class PostgreSQLResult:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def _map(self, row):
        if row is None or isinstance(row,dict):
            return row
        return {column.name:value for column,value in zip(self.cursor.description,row)}

    def fetchone(self):
        return self._map(self.cursor.fetchone())

    def fetchall(self):
        return [self._map(row) for row in self.cursor.fetchall()]


class PostgreSQLConnection:
    """Small DB-API adapter for the Store's positional SQL and transaction locks."""
    def __init__(self, connection):
        self.connection = connection
        self._tenant_lock_pending = False

    def execute(self, sql, args=()):
        statement = sql.strip()
        if statement.upper() == 'BEGIN IMMEDIATE':
            result = self.connection.execute('BEGIN')
            self._tenant_lock_pending = True
            return PostgreSQLResult(result)

        if self._tenant_lock_pending:
            match = re.search(r'\btenant_id\s*=\s*\?',sql,re.IGNORECASE)
            if match:
                tenant_arg_index = sql[:match.start()].count('?')
                tenant_id = args[tenant_arg_index]
                self.connection.execute(
                    'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',(str(tenant_id),))
                self._tenant_lock_pending = False

        return PostgreSQLResult(self.connection.execute(sql.replace('?','%s'),args))


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.is_postgres = self.path.startswith(('postgres://','postgresql://','postgresql+psycopg://'))
        if self.is_postgres:
            url = self.path.replace('postgres://','postgresql+psycopg://',1)
            if url.startswith('postgresql://'):
                url = url.replace('postgresql://','postgresql+psycopg://',1)
            self.path = url
            self.engine = create_engine(url,pool_pre_ping=True)
            try:
                with self.connect() as db:
                    revision = db.execute('SELECT version_num FROM alembic_version').fetchone()
                    if not revision or revision['version_num'] != '0001_postgres':
                        raise RuntimeError('PostgreSQL schema is not current; run `alembic upgrade head`')
            except Exception as exc:
                self.engine.dispose()
                if isinstance(exc,RuntimeError):
                    raise
                raise RuntimeError('Cannot open PostgreSQL store; check database connectivity and run `alembic upgrade head`') from exc
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(idempotency)')}
            if 'request_hash' not in columns:
                db.execute('ALTER TABLE idempotency ADD COLUMN request_hash TEXT')
            appointment_columns = {row['name'] for row in db.execute('PRAGMA table_info(appointments)')}
            if 'request_key' not in appointment_columns:
                db.execute('ALTER TABLE appointments ADD COLUMN request_key TEXT')
            if 'request_hash' not in appointment_columns:
                db.execute('ALTER TABLE appointments ADD COLUMN request_hash TEXT')
            if 'reservation_at' not in appointment_columns:
                db.execute('ALTER TABLE appointments ADD COLUMN reservation_at TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS appointments_request_key ON appointments(tenant_id, request_key) WHERE request_key IS NOT NULL')
            lead_job_columns = {row['name'] for row in db.execute('PRAGMA table_info(lead_sync_jobs)')}
            for column, definition in (('lease_token','TEXT'),('lease_until','TEXT'),("available_at", "TEXT NOT NULL DEFAULT ''"),('payload_hash', "TEXT NOT NULL DEFAULT ''")):
                if column not in lead_job_columns:
                    db.execute(f'ALTER TABLE lead_sync_jobs ADD COLUMN {column} {definition}')
            db.execute('CREATE INDEX IF NOT EXISTS lead_sync_jobs_pending ON lead_sync_jobs(status,available_at,updated_at)')
            schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='appointments'").fetchone()[0]
            if 'UNIQUE(tenant_id, start_at)' in schema:
                # Migrate the first pilot schema, preserving existing appointment rows.
                db.execute('ALTER TABLE appointments RENAME TO appointments_old')
                db.execute('CREATE TABLE appointments (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, external_id TEXT, name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL, request_key TEXT, request_hash TEXT, reservation_at TEXT)')
                db.execute('INSERT INTO appointments SELECT * FROM appointments_old')
                db.execute('DROP TABLE appointments_old')
                db.execute('CREATE INDEX appointments_tenant ON appointments(tenant_id, start_at)')
                db.execute("CREATE UNIQUE INDEX appointments_active_slot ON appointments(tenant_id, start_at) WHERE status='confirmed'")
                db.execute('CREATE UNIQUE INDEX appointments_request_key ON appointments(tenant_id, request_key) WHERE request_key IS NOT NULL')

    @contextmanager
    def connect(self):
        if self.is_postgres:
            raw = self.engine.raw_connection()
            connection = PostgreSQLConnection(raw.driver_connection)
            try:
                yield connection
                raw.commit()
            except BaseException:
                raw.rollback()
                raise
            finally:
                raw.close()
            return
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

    def prune_call_data(self, cutoff, batch_size=500, apply=False):
        """Count or remove old call records, their transcript state, and events."""
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 5000:
            raise ValueError('batch_size must be between 1 and 5000')
        total = 0
        for tenant in self.all('SELECT id FROM tenants'):
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                criteria = "tenant_id=? AND ((status='ended' AND ended_at IS NOT NULL AND ended_at < ?) OR (status='active' AND started_at < ?))"
                if not apply:
                    total += db.execute(f'SELECT count(*) AS count FROM calls WHERE {criteria}',
                                         (tenant['id'],cutoff,cutoff)).fetchone()['count']
                    continue
                while True:
                    rows = db.execute(f'SELECT id FROM calls WHERE {criteria} ORDER BY started_at LIMIT ?',
                                      (tenant['id'],cutoff,cutoff,batch_size)).fetchall()
                    call_ids = [row['id'] for row in rows]
                    total += len(call_ids)
                    if not call_ids:
                        break
                    placeholders = ','.join('?' for _ in call_ids)
                    args = [tenant['id'],*call_ids]
                    db.execute(f'DELETE FROM events WHERE tenant_id=? AND call_id IN ({placeholders})',args)
                    db.execute(f'DELETE FROM calls WHERE tenant_id=? AND id IN ({placeholders})',args)
                    if len(call_ids) < batch_size:
                        break
        return total

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

    def prior_booking(self, tenant_id, name, phone, service, start_at, end_at, key):
        row = self.one('SELECT result,request_hash FROM idempotency WHERE tenant_id=? AND key=?', (tenant_id,key))
        if not row:
            return None
        expected = request_fingerprint(['book', name, phone, service, start_at, end_at])
        if row['request_hash'] != expected:
            raise ValueError('idempotency key reused for a different request')
        return json.loads(row['result'])

    def prior_cancellation(self, tenant_id, appointment_id, key):
        row = self.one('SELECT result,request_hash FROM idempotency WHERE tenant_id=? AND key=?', (tenant_id,key))
        if not row:
            return None
        expected = request_fingerprint(['cancel', appointment_id])
        if row['request_hash'] != expected:
            raise ValueError('idempotency key reused for a different request')
        return json.loads(row['result'])

    def reserve_appointment_operation(self, tenant_id, appointment_id, kind, key, new_start=None, new_end=None):
        request_hash = (request_fingerprint(['cancel',appointment_id]) if kind == 'cancel'
                        else request_fingerprint([kind,appointment_id,new_start,new_end]))
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old_result = db.execute('SELECT result,request_hash FROM idempotency WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if old_result:
                if old_result['request_hash'] != request_hash:
                    raise ValueError('idempotency key reused for a different request')
                return {'status':'complete','result':json.loads(old_result['result'])}
            row = db.execute('SELECT * FROM appointment_operations WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if row:
                if row['request_hash'] != request_hash:
                    raise ValueError('idempotency key reused for a different request')
                if row['status'] in ('complete','rejected'):
                    return {'status':'complete','result':json.loads(row['result'])}
                # Replays of the same key are safe: provider calls are idempotent,
                # and replacing the token prevents an older worker from committing.
                current = datetime.now(timezone.utc)
                token = str(uuid.uuid4())
                lease_until = (current+timedelta(minutes=2)).isoformat()
                db.execute('UPDATE appointment_operations SET token=?,lease_until=? WHERE tenant_id=? AND key=?',
                           (token,lease_until,tenant_id,key))
                return {'status':'claimed','token':token}
            appointment = db.execute('SELECT status FROM appointments WHERE tenant_id=? AND id=?', (tenant_id,appointment_id)).fetchone()
            if not appointment or appointment['status'] != 'confirmed':
                return {'status':'not_found'}
            active = db.execute("SELECT key FROM appointment_operations WHERE tenant_id=? AND appointment_id=? AND status='pending'", (tenant_id,appointment_id)).fetchone()
            if active:
                return {'status':'pending'}
            if kind == 'reschedule' and _has_slot_conflict(db,tenant_id,new_start,new_end,appointment_id):
                return {'status':'conflict'}
            token = str(uuid.uuid4())
            lease_until = (datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat()
            db.execute('INSERT INTO appointment_operations VALUES (?,?,?,?,?,?,?,?,?,?,NULL)',
                       (tenant_id,key,appointment_id,kind,request_hash,'pending',token,lease_until,new_start,new_end))
            return {'status':'claimed','token':token}

    def finish_appointment_operation(self, tenant_id, key, token, external_id=None):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            operation = db.execute('SELECT * FROM appointment_operations WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if not operation or operation['status'] != 'pending' or operation['token'] != token:
                raise ValueError('appointment operation claim was lost')
            if operation['kind'] == 'cancel':
                cur = db.execute("UPDATE appointments SET status='cancelled' WHERE tenant_id=? AND id=? AND status='confirmed'",
                                 (tenant_id,operation['appointment_id']))
                if cur.rowcount != 1:
                    raise ValueError('appointment is no longer active')
                result = {'ok':True,'appointment_id':operation['appointment_id']}
            else:
                cur = db.execute("UPDATE appointments SET start_at=?,end_at=?,external_id=COALESCE(?,external_id) WHERE tenant_id=? AND id=? AND status='confirmed'",
                                 (operation['new_start'],operation['new_end'],external_id,tenant_id,operation['appointment_id']))
                if cur.rowcount != 1:
                    raise ValueError('appointment is no longer active')
                result = {'ok':True,'appointment_id':operation['appointment_id'],'start_at':operation['new_start'],'end_at':operation['new_end']}
            db.execute("UPDATE appointment_operations SET status='complete',result=? WHERE tenant_id=? AND key=?",
                       (json.dumps(result),tenant_id,key))
            db.execute('INSERT INTO idempotency(tenant_id,key,result,request_hash) VALUES (?,?,?,?)',
                       (tenant_id,key,json.dumps(result),operation['request_hash']))
            return result

    def reject_appointment_operation(self, tenant_id, key, token, error):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            operation = db.execute('SELECT * FROM appointment_operations WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if not operation or operation['status'] != 'pending' or operation['token'] != token:
                raise ValueError('appointment operation claim was lost')
            result = {'ok':False,'error':error}
            db.execute("UPDATE appointment_operations SET status='rejected',result=? WHERE tenant_id=? AND key=?",
                       (json.dumps(result),tenant_id,key))
            db.execute('INSERT INTO idempotency(tenant_id,key,result,request_hash) VALUES (?,?,?,?)',
                       (tenant_id,key,json.dumps(result),operation['request_hash']))
            return result

    def release_appointment_operation(self, tenant_id, key, token):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute("DELETE FROM appointment_operations WHERE tenant_id=? AND key=? AND token=? AND status='pending'",
                             (tenant_id,key,token))
            if cur.rowcount != 1:
                raise ValueError('appointment operation claim was lost')

    def slot_available(self, tenant_id, start_at, end_at, exclude_id=None):
        with self.connect() as db:
            return not _has_slot_conflict(db,tenant_id,start_at,end_at,exclude_id)

    def reserve_external_booking(self, tenant_id, name, phone, service, start_at, end_at, key):
        request_hash = request_fingerprint(['book', name, phone, service, start_at, end_at])
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT id,status,request_hash,reservation_at FROM appointments WHERE tenant_id=? AND request_key=?', (tenant_id,key)).fetchone()
            if prior:
                if prior['request_hash'] != request_hash:
                    raise ValueError('idempotency key reused for a different request')
                if prior['status'] == 'pending' and prior['reservation_at'] and datetime.fromisoformat(prior['reservation_at']) < datetime.now(timezone.utc)-timedelta(minutes=2):
                    db.execute('UPDATE appointments SET reservation_at=? WHERE id=?', (now(),prior['id']))
                    return {'ok': True, 'appointment_id': prior['id'], 'status': 'pending', 'claimed': True}
                return {'ok': prior['status'] in ('pending','confirmed'), 'appointment_id': prior['id'], 'status': prior['status'], 'claimed': False}
            used_key = db.execute('SELECT request_hash FROM idempotency WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if used_key:
                raise ValueError('idempotency key reused for a different request')
            if _has_slot_conflict(db,tenant_id,start_at,end_at):
                return {'ok': False, 'error': 'slot_unavailable'}
            aid = str(uuid.uuid4())
            db.execute('INSERT INTO appointments(id,tenant_id,external_id,name,phone,service,start_at,end_at,status,request_key,request_hash,reservation_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (aid,tenant_id,None,name,phone,service,start_at,end_at,'pending',key,request_hash,now()))
            return {'ok': True, 'appointment_id': aid, 'status': 'pending', 'claimed': True}

    def finish_external_booking(self, tenant_id, appointment_id, key, external_id, start_at):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,request_hash FROM appointments WHERE tenant_id=? AND id=? AND request_key=?',
                             (tenant_id,appointment_id,key)).fetchone()
            if not row or row['status'] not in ('pending','confirmed'):
                raise ValueError('booking reservation is unavailable')
            result = {'ok': True, 'appointment_id': appointment_id, 'start_at': start_at}
            db.execute("UPDATE appointments SET status='confirmed',external_id=? WHERE tenant_id=? AND id=?", (external_id,tenant_id,appointment_id))
            db.execute('INSERT INTO idempotency(tenant_id,key,result,request_hash) VALUES (?,?,?,?) ON CONFLICT(tenant_id,key) DO NOTHING',
                       (tenant_id,key,json.dumps(result),row['request_hash']))
            return result

    def reject_external_booking(self, tenant_id, appointment_id, key, error):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT request_hash FROM appointments WHERE tenant_id=? AND id=? AND request_key=? AND status=?',
                             (tenant_id,appointment_id,key,'pending')).fetchone()
            if not row:
                raise ValueError('booking reservation is unavailable')
            result = {'ok': False, 'error': error}
            db.execute("UPDATE appointments SET status='rejected' WHERE tenant_id=? AND id=?", (tenant_id,appointment_id))
            db.execute('INSERT INTO idempotency(tenant_id,key,result,request_hash) VALUES (?,?,?,?)',
                       (tenant_id,key,json.dumps(result),row['request_hash']))
            return result

    def book(self, tenant_id, name, phone, service, start_at, end_at, key):
        request_hash = request_fingerprint(['book', name, phone, service, start_at, end_at])
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT result,request_hash FROM idempotency WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if prior:
                if prior['request_hash'] != request_hash:
                    raise ValueError('idempotency key reused for a different request')
                return json.loads(prior['result'])
            if _has_slot_conflict(db,tenant_id,start_at,end_at):
                return {'ok': False, 'error': 'slot_unavailable'}
            aid = str(uuid.uuid4())
            cur = db.execute('INSERT INTO appointments(id,tenant_id,external_id,name,phone,service,start_at,end_at,status,request_key,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                             (aid,tenant_id,None,name,phone,service,start_at,end_at,'confirmed',key,request_hash))
            if cur.rowcount != 1:
                return {'ok': False, 'error': 'slot_unavailable'}
            result = {'ok': True, 'appointment_id': aid, 'start_at': start_at}
            db.execute('INSERT INTO idempotency(tenant_id,key,result,request_hash) VALUES (?,?,?,?)', (tenant_id,key,json.dumps(result),request_hash))
            return result

    def appointments(self, tenant_id):
        return self.all('SELECT * FROM appointments WHERE tenant_id=? ORDER BY start_at', (tenant_id,))

    def cancel(self, tenant_id, appointment_id, key):
        request_hash = request_fingerprint(['cancel', appointment_id])
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT result,request_hash FROM idempotency WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if prior:
                if prior['request_hash'] != request_hash:
                    raise ValueError('idempotency key reused for a different request')
                return json.loads(prior['result'])
            active = db.execute("SELECT key FROM appointment_operations WHERE tenant_id=? AND appointment_id=? AND status='pending'", (tenant_id,appointment_id)).fetchone()
            if active:
                raise ValueError('appointment has an operation pending reconciliation')
            cur = db.execute('UPDATE appointments SET status=? WHERE tenant_id=? AND id=? AND status=?', ('cancelled',tenant_id,appointment_id,'confirmed'))
            result = {'ok': bool(cur.rowcount), 'appointment_id': appointment_id}
            db.execute('INSERT INTO idempotency(tenant_id,key,result,request_hash) VALUES (?,?,?,?)', (tenant_id,key,json.dumps(result),request_hash))
            return result

    def upsert_lead(self, tenant_id, name, phone, email='', queue_sync=False):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id FROM leads WHERE tenant_id=? AND phone=?', (tenant_id,phone)).fetchone()
            if row:
                db.execute('UPDATE leads SET name=?, email=? WHERE tenant_id=? AND id=?', (name,email,tenant_id,row['id']))
                lid = row['id']
            else:
                lid = str(uuid.uuid4())
                db.execute('INSERT INTO leads VALUES (?,?,?,?,?,?,?)', (lid,tenant_id,name,phone,email,'new',None))
            if queue_sync:
                current = now()
                payload_hash = request_fingerprint([name,phone,email or ''])
                db.execute('''INSERT INTO lead_sync_jobs(tenant_id,lead_id,status,attempts,last_error,updated_at,available_at,payload_hash)
                              VALUES (?,?,'pending',0,NULL,?,?,?)
                              ON CONFLICT(tenant_id,lead_id) DO UPDATE SET
                              status=CASE WHEN lead_sync_jobs.status='processing' THEN 'processing' ELSE 'pending' END,
                              last_error=NULL,
                              lease_token=CASE WHEN lead_sync_jobs.status='processing' THEN lead_sync_jobs.lease_token ELSE NULL END,
                              lease_until=CASE WHEN lead_sync_jobs.status='processing' THEN lead_sync_jobs.lease_until ELSE NULL END,
                              available_at=CASE WHEN lead_sync_jobs.status='processing' THEN lead_sync_jobs.available_at ELSE excluded.available_at END,
                              payload_hash=excluded.payload_hash,updated_at=excluded.updated_at''',
                           (tenant_id,lid,current,current,payload_hash))
            return lid

    def queue_lead_sync(self, tenant_id, lead_id):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            lead = db.execute('SELECT name,phone,email FROM leads WHERE tenant_id=? AND id=?',(tenant_id,lead_id)).fetchone()
            if not lead:
                return None
            current = now()
            payload_hash = request_fingerprint([lead['name'],lead['phone'],lead['email'] or ''])
            db.execute('''INSERT INTO lead_sync_jobs(tenant_id,lead_id,status,attempts,last_error,updated_at,available_at,payload_hash)
                          VALUES (?,?,'pending',0,NULL,?,?,?)
                          ON CONFLICT(tenant_id,lead_id) DO UPDATE SET
                          status=CASE WHEN lead_sync_jobs.status='processing' THEN 'processing' ELSE 'pending' END,
                          last_error=NULL,
                          lease_token=CASE WHEN lead_sync_jobs.status='processing' THEN lead_sync_jobs.lease_token ELSE NULL END,
                          lease_until=CASE WHEN lead_sync_jobs.status='processing' THEN lead_sync_jobs.lease_until ELSE NULL END,
                          available_at=CASE WHEN lead_sync_jobs.status='processing' THEN lead_sync_jobs.available_at ELSE excluded.available_at END,
                          payload_hash=excluded.payload_hash,updated_at=excluded.updated_at''',
                       (tenant_id,lead_id,current,current,payload_hash))
            return db.execute('SELECT * FROM lead_sync_jobs WHERE tenant_id=? AND lead_id=?', (tenant_id,lead_id)).fetchone()

    def claim_lead_sync(self, tenant_id, lead_id, lease_seconds=60):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = datetime.now(timezone.utc)
            row = db.execute('''SELECT leads.*,lead_sync_jobs.attempts,lead_sync_jobs.payload_hash FROM lead_sync_jobs
                                JOIN leads ON leads.tenant_id=lead_sync_jobs.tenant_id AND leads.id=lead_sync_jobs.lead_id
                                WHERE lead_sync_jobs.tenant_id=? AND lead_sync_jobs.lead_id=?
                                AND (lead_sync_jobs.status='pending' AND lead_sync_jobs.available_at<=?
                                     OR lead_sync_jobs.status='processing' AND lead_sync_jobs.lease_until<=?)''',
                             (tenant_id,lead_id,current.isoformat(),current.isoformat())).fetchone()
            if not row:
                return None
            token = str(uuid.uuid4())
            until = (current+timedelta(seconds=lease_seconds)).isoformat()
            db.execute("UPDATE lead_sync_jobs SET status='processing',lease_token=?,lease_until=?,updated_at=? WHERE tenant_id=? AND lead_id=?",
                       (token,until,current.isoformat(),tenant_id,lead_id))
            result = dict(row)
            result.update(lease_token=token,lease_until=until)
            return result

    def claim_pending_lead_syncs(self, limit=25, lease_seconds=60, tenant_id=None):
        claims = []
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = datetime.now(timezone.utc)
            sql = '''SELECT tenant_id,lead_id FROM lead_sync_jobs
                     WHERE ((status='pending' AND available_at<=?)
                        OR (status='processing' AND lease_until<=?))'''
            args = [current.isoformat(),current.isoformat()]
            if tenant_id:
                sql += ' AND tenant_id=?'
                args.append(tenant_id)
            sql += ' ORDER BY updated_at LIMIT ?'
            if self.is_postgres:
                sql += ' FOR UPDATE SKIP LOCKED'
            args.append(limit)
            candidates = db.execute(sql,args).fetchall()
            for candidate in candidates:
                tenant_id,lead_id = candidate['tenant_id'],candidate['lead_id']
                token = str(uuid.uuid4())
                until = (current+timedelta(seconds=lease_seconds)).isoformat()
                db.execute("UPDATE lead_sync_jobs SET status='processing',lease_token=?,lease_until=?,updated_at=? WHERE tenant_id=? AND lead_id=?",
                           (token,until,current.isoformat(),tenant_id,lead_id))
                lead = db.execute('''SELECT leads.*,lead_sync_jobs.attempts,lead_sync_jobs.payload_hash FROM leads JOIN lead_sync_jobs
                                     ON leads.tenant_id=lead_sync_jobs.tenant_id AND leads.id=lead_sync_jobs.lead_id
                                     WHERE leads.tenant_id=? AND leads.id=?''',(tenant_id,lead_id)).fetchone()
                if lead:
                    result=dict(lead)
                    result.update(lease_token=token,lease_until=until)
                    claims.append(result)
        return claims

    def lead_sync_job(self, tenant_id, lead_id):
        rows = self.all('SELECT * FROM lead_sync_jobs WHERE tenant_id=? AND lead_id=?', (tenant_id,lead_id))
        return rows[0] if rows else None

    def expedite_lead_sync_retry(self, tenant_id, lead_id):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE lead_sync_jobs SET available_at=?,updated_at=? WHERE tenant_id=? AND lead_id=? AND status='pending'",
                       (now(),now(),tenant_id,lead_id))
            return db.execute('SELECT status FROM lead_sync_jobs WHERE tenant_id=? AND lead_id=?', (tenant_id,lead_id)).fetchone()

    def record_lead_sync(self, tenant_id, lead_id, lease_token, payload_hash, external_id):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT status,payload_hash FROM lead_sync_jobs WHERE tenant_id=? AND lead_id=? AND lease_token=?", (tenant_id,lead_id,lease_token)).fetchone()
            if not job or job['status'] != 'processing':
                raise ValueError('lead sync claim was lost')
            if job['payload_hash'] != payload_hash:
                current = now()
                db.execute("UPDATE lead_sync_jobs SET status='pending',lease_token=NULL,lease_until=NULL,available_at=?,updated_at=? WHERE tenant_id=? AND lead_id=?",
                           (current,current,tenant_id,lead_id))
                return False
            db.execute('UPDATE leads SET external_id=? WHERE tenant_id=? AND id=?', (external_id,tenant_id,lead_id))
            db.execute("UPDATE lead_sync_jobs SET status='synced',attempts=attempts+1,last_error=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE tenant_id=? AND lead_id=?", (now(),tenant_id,lead_id))
            return True

    def fail_lead_sync(self, tenant_id, lead_id, lease_token, error):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT attempts,status FROM lead_sync_jobs WHERE tenant_id=? AND lead_id=? AND lease_token=?", (tenant_id,lead_id,lease_token)).fetchone()
            if not job or job['status'] != 'processing':
                raise ValueError('lead sync claim was lost')
            current = datetime.now(timezone.utc)
            delay = min(2 ** min(job['attempts'],8),300)
            available = (current+timedelta(seconds=delay)).isoformat()
            db.execute("UPDATE lead_sync_jobs SET status='pending',attempts=attempts+1,last_error=?,lease_token=NULL,lease_until=NULL,available_at=?,updated_at=? WHERE tenant_id=? AND lead_id=?",
                       (error[:500],available,current.isoformat(),tenant_id,lead_id))

    def leads(self, tenant_id):
        return self.all('''SELECT leads.*,COALESCE(lead_sync_jobs.status,'not_requested') AS crm_sync,
                           COALESCE(lead_sync_jobs.attempts,0) AS crm_sync_attempts,
                           lead_sync_jobs.last_error AS crm_sync_error
                           FROM leads LEFT JOIN lead_sync_jobs
                           ON leads.tenant_id=lead_sync_jobs.tenant_id AND leads.id=lead_sync_jobs.lead_id
                           WHERE leads.tenant_id=?''', (tenant_id,))

    def accept_webhook(self, provider, event_id, tenant_id):
        with self.connect() as db:
            result = db.execute('INSERT INTO webhooks VALUES (?,?,?,?) ON CONFLICT(provider,event_id) DO NOTHING',
                                (provider,event_id,tenant_id,now()))
            return result.rowcount == 1

    def claim_webhook_turn(self, tenant_id, key, request_hash):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            legacy = db.execute('SELECT result,request_hash FROM idempotency WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if legacy:
                if legacy['request_hash'] and legacy['request_hash'] != request_hash:
                    return {'status': 'conflict'}
                return {'status': 'complete', 'response': legacy['result']}
            row = db.execute('SELECT * FROM webhook_turns WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if row and row['request_hash'] != request_hash:
                return {'status': 'conflict'}
            if row and row['status'] == 'complete':
                return {'status': 'complete', 'response': row['response']}
            current = datetime.now(timezone.utc)
            if row and datetime.fromisoformat(row['lease_until']) > current:
                return {'status': 'pending'}
            token = str(uuid.uuid4())
            lease_until = (current + timedelta(minutes=2)).isoformat()
            if row:
                db.execute('UPDATE webhook_turns SET token=?,lease_until=? WHERE tenant_id=? AND key=?',
                           (token,lease_until,tenant_id,key))
            else:
                db.execute('INSERT INTO webhook_turns VALUES (?,?,?,?,?,?,NULL)',
                           (tenant_id,key,request_hash,'pending',token,lease_until))
            return {'status': 'claimed', 'token': token}

    def finish_webhook_turn(self, tenant_id, key, token, response, call_id, state=None, turn_event=None):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,token,response FROM webhook_turns WHERE tenant_id=? AND key=?', (tenant_id,key)).fetchone()
            if not row or row['token'] != token or row['status'] != 'pending':
                if row and row['status'] == 'complete':
                    return row['response']
                raise ValueError('webhook claim was lost')
            if state is not None:
                cur = db.execute('UPDATE calls SET state=? WHERE tenant_id=? AND id=? AND status=?',
                                 (json.dumps(state),tenant_id,call_id,'active'))
                if cur.rowcount != 1:
                    raise KeyError('active call not found')
            if turn_event is not None:
                db.execute('INSERT INTO events(tenant_id,call_id,kind,payload,at) VALUES (?,?,?,?,?)',
                           (tenant_id,call_id,'turn_completed',json.dumps(turn_event),now()))
            db.execute("UPDATE webhook_turns SET status='complete',response=? WHERE tenant_id=? AND key=?", (response,tenant_id,key))
            return response
