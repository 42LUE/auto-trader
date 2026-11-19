"""Market snapshot for Claude's daily research (written by GitHub Actions, which
can reach the exchange; Claude's cloud session reads it from the repo)."""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

import bot

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "data", "snapshot.json")
RECENT = os.path.join(HERE, "data", "btc-aud-daily-recent.csv")


def _rsi(c: pd.Series, n: int = 14) -> float:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return float(100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1])) if dn.iloc[-1] > 0 else 100.0


def snapshot(cfg: dict, mkt: bot.Market) -> dict:
    df = mkt.df
    c = df["close"]
    last = float(c.iloc[-1])

    def ret(n):
        return round(float(c.iloc[-1] / c.iloc[-1 - n] - 1), 4) if len(c) > n else None

    mas = {}
    for n in (20, 50, 100, 150, 200):
        if len(c) >= n:
            m = float(c.rolling(n).mean().iloc[-1])
            mas[str(n)] = {"value": round(m, 2), "price_vs_ma": round(last / m - 1, 4)}
    rv = float(np.log(c).diff().tail(30).std() * np.sqrt(365))
    hi365, lo365 = float(c.tail(365).max()), float(c.tail(365).min())
    snap = {
        "generated_at": bot.now_utc().isoformat(),
        "symbol": cfg["symbol"], "exchange": cfg["exchange"],
        "last_closed_candle": mkt.candle, "close": round(last, 2), "price_now": round(mkt.price, 2),
        "returns": {"1d": ret(1), "7d": ret(7), "30d": ret(30), "90d": ret(90), "365d": ret(365)},
        "moving_averages": mas,
        "rsi14": round(_rsi(c), 1),
        "realised_vol_30d_annual": round(rv, 3),
        "range_365d": {"high": round(hi365, 2), "low": round(lo365, 2),
                       "drawdown_from_high": round(last / hi365 - 1, 4)},
        "rule_benchmark_target": bot.rule_target(cfg, df),
        "last_14_closes": {str(k.date()): round(float(v), 2) for k, v in c.tail(14).items()},
        "accounts": {},
    }
    for a in bot.ACCOUNTS:
        P = bot.Paths(a)
        if os.path.exists(P.state):
            st = json.load(open(P.state))
            eq = st["cash"] + st["base"] * mkt.price
            snap["accounts"][a] = {
                "mode": st["mode"], "equity": round(eq, 4),
                "btc_exposure": round(st["base"] * mkt.price / eq, 3) if eq > 0 else 0,
                "paused": os.path.exists(P.paused), "shutdown": os.path.exists(P.shutdown)}
    os.makedirs(os.path.dirname(SNAP), exist_ok=True)
    with open(SNAP, "w") as f:
        json.dump(snap, f, indent=2)
    df.tail(400).round(2).to_csv(RECENT, index_label="date")
    return snap
