@echo off
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
echo Edit .env, create a tenant with python -m vocalis.admin "Business Name", then run uvicorn app:app --host 127.0.0.1 --port 8000
