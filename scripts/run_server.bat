@echo off
REM 云小圈质检服务 · 自守护启动脚本
REM 崩溃自动重启（:loop）；被 scripts\service_install.ps1 注册为开机自启任务的目标。
setlocal
cd /d "%~dp0\.."

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%CD%
set HOST=0.0.0.0
set PORT=8000

:loop
echo [%date% %time%] starting uvicorn on %HOST%:%PORT% ...
".venv\Scripts\python.exe" -m uvicorn app.server:app --host %HOST% --port %PORT%
echo [%date% %time%] uvicorn exited (code %errorlevel%). restarting in 3s ...
REM 兜底：清理可能残留占用端口的进程后再重启
timeout /t 3 /nobreak >nul
goto loop
