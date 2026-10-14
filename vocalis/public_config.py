"""Configuration for Vocalis's public assistant endpoint."""

import os


def enabled() -> bool:
    return os.getenv('VOCALIS_PUBLIC_ASSISTANT') == '1'


def tenant_id() -> str | None:
    return os.getenv('VOCALIS_PUBLIC_TENANT_ID')


def tenant_name() -> str | None:
    return os.getenv('VOCALIS_PUBLIC_TENANT_NAME') or 'Vocalis'


def set_tenant_id(value: str) -> None:
    os.environ['VOCALIS_PUBLIC_TENANT_ID'] = value


def is_tenant(value: str) -> bool:
    return enabled() and tenant_id() == value
