# Start Scrub. Run from the repo root:  .\run.ps1
$ErrorActionPreference = "Stop"

Push-Location backend
try {
    # Serve the frontend from the backend (single origin, no Node needed).
    New-Item -ItemType Directory -Force -Path static | Out-Null
    Copy-Item ..\frontend\index.html static\index.html -Force

    Write-Host "-> Starting Scrub at http://localhost:8000"
    Write-Host "   (Press Ctrl+C to stop.)"

    # Open the browser shortly after the server boots.
    Start-Job -ScriptBlock {
        Start-Sleep -Seconds 2
        Start-Process "http://localhost:8000"
    } | Out-Null

    & .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
} finally {
    Pop-Location
}
