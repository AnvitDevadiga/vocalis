@echo off
echo ========================================
echo   Ava Voice Receptionist — Setup
echo ========================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Please install Python 3.9+ from python.org
    pause
    exit /b 1
)

echo Creating virtual environment...
python -m venv venv

echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Upgrading pip...
pip install --upgrade pip

echo.
echo Installing Python packages (this may take a few minutes)...
pip install -r requirements.txt

if not exist .env (
    echo.
    echo Creating .env file...
    copy .env.example .env
    echo.
    echo IMPORTANT: Edit .env and add your GROQ_API_KEY
    echo Get one free at: https://console.groq.com/keys
)

echo.
echo ========================================
echo   Setup Complete!
echo ========================================
echo.
echo To start the server:
echo   venv\Scripts\activate.bat
echo   python app.py
echo.
echo Then open: http://localhost:8000
echo.
pause
