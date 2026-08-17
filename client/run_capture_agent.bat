@echo off
cd /d "%~dp0.."
.venv\Scripts\python.exe -m client.capture_agent --config client\client_config.json --host 127.0.0.1 --port 8812
pause
