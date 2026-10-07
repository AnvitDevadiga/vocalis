#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; fi
printf 'Edit .env, then run: set -a; source .env; set +a; python -m vocalis.admin "Business Name"; uvicorn app:app --host 127.0.0.1 --port 8000\n'
