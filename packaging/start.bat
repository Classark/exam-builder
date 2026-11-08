@echo off
rem Classark Exam Builder - starts the local server and opens it in the browser.
rem First run downloads Python 3.12 and the libraries it needs (internet required once);
rem after that it starts offline. Everything stays on this computer: the server listens on
rem 127.0.0.1 only, so nothing else on the network can reach it.
setlocal
cd /d "%~dp0engine"

where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo  This needs "uv", a small tool that sets up Python for it. Install it once with:
    echo.
    echo     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    echo.
    echo  then close this window and run start.bat again.
    echo.
    pause
    exit /b 1
)

echo Setting up (the first run takes a minute)...
uv sync --frozen --no-dev
if errorlevel 1 (
    echo.
    echo  Setup failed - see the message above.
    pause
    exit /b 1
)

rem open the browser once the server has had a moment to start
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"
echo.
echo  Classark Exam Builder is running at http://127.0.0.1:8000
echo  Close this window to stop it.
echo.
uv run --frozen --no-dev uvicorn main:app --host 127.0.0.1 --port 8000
