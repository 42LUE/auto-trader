#!/usr/bin/env bash
# Optional: run the daily job locally instead of GitHub Actions.
cd "$(dirname "$0")" && python3 daily.py >> state/log.txt 2>&1
