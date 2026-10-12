"""Apply database migrations before launching the HTTP/WebSocket service."""
import os
import subprocess
import sys

import uvicorn


def main():
    if os.getenv('VOCALIS_DATABASE_URL') and os.getenv('VOCALIS_AUTO_MIGRATE','1') == '1':
        subprocess.run([sys.executable,'-m','alembic','upgrade','head'],check=True)
    uvicorn.run('app:app',host='0.0.0.0',port=int(os.getenv('PORT','8000')),
                ws_max_size=5_000_000,ws_max_queue=16)


if __name__ == '__main__':
    main()
