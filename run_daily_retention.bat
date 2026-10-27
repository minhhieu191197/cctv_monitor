@echo off
chcp 65001 > nul
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

if not exist "logs" mkdir "logs"
if not exist "reports" mkdir "reports"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i
set LOGFILE=logs\scheduled_%TODAY%.log

echo. >> "%LOGFILE%"
echo ===== BAT START %date% %time% ===== >> "%LOGFILE%"

REM Retention: CSV 30 days; HTML 7 days; scheduled logs 7 days.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath 'reports' -File -Filter '*.csv' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force -ErrorAction SilentlyContinue" >> "%LOGFILE%" 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath 'reports' -File -Filter '*.html' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Force -ErrorAction SilentlyContinue" >> "%LOGFILE%" 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath 'logs' -File -Filter 'scheduled_*.log' -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Force -ErrorAction SilentlyContinue" >> "%LOGFILE%" 2>&1

"C:\Users\tmhieu\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 monitor.py >> "%LOGFILE%" 2>&1
if errorlevel 1 (
  echo ERROR: monitor.py failed. Teams send skipped. >> "%LOGFILE%"
  echo ===== BAT END WITH ERROR %date% %time% ===== >> "%LOGFILE%"
  exit /b 1
)

"C:\Users\tmhieu\AppData\Local\Programs\Python\Python312\python.exe" -X utf8 send_teams_report.py >> "%LOGFILE%" 2>&1
if errorlevel 1 echo WARNING: Teams send failed; check webhook/environment and log. >> "%LOGFILE%"

echo ===== BAT END %date% %time% ===== >> "%LOGFILE%"
endlocal
