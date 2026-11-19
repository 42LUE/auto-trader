@echo off
REM Windows: point Task Scheduler at this file, daily at 11:30am.
cd /d %~dp0
python bot.py run >> state\log.txt 2>&1
python bot.py status
