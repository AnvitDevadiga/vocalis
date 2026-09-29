#!/bin/bash
# Setup script for Voice Receptionist
# Works on macOS and Linux

echo "========================================"
echo "  Ava Voice Receptionist — Setup"
echo "========================================"
echo ""

# Check Python version
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $python_version"

# Create virtual environment
echo ""
echo "📦 Creating virtual environment..."
python3 -m venv venv

# Activate
source venv/bin/activate

# Upgrade pip
echo "⬆️  Upgrading pip..."
pip install --upgrade pip

# Install requirements
echo ""
echo "📥 Installing Python packages..."
echo "   (This may take a few minutes — torch is ~500MB)"
pip install -r requirements.txt

# Check if .env exists
if [ ! -f .env ]; then
    echo ""
    echo "📝 Creating .env file..."
    cp .env.example .env
    echo ""
    echo "⚠️  IMPORTANT: Edit .env and add your GROQ_API_KEY"
    echo "   Get one free at: https://console.groq.com/keys"
fi

echo ""
echo "========================================"
echo "  ✅ Setup Complete!"
echo "========================================"
echo ""
echo "To start the server:"
echo "   source venv/bin/activate"
echo "   python app.py"
echo ""
echo "Then open: http://localhost:8000"
echo ""
