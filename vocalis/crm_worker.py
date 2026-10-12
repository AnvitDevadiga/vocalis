"""Run `python -m vocalis.crm_worker` beside the API using the same database."""
import argparse
import asyncio
import os

from .crm import process_pending
from .store import Store


async def run(once=False, poll_seconds=5, batch_size=25):
    store = Store(os.getenv('VOCALIS_DATABASE_URL') or os.getenv('VOCALIS_DB_PATH','data/vocalis.db'))
    while True:
        results = await process_pending(store,limit=batch_size)
        print({'claimed_and_processed':len(results),'synced':sum(x['status']=='synced' for x in results)})
        if once:
            return
        await asyncio.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(description='Process retryable HubSpot lead sync jobs')
    parser.add_argument('--once',action='store_true',help='process one batch and exit')
    parser.add_argument('--poll-seconds',type=float,default=5)
    parser.add_argument('--batch-size',type=int,default=25)
    args = parser.parse_args()
    if args.poll_seconds < 0.5 or not 1 <= args.batch_size <= 500:
        parser.error('poll-seconds must be >= 0.5 and batch-size must be from 1 to 500')
    asyncio.run(run(args.once,args.poll_seconds,args.batch_size))


if __name__ == '__main__':
    main()
