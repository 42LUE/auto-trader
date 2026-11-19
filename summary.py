"""Daily / weekly summaries from the bot's ledger. Deterministic: no AI, no cost.

  python summary.py daily    -> reports/daily/YYYY-MM-DD.md  (+ phone push)
  python summary.py weekly   -> reports/weekly/YYYY-Www.md   (+ phone push)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import yaml

import bot

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, "reports")
BACKTEST_MAX_DD = -0.25      # out-of-sample worst case, for the sanity checks
BACKTEST_TRADES_PER_WEEK = 19.5 / 52


def _load():
    cfg = bot.load_cfg(os.path.join(HERE, "config.yaml"))
    if not os.path.exists(bot.EQUITY):
        return cfg, None, None, None
    eq = pd.read_csv(bot.EQUITY, parse_dates=["time"])
    tr = pd.read_csv(bot.TRADES, parse_dates=["time"]) if os.path.exists(bot.TRADES) else pd.DataFrame()
    with open(bot.STATE) as f:
        st = json.load(f)
    return cfg, eq, tr, st


def _status_line():
    if os.path.exists(bot.SHUTDOWN):
        return "🛑 SHUT DOWN (kill switch)"
    if os.path.exists(bot.PAUSED):
        return "⏸️ PAUSED, needs your approval"
    return "✅ running"


def _signal_block(st) -> list[str]:
    sig = st.get("last_signal")
    if not sig:
        return []
    c = sig["close"]
    lines = ["", "**Signal** (price vs moving averages; 3-4 above = 100%, 2 = 50%, else cash):"]
    for n, ma in sig["mas"].items():
        lines.append(f"- {n}-day avg {ma:,.0f}: price is {c / ma - 1:+.1%} {'above ✓' if c > ma else 'below'}")
    return lines


def daily() -> str:
    cfg, eq, tr, st = _load()
    if eq is None:
        return "No runs yet."
    last = eq.iloc[-1]
    prev = eq.iloc[-2] if len(eq) > 1 else last
    start = st["start_capital"]
    expo = last["btc"] * last["price"] / max(last["equity"], 1e-9)
    day_chg = last["equity"] - prev["equity"]
    lines = [
        f"# Daily: {last['candle']} ({cfg['mode']})",
        f"{_status_line()} · action today: **{last['action']}**",
        "",
        f"- Equity **${last['equity']:.2f}** ({day_chg:+.2f} today, {last['equity'] - start:+.2f} total)",
        f"- Buy & hold would be ${last['buy_hold_equity']:.2f}",
        f"- BTC {last['price']:,.0f} · exposure {expo:.0%} · target {last['target']:.0%}",
    ]
    lines += _signal_block(st)
    return "\n".join(lines)


def weekly() -> str:
    cfg, eq, tr, st = _load()
    if eq is None:
        return "No runs yet."
    start = st["start_capital"]
    last = eq.iloc[-1]
    wk = eq[eq["time"] >= eq["time"].max() - pd.Timedelta(days=7)]
    w0 = wk.iloc[0]
    wtr = tr[tr["time"] >= eq["time"].max() - pd.Timedelta(days=7)] if len(tr) else tr
    peak = eq["equity"].cummax()
    dd_now = last["equity"] / peak.iloc[-1] - 1
    max_dd = (eq["equity"] / peak - 1).min()
    weeks = max((eq["time"].max() - eq["time"].min()).days / 7, 1)
    lines = [
        f"# Weekly: week ending {last['candle']} ({cfg['mode']})",
        _status_line(),
        "",
        "| | This week | Since start |",
        "|---|---|---|",
        f"| Bot | {last['equity'] / w0['equity'] - 1:+.1%} | {last['equity'] / start - 1:+.1%} (${last['equity']:.2f}) |",
        f"| Buy & hold BTC | {last['buy_hold_equity'] / w0['buy_hold_equity'] - 1:+.1%} | "
        f"{last['buy_hold_equity'] / start - 1:+.1%} (${last['buy_hold_equity']:.2f}) |",
        "",
        f"- Trades this week: {len(wtr)} · total {len(tr)} · fees paid ${st['fees_paid']:.3f}",
        f"- Drawdown now {dd_now:.1%} · worst so far {max_dd:.1%} (pause at "
        f"{-(cfg.get('pause_drawdown') or 1):.0%})",
        f"- Does it pay for itself yet? Profit ${last['equity'] - start:+.2f}. No."
        if last["equity"] - start < 30 else f"- Profit ${last['equity'] - start:+.2f}.",
    ]
    lines += _signal_block(st)
    # deterministic "what could I be wrong about" checks
    warn = []
    if max_dd < BACKTEST_MAX_DD:
        warn.append(f"Drawdown {max_dd:.0%} is worse than the backtest's worst ({BACKTEST_MAX_DD:.0%}).")
    if len(tr) / weeks > 3 * BACKTEST_TRADES_PER_WEEK and len(tr) >= 4:
        warn.append(f"Trading {len(tr) / weeks:.1f}x/week vs ~{BACKTEST_TRADES_PER_WEEK:.1f} in backtest: "
                    "whipsaw or a bug.")
    gap = (last["equity"] / start) - (last["buy_hold_equity"] / start)
    if gap < -0.15:
        warn.append(f"Lagging buy & hold by {-gap:.0%}: normal in a fast rally, a concern if it persists.")
    if st["fees_paid"] > 0.05 * start:
        warn.append(f"Fees (${st['fees_paid']:.2f}) are over 5% of starting capital.")
    lines += ["", "**What could I be wrong about?**"]
    lines += [f"- ⚠️ {w}" for w in warn] or ["- Nothing outside backtest expectations this week."]
    return "\n".join(lines)


def write(kind: str) -> str:
    text = daily() if kind == "daily" else weekly()
    now = datetime.now(timezone.utc)
    name = now.strftime("%Y-%m-%d") if kind == "daily" else now.strftime("%G-W%V")
    d = os.path.join(REPORTS, kind)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{name}.md"), "w") as f:
        f.write(text + "\n")
    cfg = bot.load_cfg(os.path.join(HERE, "config.yaml"))
    bot.notify(cfg, f"Trader {kind} summary", text.replace("**", ""))
    return text


if __name__ == "__main__":
    print(write(sys.argv[1] if len(sys.argv) > 1 else "daily"))
