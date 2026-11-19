@echo off
REM Optional: run the daily job on your PC instead of GitHub Actions.
cd /d %~dp0
python daily.py >> state\log.txt 2>&1
