"""Fee-aware daily backtester.

Timing: signal computed at day t close -> trade at day t+1 open -> held
open-to-open. Costs are charged on every change in exposure.

Usage:  python backtest.py            (prints tables, writes results/*.csv + chart)
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

import strategies as S

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "btc-usd-daily.csv")
OUT = os.path.join(HERE, "results")

START = "2014-01-01"          # Bitstamp data before 2014 is thin
IS_END = "2021-12-31"         # in-sample: pick parameters here
OOS_START = "2022-01-01"      # out-of-sample: judge here, never tune on it
MIN_REBAL = S.MIN_REBAL


def load(path: str = DATA) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    df = df[["open", "high", "low", "close"]].astype(float)
    df = df[~df.index.duplicated()]
    return df


def run(df: pd.DataFrame, signal: pd.Series, cost_per_side: float,
        start: str | None = None, end: str | None = None, start_equity: float = 20.0) -> dict:
    """Simulate. `signal` must be computed on the FULL history (so indicators are warm)."""
    sig = signal.reindex(df.index).fillna(0.0).clip(0, 1)
    open_ = df["open"].to_numpy()
    idx = df.index
    mask = np.ones(len(df), bool)
    if start:
        mask &= idx >= pd.Timestamp(start)
    if end:
        mask &= idx <= pd.Timestamp(end)
    ii = np.flatnonzero(mask)

    eq = start_equity
    pos = 0.0                    # fraction of equity in BTC
    curve, expo = [], []
    trades = 0
    for k, i in enumerate(ii[:-1]):
        # at open of day i we act on signal from close of day i-1
        tgt = sig.iloc[i - 1] if i > 0 else 0.0
        if S.should_trade(tgt, pos, MIN_REBAL):
            eq -= eq * abs(tgt - pos) * cost_per_side
            pos = tgt
            trades += 1
        r = open_[i + 1] / open_[i] - 1          # open(i) -> open(i+1)
        eq = eq * (1 + pos * r)
        # drift of exposure with price move
        if eq > 0:
            btc_val = pos * (1 + r)
            pos = btc_val / (btc_val + (1 - pos)) if pos > 0 else 0.0
        curve.append(eq)
        expo.append(pos)

    curve = pd.Series(curve, index=idx[ii[1:]])
    rets = curve.pct_change().fillna(curve.iloc[0] / start_equity - 1)
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    dd = curve / curve.cummax() - 1
    yearly = curve.resample("YE").last().pct_change()
    yearly.iloc[0] = curve.resample("YE").last().iloc[0] / start_equity - 1
    return {
        "final": curve.iloc[-1],
        "total_ret": curve.iloc[-1] / start_equity - 1,
        "cagr": (curve.iloc[-1] / start_equity) ** (1 / years) - 1 if years > 0 else np.nan,
        "max_dd": dd.min(),
        "sharpe": rets.mean() / rets.std() * np.sqrt(365) if rets.std() > 0 else np.nan,
        "trades": trades,
        "trades_per_yr": trades / years,
        "time_in_mkt": float(np.mean(np.array(expo) > 0.01)),
        "worst_month": curve.resample("ME").last().pct_change().min(),
        "pct_months_up": float((curve.resample("ME").last().pct_change().dropna() > 0).mean()),
        "curve": curve,
        "yearly": yearly,
    }


def param_grid():
    g = {"buy_and_hold": [{}]}
    g["sma_filter"] = [{"n": n} for n in (50, 100, 150, 200)]
    g["ema_cross"] = [{"fast": f, "slow": s} for f, s in ((10, 50), (20, 100), (50, 200))]
    g["donchian"] = [{"entry": e, "exit": x} for e, x in ((20, 10), (50, 20), (100, 50))]
    g["tsmom"] = [{"lookback": n} for n in (30, 90, 180)]
    g["rsi_reversion"] = [{}]
    g["trend_ensemble"] = [{}]
    g["trend_ensemble_vt"] = [{"target_ann_vol": v} for v in (0.4, 0.6)]
    g["trend_ensemble_half"] = [{}]
    return g


def fmt(r: dict) -> dict:
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items() if k not in ("curve", "yearly")}


def main(cost_base=0.0015, cost_stress=0.009):
    os.makedirs(OUT, exist_ok=True)
    df = load()
    rows = []
    curves = {}
    for name, plist in param_grid().items():
        for p in plist:
            sig = S.REGISTRY[name](df, **p)
            label = name + ("(" + ",".join(f"{k}={v}" for k, v in p.items()) + ")" if p else "")
            ins = run(df, sig, cost_base, START, IS_END)
            oos = run(df, sig, cost_base, OOS_START, None)
            oos_stress = run(df, sig, cost_stress, OOS_START, None)
            full = run(df, sig, cost_base, START, None)
            rows.append({
                "strategy": label,
                "IS_cagr": ins["cagr"], "IS_maxdd": ins["max_dd"], "IS_sharpe": ins["sharpe"],
                "OOS_cagr": oos["cagr"], "OOS_maxdd": oos["max_dd"], "OOS_sharpe": oos["sharpe"],
                "OOS_$20_to": oos["final"], "OOS_trades/yr": oos["trades_per_yr"],
                "OOS_time_in_mkt": oos["time_in_mkt"],
                "OOS_cagr_highfee": oos_stress["cagr"],
                "OOS_pct_months_up": oos["pct_months_up"],
                "OOS_worst_month": oos["worst_month"],
            })
            curves[label] = oos["curve"]
    t = pd.DataFrame(rows).set_index("strategy")
    t.to_csv(os.path.join(OUT, "summary.csv"))
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print(t.round(3).to_string())
    pd.DataFrame(curves).to_csv(os.path.join(OUT, "oos_curves.csv"))
    return df, t, curves


if __name__ == "__main__":
    main()
