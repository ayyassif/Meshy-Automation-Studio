@echo off
REM One-time setup: creates .venv and installs dependencies.
cd /d "%~dp0"
where uv >nul 2>nul
if %errorlevel%==0 (
    uv venv .venv --python 3.12 || goto :error
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt || goto :error
) else (
    where py >nul 2>nul || (echo Python 3.11+ not found. Install from https://www.python.org/downloads/ ^(tick "Add to PATH"^) and re-run. & goto :error)
    py -3 -m venv .venv || goto :error
    .venv\Scripts\python.exe -m pip install --upgrade pip || goto :error
    .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :error
)
echo.
echo Setup complete. Start the app with run.bat
pause
exit /b 0
:error
echo Setup failed.
pause
exit /b 1
