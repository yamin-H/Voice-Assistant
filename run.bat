@echo off
REM Voice Assistant - Simple launcher
REM Run from repository root: D:\users\playground\voice-assitant

cd /d "%~dp0"

echo Starting Voice Assistant...
echo.

REM Check if .env exists
if not exist ".env" (
    echo ERROR: .env file not found in repository root!
    echo Please create .env with your credentials:
    echo   LIVEKIT_URL=wss://your-project.livekit.cloud
    echo   LIVEKIT_API_KEY=your_key
    echo   LIVEKIT_API_SECRET=your_secret
    echo   GROQ_API_KEY=your_groq_key
    pause
    exit /b 1
)

REM Check if venv exists
if not exist "server\.venv" (
    echo Creating virtual environment...
    python -m venv server\.venv
    echo Installing dependencies...
    server\.venv\Scripts\python.exe -m pip install -r server\requirements.txt
    echo.
)

echo Starting Token Server (Terminal 1)...
start "Token Server" cmd /k "server\.venv\Scripts\python.exe server\token_server.py"

echo Waiting 2 seconds for token server to start...
timeout /t 2 /nobreak >nul

echo Starting Agent Worker (Terminal 2)...
start "Agent Worker" cmd /k "server\.venv\Scripts\python.exe -m livekit.agents start server/agent.py --dev --log-format colored"

echo.
echo Both servers started in separate windows.
echo Open http://127.0.0.1:8000 in your browser and click "Join room".
echo.
pause