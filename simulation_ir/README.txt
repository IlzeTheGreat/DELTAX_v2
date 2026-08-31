DELTAX EVENT FULL SCHEDULER UPDATE

Copy BOTH files into:
C:\00_Ilze\Whiskas\2026_08_28_Alpaca_hackathon\Deltax_v2\simulation_ir\

1) deltax_event_adaptive_scanner.py
2) run_deltax_event_iran_v2.cmd

Overwrite the existing files when Windows asks.

Keep the already installed:
- deltax_event_iran_v2.py
- deltax_event_adaptive_manager.py
- Windows Task Scheduler task DeltaX_Event_Iran_V2

NO scheduler reinstall is needed because the task already points to:
simulation_ir\run_deltax_event_iran_v2.cmd

WHAT EACH 5-MINUTE CYCLE NOW DOES

1. Runs historical Event Playbook.
   Its own 10:00 ET cutoff prevents late historical entries.

2. Runs adaptive scanner.
   - entry window 09:40-11:30 ET
   - scans the full watchlist again
   - already executed adaptive tickers are skipped
   - only REAL orders with order_id count toward the max
   - stale dry-run/phantom entries such as LITE are removed
   - maximum 5 real adaptive trades/day

3. Runs adaptive position manager.
   - does nothing before 15:50 ET
   - closes recorded adaptive positions at/after 15:50 ET

TEST

Run:
simulation_ir\run_deltax_event_iran_v2.cmd

Then:
Get-Content simulation_ir\deltax_event_iran_v2_scheduler.log -Tail 180

You should see, in order:
DELTAX EVENT - IRAN PLAYBOOK V2
DELTAX CURRENT-EVENT ADAPTIVE SCANNER
DELTAX ADAPTIVE POSITION MANAGER
