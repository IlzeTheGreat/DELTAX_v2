@echo off
cd /d "C:\00_Ilze\Whiskas\2026_08_28_Alpaca_hackathon\Deltax_v2"

set "PY=C:\00_Ilze\Whiskas\2026_08_28_Alpaca_hackathon\Deltax_v2\.venv\Scripts\python.exe"
set "LOG=C:\00_Ilze\Whiskas\2026_08_28_Alpaca_hackathon\Deltax_v2\simulation_ir\deltax_event_iran_v2_scheduler.log"

echo.>> "%LOG%"
echo ==================== SCHEDULER CYCLE %DATE% %TIME% ====================>> "%LOG%"

REM 1) Historical fixed playbook
"%PY%" "simulation_ir\deltax_event_iran_v2.py" --execute >> "%LOG%" 2>&1

REM 2) Current-event adaptive rescanner.
REM    Script itself enforces 09:40-11:30 ET and max 5 real adaptive trades/day.
"%PY%" "simulation_ir\deltax_event_adaptive_scanner.py" --execute >> "%LOG%" 2>&1

REM 3) Manage/close adaptive positions at 15:50 ET.
"%PY%" "simulation_ir\deltax_event_adaptive_manager.py" --execute >> "%LOG%" 2>&1
