"""Privacy maintenance commands for stored call data."""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

from vocalis.store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect or prune expired Vocalis call data')
    parser.add_argument('--retention-days', type=int,
                        default=int(os.getenv('VOCALIS_CALL_RETENTION_DAYS', '30')),
                        help='age after which ended calls and stale active calls are eligible (default: 30)')
    parser.add_argument('--batch-size', type=int, default=500)
    parser.add_argument('--apply', action='store_true', help='delete eligible records; without this flag the command is a dry run')
    args = parser.parse_args(argv)
    if not 1 <= args.retention_days <= 3650:
        parser.error('--retention-days must be between 1 and 3650')
    if not 1 <= args.batch_size <= 5000:
        parser.error('--batch-size must be between 1 and 5000')
    database = os.getenv('VOCALIS_DATABASE_URL') or os.getenv('VOCALIS_DB_PATH', 'data/vocalis.db')
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.retention_days)).isoformat()
    store = Store(database)
    count = store.prune_call_data(cutoff, batch_size=args.batch_size, apply=args.apply)
    print(json.dumps({'mode':'apply' if args.apply else 'dry_run',
                      'retention_days':args.retention_days,
                      'eligible_calls':count,
                      'deleted_calls':count if args.apply else 0}, sort_keys=True))


if __name__ == '__main__':
    main()
