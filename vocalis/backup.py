"""Create restrictive, verified backups of the configured Vocalis database."""
import argparse
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

from vocalis.migrate_sqlite import create_sqlite_backup


class BackupError(RuntimeError):
    pass


def _postgres_environment(database_url):
    url = make_url(database_url)
    if not url.drivername.startswith('postgresql'):
        raise BackupError('PostgreSQL backup requires a PostgreSQL URL')
    environment = os.environ.copy()
    for key, value in {
        'PGHOST': url.host,
        'PGPORT': str(url.port) if url.port else None,
        'PGUSER': url.username,
        'PGPASSWORD': url.password,
        'PGDATABASE': url.database,
    }.items():
        if value is not None:
            environment[key] = value
    for key in ('sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'target_session_attrs', 'application_name'):
        value = url.query.get(key)
        if value:
            environment['PG' + key.upper()] = str(value)
    return environment


def create_postgres_backup(database_url, output_path):
    pg_dump = shutil.which('pg_dump')
    pg_restore = shutil.which('pg_restore')
    if not pg_dump or not pg_restore:
        raise BackupError('pg_dump and pg_restore must be installed for PostgreSQL backups')
    destination = Path(output_path).expanduser().resolve()
    if not destination.parent.is_dir():
        raise BackupError('backup directory does not exist')
    if destination.exists():
        raise BackupError('backup path already exists; choose a new filename')
    environment = _postgres_environment(database_url)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.vocalis-backup-', suffix='.partial', dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        os.close(descriptor)
        subprocess.run([pg_dump, '--format=custom', '--no-owner', '--no-privileges', '--file', str(temporary)],
                       env=environment, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        subprocess.run([pg_restore, '--list', str(temporary)], env=environment, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        with temporary.open('rb') as backup:
            os.fsync(backup.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        # Hard-link creation is atomic and refuses to overwrite an existing backup.
        os.link(temporary, destination)
        temporary.unlink()
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return str(destination)
    except FileExistsError as exc:
        raise BackupError('backup path already exists; choose a new filename') from exc
    except subprocess.CalledProcessError as exc:
        # Never print stderr: PostgreSQL clients may include connection details.
        raise BackupError('PostgreSQL backup or archive verification failed') from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)


def create_backup(database_url, sqlite_path, output_path):
    database = database_url or sqlite_path
    if database.startswith(('postgres://', 'postgresql://', 'postgresql+psycopg://')):
        return create_postgres_backup(database, output_path), 'postgresql'
    return create_sqlite_backup(database, output_path), 'sqlite'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Create a private, verified Vocalis database backup')
    parser.add_argument('--output', required=True, help='new backup filename; existing files are never overwritten')
    args = parser.parse_args(argv)
    try:
        path, backend = create_backup(os.getenv('VOCALIS_DATABASE_URL'),
                                      os.getenv('VOCALIS_DB_PATH', 'data/vocalis.db'), args.output)
    except BackupError as exc:
        parser.error(str(exc))
    except (OSError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps({'status':'complete','backend':backend,'backup':path}, sort_keys=True))


if __name__ == '__main__':
    main()
