@echo off
chcp 65001 >nul
REM Web entry on 8001. Keep OCR warmup off so this UI process does not duplicate GPU model preload.
cd /d "%~dp0\.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%CD%
set OCR_WARMUP=0
set CAM_SUPERVISOR=0
set HOST=0.0.0.0
set PORT=8001

:loop
echo [%date% %time%] starting web entry on %HOST%:%PORT% ... >> server.8001.stdout.log
".venv\Scripts\python.exe" -m uvicorn app.server:app --host %HOST% --port %PORT% >> server.8001.stdout.log 2>> server.8001.stderr.log
echo [%date% %time%] web entry exited code %errorlevel%, restarting in 3s ... >> server.8001.stderr.log
timeout /t 3 /nobreak >nul
goto loop
