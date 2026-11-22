"""The daily job (GitHub Actions, ~11:41am Sydney + backup runs at 12:41 / 2:41pm). One market fetch, then:
  1. market snapshot for Claude's research      -> data/snapshot.json
  2. rule benchmark trades automatically         -> state/rule/
  3. any approved Claude decisions execute       -> state/claude/
  4. Claude account marked to market + risk rails
  5. daily (+ Sunday weekly) report, dashboard data

  python daily.py [--offline CSV --trust-local]
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import bot
import build_site
import execute
import market
import summary

ap = argparse.ArgumentParser()
ap.add_argument("--offline")
ap.add_argument("--trust-local", action="store_true")
ap.add_argument("--no-reports", action="store_true")
a = ap.parse_args()

cfg = bot.load_cfg()
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
already_reported = os.path.exists(os.path.join(summary.REPORTS, "daily", f"{today}.md"))
mkt = bot.Market(cfg, a.offline, live=(bot.acct_mode(cfg, "claude") == "live"))
bot.cycle(cfg, "rule", mkt, bot.rule_target(cfg, mkt.df), f"rule {cfg['accounts']['rule']['strategy']}")
executed = execute.execute_pending(cfg, mkt, a.trust_local)
bot.cycle(cfg, "claude", mkt, None, "daily mark")
market.snapshot(cfg, mkt)
# Backup schedule runs on a day that's already reported only refresh the snapshot and
# dashboard, so Kaileb doesn't get 3 identical phone pushes.
if not a.no_reports and (not already_reported or executed):
    summary.write("daily")
    if datetime.now(timezone.utc).isoweekday() == 7:
        summary.write("weekly")
build_site.build()
for acc in bot.ACCOUNTS:
    bot.status(cfg, acc)
