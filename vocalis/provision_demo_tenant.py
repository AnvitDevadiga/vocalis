"""Create the stable tenant used by the public portfolio demo."""
import argparse
import hashlib
import secrets
import os
import sys

from vocalis.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='Vocalis Portfolio Demo')
    args = parser.parse_args()
    database = os.getenv('VOCALIS_DATABASE_URL')
    if not database:
        parser.error('VOCALIS_DATABASE_URL must point to the migrated PostgreSQL database')
    store = Store(database)
    existing = store.one('SELECT id FROM tenants WHERE name=?', (args.name,))
    if existing:
        tenant_id = existing['id']
        action = 'already exists'
    else:
        discarded_api_key = secrets.token_urlsafe(32)
        tenant_id = store.create_tenant(args.name, hashlib.sha256(discarded_api_key.encode()).hexdigest())
        action = 'created'
    print(f'{action}: {args.name}')
    print(f'VOCALIS_DEMO_TENANT_ID={tenant_id}')
    print('The tenant API key was intentionally discarded; the public demo uses the restricted WebSocket route.')
    if store.is_postgres:
        store.engine.dispose()


if __name__ == '__main__':
    main()
