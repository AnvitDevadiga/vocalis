"""One-way, fail-closed transfer from the current SQLite schema to PostgreSQL."""
import argparse
import json
import os
import stat
import sqlite3
from pathlib import Path

from sqlalchemy.engine import make_url

try:
    from psycopg import connect as psycopg_connect
except ImportError:  # SQLite backup validation remains usable without PG extras installed.
    psycopg_connect = None


TABLES = (
    'tenants',
    'calls',
    'events',
    'documents',
    'appointments',
    'leads',
    'idempotency',
    'webhooks',
    'webhook_turns',
    'lead_sync_jobs',
    'appointment_operations',
)
EXPECTED_REVISION = '0001_postgres'
MIGRATION_LOCK_ID = 7211640465329942241


class TransferRefused(RuntimeError):
    """The source or target failed a safety precondition."""


def _sqlite_source(path):
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise TransferRefused(f'SQLite source does not exist: {source}')
    uri = source.as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri,uri=True,timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


def create_sqlite_backup(source_path, backup_path):
    source_file = Path(source_path).expanduser().resolve()
    backup_file = Path(backup_path).expanduser().resolve()
    if source_file == backup_file:
        raise TransferRefused('Backup path must differ from the SQLite source')
    if not source_file.is_file():
        raise TransferRefused(f'SQLite source does not exist: {source_file}')
    if not backup_file.parent.is_dir():
        raise TransferRefused('Backup parent directory does not exist')
    try:
        descriptor = os.open(backup_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,stat.S_IRUSR|stat.S_IWUSR)
    except FileExistsError as exc:
        raise TransferRefused('Backup path already exists; choose a new backup filename') from exc
    os.close(descriptor)
    try:
        source = _sqlite_source(source_file)
        backup = sqlite3.connect(backup_file)
        try:
            source.backup(backup,pages=256,sleep=0.05)
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise TransferRefused('SQLite backup integrity check failed')
            if backup.execute('PRAGMA foreign_key_check').fetchone():
                raise TransferRefused('SQLite backup foreign-key check failed')
        finally:
            source.close()
            backup.close()
        os.chmod(backup_file,stat.S_IRUSR|stat.S_IWUSR)
        backup_fd = os.open(backup_file,os.O_RDONLY)
        try:
            os.fsync(backup_fd)
        finally:
            os.close(backup_fd)
        directory_fd = os.open(backup_file.parent,os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return str(backup_file)
    except BaseException:
        backup_file.unlink(missing_ok=True)
        raise


def _postgres_url(url):
    parsed = make_url(url)
    if parsed.drivername not in ('postgresql','postgresql+psycopg','postgres'):
        raise TransferRefused('Target URL must use PostgreSQL with the psycopg driver')
    return parsed.set(drivername='postgresql').render_as_string(hide_password=False)


def _source_columns(connection, table):
    return [row['name'] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def transfer_sqlite_to_postgres(source_path, target_url, batch_size=1000):
    """Copy a complete current SQLite store into an empty, migrated PostgreSQL DB.

    The SQLite connection is read-only and held in one transaction for a consistent
    snapshot. PostgreSQL changes are committed once after all rows and counts verify.
    """
    if not 1 <= batch_size <= 20_000:
        raise ValueError('batch_size must be between 1 and 20000')

    source = _sqlite_source(source_path)
    target = None
    try:
        source.execute('BEGIN')
        integrity = source.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise TransferRefused('SQLite integrity check failed')
        if source.execute('PRAGMA foreign_key_check').fetchone():
            raise TransferRefused('SQLite foreign-key check failed')

        source_tables = {row['name'] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing_tables = [table for table in TABLES if table not in source_tables]
        if missing_tables:
            raise TransferRefused('SQLite database is missing required tables: '+', '.join(missing_tables))
        unexpected_tables = sorted(table for table in source_tables
                                   if not table.startswith('sqlite_') and table not in TABLES)
        if unexpected_tables:
            raise TransferRefused('SQLite database has unrecognized tables: '+', '.join(unexpected_tables))
        source_columns = {table:_source_columns(source,table) for table in TABLES}
        missing_columns = [f'{table}.{column}' for table,columns in source_columns.items()
                           for column in _required_source_columns(table) if column not in columns]
        if missing_columns:
            raise TransferRefused('SQLite schema is outdated; start the current app once, then retry')

        if psycopg_connect is None:
            raise TransferRefused('psycopg is required to transfer data into PostgreSQL')
        target = psycopg_connect(_postgres_url(target_url),autocommit=False)
        target.execute('BEGIN')
        target.execute('SELECT pg_advisory_xact_lock(%s)',(MIGRATION_LOCK_ID,))
        revision = target.execute('SELECT version_num FROM alembic_version').fetchone()
        if not revision or revision[0] != EXPECTED_REVISION:
            raise TransferRefused('PostgreSQL schema is not at the required Alembic revision')

        quoted_tables = ','.join(f'"{table}"' for table in TABLES)
        target.execute(f'LOCK TABLE {quoted_tables} IN ACCESS EXCLUSIVE MODE')

        for table in TABLES:
            count = target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count:
                raise TransferRefused(f'PostgreSQL target table "{table}" is not empty; no rows were copied')

        copied = {}
        for table in TABLES:
            columns = _target_columns(target,table)
            missing = [column for column in columns if column not in source_columns[table]]
            if missing:
                raise TransferRefused(f'SQLite table "{table}" is missing columns required by PostgreSQL')
            quoted_columns = ','.join(f'"{column}"' for column in columns)
            placeholders = ','.join(['%s']*len(columns))
            insert = f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})'
            cursor = source.execute(f'SELECT {quoted_columns} FROM "{table}"')
            copied[table] = 0
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                values = [tuple(row[column] for column in columns) for row in rows]
                with target.cursor() as target_cursor:
                    target_cursor.executemany(insert,values)
                copied[table] += len(values)

        target.execute("SELECT setval(pg_get_serial_sequence('events','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM events")
        for table,expected_count in copied.items():
            actual_count = target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if actual_count != expected_count:
                raise RuntimeError(f'row-count verification failed for {table}')
        target.commit()
        return copied
    except BaseException:
        if target is not None:
            try:
                target.rollback()
            except Exception:
                pass
        raise
    finally:
        source.rollback()
        source.close()
        if target is not None:
            target.close()


def _required_source_columns(table):
    # Current SQLite Store schema fields; older ad-hoc versions are rejected explicitly.
    return {
        'tenants':('id','name','api_key_hash','config'),
        'calls':('id','tenant_id','caller','channel','status','state','started_at','ended_at'),
        'events':('id','tenant_id','call_id','kind','payload','at'),
        'documents':('id','tenant_id','title','content','created_at'),
        'appointments':('id','tenant_id','external_id','name','phone','service','start_at','end_at','status','request_key','request_hash','reservation_at'),
        'leads':('id','tenant_id','name','phone','email','status','external_id'),
        'idempotency':('tenant_id','key','result','request_hash'),
        'webhooks':('provider','event_id','tenant_id','received_at'),
        'webhook_turns':('tenant_id','key','request_hash','status','token','lease_until','response'),
        'lead_sync_jobs':('tenant_id','lead_id','status','attempts','last_error','updated_at','lease_token','lease_until','available_at','payload_hash'),
        'appointment_operations':('tenant_id','key','appointment_id','kind','request_hash','status','token','lease_until','new_start','new_end','result'),
    }[table]


def _target_columns(connection, table):
    cursor = connection.execute(f'SELECT * FROM "{table}" LIMIT 0')
    return [column.name for column in cursor.description]


def main():
    parser = argparse.ArgumentParser(description='Copy a current SQLite Vocalis database into an empty migrated PostgreSQL database')
    parser.add_argument('--source',default=os.getenv('VOCALIS_DB_PATH','data/vocalis.db'))
    parser.add_argument('--target',default=os.getenv('VOCALIS_DATABASE_URL'))
    parser.add_argument('--backup',required=True,help='new filename for a verified, read-consistent SQLite backup')
    parser.add_argument('--batch-size',type=int,default=1000)
    args = parser.parse_args()
    if not args.target:
        parser.error('set VOCALIS_DATABASE_URL or pass --target')
    try:
        backup_path = create_sqlite_backup(args.source,args.backup)
        counts = transfer_sqlite_to_postgres(args.source,args.target,args.batch_size)
    except TransferRefused as exc:
        parser.exit(2,f'transfer refused: {exc}\n')
    except Exception as exc:
        parser.exit(1,f'transfer failed ({type(exc).__name__}); inspect the PostgreSQL target before retrying\n')
    print(json.dumps({'status':'complete','sqlite_backup':backup_path,'rows_copied':counts},sort_keys=True))


if __name__ == '__main__':
    main()
