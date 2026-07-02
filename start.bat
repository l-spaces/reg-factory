@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Not installed yet. Double-click install.bat first.
  pause
  exit /b 1
)

echo Starting reg-factory control panel ...
echo Panel: http://127.0.0.1:8799 or http://YOUR_LOCAL_IP:8799  (browser opens automatically)
echo Close this window to stop the server.
echo.

start "" /b cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8799"

.venv\Scripts\python.exe -m uvicorn webui.server:app --host 0.0.0.0 --port 8799
pause
