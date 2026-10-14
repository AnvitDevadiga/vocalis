"""Configuration for Vocalis's public assistant endpoint.

Legacy environment names stay readable so an existing Render service continues
working while its non-secret settings are migrated to the current names.
"""

import os


def _setting(current: str, legacy: str) -> str | None:
    value = os.getenv(current)
    return value if value is not None else os.getenv(legacy)


def enabled() -> bool:
    return _setting('VOCALIS_PUBLIC_ASSISTANT', 'VOCALIS_PUBLIC_DEMO') == '1'


def tenant_id() -> str | None:
    return os.getenv('VOCALIS_PUBLIC_TENANT_ID') or os.getenv('VOCALIS_DEMO_TENANT_ID')


def tenant_name() -> str | None:
    return os.getenv('VOCALIS_PUBLIC_TENANT_NAME') or 'Vocalis'


def set_tenant_id(value: str) -> None:
    os.environ['VOCALIS_PUBLIC_TENANT_ID'] = value


def is_tenant(value: str) -> bool:
    return enabled() and tenant_id() == value
