#!/usr/bin/env bash
# Start Scrub. Run from the repo root:  ./run.sh
set -e

cd backend
source .venv/bin/activate

# Serve the frontend from the backend (single origin, no Node needed).
mkdir -p static
cp ../frontend/index.html static/index.html

echo "→ Starting Scrub at http://localhost:8000"
echo "   (Press Ctrl+C to stop.)"

# Open the browser shortly after the server boots.
( sleep 2 && (open http://localhost:8000 2>/dev/null || xdg-open http://localhost:8000 2>/dev/null) ) &

uvicorn app.main:app --host 127.0.0.1 --port 8000
