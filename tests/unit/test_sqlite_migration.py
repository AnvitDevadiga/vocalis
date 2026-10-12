import sqlite3
import stat

import pytest

from vocalis.migrate_sqlite import TransferRefused, create_sqlite_backup
from vocalis.store import Store


def test_sqlite_backup_captures_wal_and_refuses_overwrite(tmp_path):
    source_path = tmp_path/'source.sqlite'
    backup_path = tmp_path/'source.backup.sqlite'
    store = Store(source_path)
    tenant = store.create_tenant('Clinic','key-hash')

    keeper = sqlite3.connect(source_path)
    keeper.execute('PRAGMA journal_mode=WAL')
    keeper.execute('UPDATE tenants SET name=name WHERE id=?',(tenant,))
    keeper.commit()
    assert (tmp_path/'source.sqlite-wal').exists()

    assert create_sqlite_backup(source_path,backup_path) == str(backup_path.resolve())
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute('SELECT name FROM tenants WHERE id=?',(tenant,)).fetchone()[0] == 'Clinic'
    with pytest.raises(TransferRefused,match='already exists'):
        create_sqlite_backup(source_path,backup_path)
    keeper.close()
