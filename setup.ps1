# One-time setup. Run from the repo root:  .\setup.ps1
$ErrorActionPreference = "Stop"

Write-Host "-> Creating Python virtual environment..."
Push-Location backend
try {
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip

    Write-Host "-> Installing dependencies (this takes a few minutes)..."
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt

    Write-Host "-> Downloading the language model used to find names (~600 MB)..."
    & .\.venv\Scripts\python.exe -m spacy download en_core_web_lg

    Write-Host ""
    Write-Host "Setup complete. Start the app with:  .\run.ps1"
} finally {
    Pop-Location
}
