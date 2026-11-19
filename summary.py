"""Daily / weekly reports (deterministic, no AI).

  python summary.py daily    -> reports/daily/YYYY-MM-DD.md   (+ phone push)
  python summary.py weekly   -> reports/weekly/YYYY-Www.md    (+ phone push)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

import bot

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, "reports")
SNAP = os.path.join(HERE, "data", "snapshot.json")
BACKTEST_MAX_DD = -0.25
BACKTEST_TRADES_PER_WEEK = 19.5 / 52


def acct(name: str) -> dict | None:
    P = bot.Paths(name)
    if not os.path.exists(P.equity):
        return None
    eq = pd.read_csv(P.equity, parse_dates=["time"])
    tr = pd.read_csv(P.trades, parse_dates=["time"]) if os.path.exists(P.trades) else pd.DataFrame()
    st = json.load(open(P.state))
    flag = "🛑 SHUT DOWN" if os.path.exists(P.shutdown) else "⏸️ PAUSED" if os.path.exists(P.paused) else "✅"
    return {"eq": eq, "tr": tr, "st": st, "flag": flag}


def _exec_log() -> dict:
    p = bot.Paths("claude").executions
    return json.load(open(p)) if os.path.exists(p) else {}


def _signal_lines() -> list[str]:
    if not os.path.exists(SNAP):
        return []
    s = json.load(open(SNAP))
    out = [f"**Market** BTC/AUD {s['close']:,.0f} · 1d {s['returns']['1d']:+.1%} · "
           f"7d {s['returns']['7d']:+.1%} · 30d {s['returns']['30d']:+.1%} · RSI {s['rsi14']}"]
    mas = " · ".join(f"{n}d {v['price_vs_ma']:+.1%}" for n, v in s["moving_averages"].items())
    out.append(f"Price vs averages: {mas} · rule wants {s['rule_benchmark_target']:.0%} BTC")
    return out


def daily() -> str:
    c, r = acct("claude"), acct("rule")
    any_ = c or r
    if not any_:
        return "No runs yet."
    candle = any_["eq"].iloc[-1]["candle"]
    lines = [f"# Daily: {candle}", ""]
    lines += _signal_lines() + [""]
    for name, a in (("Claude (trades for you)", c), ("Rule benchmark", r)):
        if not a:
            continue
        e = a["eq"]
        last, prev = e.iloc[-1], e.iloc[-2] if len(e) > 1 else e.iloc[-1]
        expo = last["btc"] * last["price"] / max(last["equity"], 1e-9)
        lines.append(f"- **{name}** {a['flag']} {a['st']['mode']}: ${last['equity']:.2f} "
                     f"({last['equity'] - prev['equity']:+.2f} today, "
                     f"{last['equity'] - a['st']['start_capital']:+.2f} total) · {expo:.0%} BTC · "
                     f"last action: {last['action']}")
    bh = any_["eq"].iloc[-1]["buy_hold_equity"]
    lines.append(f"- Buy & hold BTC since start: ${bh:.2f}")
    ex = _exec_log()
    today = [v for k, v in ex.items() if v.get("checked_at", "")[:10] == datetime.now(timezone.utc).strftime("%Y-%m-%d")]
    if today:
        lines += ["", "**Claude decisions processed today:**"]
        lines += [f"- {v['decision']}: {v['action']} → {v['target']} — {v['outcome']}" for v in today]
    return "\n".join(lines)


def weekly() -> str:
    c, r = acct("claude"), acct("rule")
    if not (c or r):
        return "No runs yet."
    base = c or r
    end = base["eq"]["time"].max()
    lines = [f"# Weekly: week ending {base['eq'].iloc[-1]['candle']}", "",
             "| Account | This week | Since start | Trades (wk / total) | Fees |", "|---|---|---|---|---|"]
    warn = []
    for name, a in (("Claude", c), ("Rule", r)):
        if not a:
            continue
        e = a["eq"]
        wk = e[e["time"] >= end - pd.Timedelta(days=7)]
        start = a["st"]["start_capital"]
        ntr = len(a["tr"])
        wtr = len(a["tr"][a["tr"]["time"] >= end - pd.Timedelta(days=7)]) if ntr else 0
        lines.append(f"| {name} {a['flag']} | {e.iloc[-1]['equity'] / wk.iloc[0]['equity'] - 1:+.1%} | "
                     f"{e.iloc[-1]['equity'] / start - 1:+.1%} (${e.iloc[-1]['equity']:.2f}) | {wtr} / {ntr} | "
                     f"${a['st']['fees_paid']:.3f} |")
        dd = (e["equity"] / e["equity"].cummax() - 1).min()
        weeks = max((e["time"].max() - e["time"].min()).days / 7, 1)
        if dd < BACKTEST_MAX_DD:
            warn.append(f"{name}: drawdown {dd:.0%} is worse than the rule's backtest worst ({BACKTEST_MAX_DD:.0%}).")
        if ntr >= 4 and ntr / weeks > 3 * BACKTEST_TRADES_PER_WEEK:
            warn.append(f"{name}: {ntr / weeks:.1f} trades/week vs ~{BACKTEST_TRADES_PER_WEEK:.1f} for the rule "
                        "— fees will bite.")
        if a["st"]["fees_paid"] > 0.05 * start:
            warn.append(f"{name}: fees ${a['st']['fees_paid']:.2f} are over 5% of starting capital.")
    e = base["eq"]
    wk = e[e["time"] >= end - pd.Timedelta(days=7)]
    lines.append(f"| Buy & hold | {e.iloc[-1]['buy_hold_equity'] / wk.iloc[0]['buy_hold_equity'] - 1:+.1%} | "
                 f"{e.iloc[-1]['buy_hold_equity'] / 20 - 1:+.1%} | – | – |")
    if c and r:
        gap = c["eq"].iloc[-1]["equity"] - r["eq"].iloc[-1]["equity"]
        lines += ["", f"**Claude vs rule: {gap:+.2f}** — "
                  + ("Claude is ahead." if gap > 0.005 else "the rule is ahead." if gap < -0.005 else "level.")]
    ex = _exec_log()
    wex = [v for v in ex.values() if v.get("checked_at", "") >= (end - pd.Timedelta(days=7)).isoformat()]
    if wex:
        lines += ["", "**Claude's decisions this week:**"]
        lines += [f"- {v['decision']}: {v['action']} → {v['target']} — {v['outcome']}" for v in wex]
    lines += [""] + _signal_lines()
    lines += ["", "**What could I be wrong about?**"]
    lines += [f"- ⚠️ {w}" for w in warn] or ["- Nothing outside expectations this week."]
    return "\n".join(lines)


def write(kind: str) -> str:
    text = daily() if kind == "daily" else weekly()
    now = datetime.now(timezone.utc)
    name = now.strftime("%Y-%m-%d") if kind == "daily" else now.strftime("%G-W%V")
    d = os.path.join(REPORTS, kind)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{name}.md"), "w") as f:
        f.write(text + "\n")
    bot.notify(bot.load_cfg(), f"Trader {kind} summary", text.replace("**", ""))
    return text


if __name__ == "__main__":
    print(write(sys.argv[1] if len(sys.argv) > 1 else "daily"))
