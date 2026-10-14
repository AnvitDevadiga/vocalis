import os

import pytest

from vocalis.runtime_secrets import load_calendar_secret_file


def test_loads_only_calendar_values_without_overriding_environment(tmp_path, monkeypatch):
    path = tmp_path / 'calendar.env'
    path.write_text(
        'GOOGLE_CALENDAR_TENANT_ID="tenant-id"\n'
        'GOOGLE_CALENDAR_CLIENT_SECRET="client-secret"\n'
        'VOCALIS_TIMEZONE="Asia/Kolkata"\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('GOOGLE_CALENDAR_TENANT_ID', 'render-env-wins')

    load_calendar_secret_file(path)

    assert os.environ['GOOGLE_CALENDAR_TENANT_ID'] == 'render-env-wins'
    assert os.environ['GOOGLE_CALENDAR_CLIENT_SECRET'] == 'client-secret'
    assert os.environ['VOCALIS_TIMEZONE'] == 'Asia/Kolkata'


@pytest.mark.parametrize('contents', [
    'GROQ_API_KEY="must-not-load"\n',
    'GOOGLE_CALENDAR_ID="one"\nGOOGLE_CALENDAR_ID="two"\n',
    'GOOGLE_CALENDAR_ID=unquoted\n',
])
def test_rejects_unknown_duplicate_or_unquoted_values(tmp_path, contents):
    path = tmp_path / 'calendar.env'
    path.write_text(contents, encoding='utf-8')

    with pytest.raises(RuntimeError, match='Invalid Calendar secret file at line'):
        load_calendar_secret_file(path)


def test_missing_secret_file_is_optional(tmp_path):
    load_calendar_secret_file(tmp_path / 'missing.env')
