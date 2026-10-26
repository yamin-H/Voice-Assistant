#!/usr/bin/env pwsh
# Voice Assistant - Simple launcher
# Run from repository root: D:\users\playground\voice-assitant

$root = Split-Path -Parent $PSCommandPath
Set-Location $root

Write-Host "Starting Voice Assistant..." -ForegroundColor Green
Write-Host ""

# Check .env
if (-not (Test-Path ".env")) {
    Write-Host "ERROR: .env file not found in repository root!" -ForegroundColor Red
    Write-Host "Please create .env with your credentials:"
    Write-Host "  LIVEKIT_URL=wss://your-project.livekit.cloud"
    Write-Host "  LIVEKIT_API_KEY=your_key"
    Write-Host "  LIVEKIT_API_SECRET=your_secret"
    Write-Host "  GROQ_API_KEY=your_groq_key"
    Read-Host "Press Enter to exit"
    exit 1
}

# Check/create venv
if (-not (Test-Path "server\.venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv server\.venv
    Write-Host "Installing dependencies..." -ForegroundColor Yellow
    & "server\.venv\Scripts\python.exe" -m pip install -r server\requirements.txt
    Write-Host ""
}

$python = "server\.venv\Scripts\python.exe"

Write-Host "Starting Token Server..." -ForegroundColor Cyan
Start-Process "cmd.exe" -ArgumentList "/k", "`"$python`" server\token_server.py" -WindowStyle Normal

Start-Sleep -Seconds 2

Write-Host "Starting Agent Worker..." -ForegroundColor Cyan
Start-Process "cmd.exe" -ArgumentList "/k", "`"$python`" -m livekit.agents start server/agent.py --dev --log-format colored" -WindowStyle Normal

Write-Host ""
Write-Host "Both servers started in separate windows." -ForegroundColor Green
Write-Host "Open http://127.0.0.1:8000 in your browser and click 'Join room'."
Write-Host ""
Read-Host "Press Enter to exit"