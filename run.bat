@echo off
chcp 65001 >nul
REM 本机同时作为 3080 服务器和相机客户机
cd /d %~dp0
set PYTHONUTF8=1
if "%OCR_DEVICE%"=="" set OCR_DEVICE=gpu
echo 正在启动服务器 8000 和客户机采集代理 8812...
start "云小圈-3080服务器" /min cmd /c "cd /d %~dp0 && .venv\Scripts\python.exe -m uvicorn app.server:app --host 0.0.0.0 --port 8000"
timeout /t 2 /nobreak >nul
start "云小圈-客户机采集代理" /min cmd /c "cd /d %~dp0 && .venv\Scripts\python.exe -m client.capture_agent --config client\client_config.json --host 127.0.0.1 --port 8812"
echo 启动完成，请打开 http://127.0.0.1:8000/camera
pause
