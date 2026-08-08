@echo off
chcp 65001 >nul
REM 内存条日期码识别系统 - 启动脚本 (Windows)
REM 默认 GPU。改 CPU: 设 OCR_DEVICE=cpu。高精度模型: 设 OCR_SERVER_MODELS=1
cd /d %~dp0
set PYTHONUTF8=1
if "%OCR_DEVICE%"=="" set OCR_DEVICE=cpu
echo 启动中... 浏览器打开 http://127.0.0.1:8000
.venv\Scripts\python.exe -m uvicorn app.server:app --host 127.0.0.1 --port 8000
pause
