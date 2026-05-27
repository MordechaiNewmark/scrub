#!/usr/bin/env bash
# One-time setup. Run from the repo root:  ./setup.sh
set -e

echo "→ Creating Python virtual environment…"
cd backend
python3 -m venv .venv
source .venv/bin/activate

echo "→ Installing dependencies (this takes a few minutes)…"
pip install --upgrade pip
pip install -r requirements.txt

echo "→ Downloading the language model used to find names (~600 MB)…"
python -m spacy download en_core_web_lg

echo ""
echo "✅ Setup complete. Start the app with:  ./run.sh"
