import stat

import pytest

from vocalis.backup import BackupError, create_backup, create_postgres_backup
from vocalis.migrate_sqlite import TransferRefused
from vocalis.store import Store


def test_sqlite_backup_uses_online_backup_and_restrictive_permissions(tmp_path):
    source = tmp_path/'source.sqlite'
    store = Store(source)
    tenant = store.create_tenant('Example','hash')
    store.create_call(tenant,'browser')
    target = tmp_path/'private.sqlite'

    backup, backend = create_backup(None, str(source), str(target))

    assert backend == 'sqlite'
    assert backup == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert Store(target).all('SELECT id FROM calls')
    with pytest.raises(TransferRefused, match='already exists'):
        create_backup(None, str(source), str(target))


def test_postgres_backup_uses_client_environment_and_never_overwrites(tmp_path, monkeypatch):
    binary_dir = tmp_path/'bin'
    binary_dir.mkdir()
    args_log = tmp_path/'args.log'
    dump = binary_dir/'pg_dump'
    dump.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_LOG"\neval "out=\\${$#}"\nprintf "verified archive" > "$out"\n')
    restore = binary_dir/'pg_restore'
    restore.write_text('#!/bin/sh\nfor arg do last=$arg; done\ntest -s "$last"\n')
    dump.chmod(0o755)
    restore.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary_dir))
    monkeypatch.setenv('ARGS_LOG', str(args_log))
    output = tmp_path/'database.dump'

    result = create_postgres_backup('postgresql+psycopg://dbuser:private-password@db.example.test:5432/vocalis?sslmode=require', output)

    assert result == str(output)
    assert 'private-password' not in args_log.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(BackupError, match='already exists'):
        create_postgres_backup('postgresql://dbuser:private-password@db.example.test/vocalis', output)


def test_failed_postgres_backup_leaves_no_partial_file(tmp_path, monkeypatch):
    binary_dir = tmp_path/'bin'
    binary_dir.mkdir()
    dump = binary_dir/'pg_dump'
    dump.write_text('#!/bin/sh\necho partial > "$(eval echo \\${$#})"\nexit 1\n')
    restore = binary_dir/'pg_restore'
    restore.write_text('#!/bin/sh\nexit 0\n')
    dump.chmod(0o755)
    restore.chmod(0o755)
    monkeypatch.setenv('PATH', str(binary_dir))

    with pytest.raises(BackupError, match='backup or archive verification failed'):
        create_postgres_backup('postgresql://user:password@localhost/db', tmp_path/'failed.dump')
    assert not (tmp_path/'failed.dump').exists()
    assert list(tmp_path.glob('.vocalis-backup-*.partial')) == []
