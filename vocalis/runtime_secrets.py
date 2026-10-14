"""Load the narrowly scoped Render Calendar secret file into process env."""
from __future__ import annotations

import ast
import os
from pathlib import Path

_CALENDAR_KEYS = frozenset({
    'GOOGLE_CALENDAR_TENANT_ID',
    'GOOGLE_CALENDAR_ID',
    'GOOGLE_CALENDAR_BUSY_IDS',
    'GOOGLE_CALENDAR_CLIENT_ID',
    'GOOGLE_CALENDAR_CLIENT_SECRET',
    'GOOGLE_CALENDAR_REFRESH_TOKEN',
    'VOCALIS_TIMEZONE',
})
_DEFAULT_SECRET_FILE = Path('/etc/secrets/google-calendar.env')


def load_calendar_secret_file(path: Path = _DEFAULT_SECRET_FILE) -> None:
    """Load approved dotenv-style values, leaving explicitly configured env intact."""
    try:
        contents = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return

    parsed: dict[str, str] = {}
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if not separator or key not in _CALENDAR_KEYS or key in parsed:
            raise RuntimeError(f'Invalid Calendar secret file at line {line_number}')
        try:
            decoded = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise RuntimeError(f'Invalid Calendar secret file at line {line_number}') from exc
        if not isinstance(decoded, str) or not decoded or '\x00' in decoded:
            raise RuntimeError(f'Invalid Calendar secret file at line {line_number}')
        parsed[key] = decoded

    for key, value in parsed.items():
        os.environ.setdefault(key, value)
