@echo off
REM Runs the app against a LOCAL FAKE Meshy server - no real API calls, no credits.
REM Uses a separate data folder so your real history/settings are untouched.
cd /d "%~dp0"
start "Fake Meshy API" .venv\Scripts\python.exe -m tests.fake_meshy
set MESHY_API_BASE=http://127.0.0.1:8765
set MESHY_API_KEY=test-key
set MESHY_STUDIO_HOME=%~dp0demo_data
.venv\Scripts\python.exe app.py
