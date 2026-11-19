#!/usr/bin/env bash
# macOS/Linux: crontab -e  ->  30 11 * * * /path/to/trader/run_daily.sh
cd "$(dirname "$0")" && mkdir -p state && python3 bot.py run >> state/log.txt 2>&1 && python3 bot.py status
