"""Signal functions shared by the backtester AND the live/paper bot.

Every function takes a daily OHLC DataFrame (index = UTC date, columns
open/high/low/close) and returns a Series of target exposure in [0, 1]
for each day, computed ONLY from information available at that day's close.
The engine applies the exposure from the NEXT day's open, so there is no
look-ahead.

Spot-only, long-or-cash. No shorting, no leverage: on a $20 account those
are the fastest ways to hit zero.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def buy_and_hold(df: pd.DataFrame) -> pd.Series:
    return pd.Series(1.0, index=df.index)


def sma_filter(df: pd.DataFrame, n: int = 150) -> pd.Series:
    """Long when close is above its n-day simple moving average."""
    sma = df["close"].rolling(n).mean()
    return (df["close"] > sma).astype(float).where(sma.notna(), 0.0)


def ema_cross(df: pd.DataFrame, fast: int = 20, slow: int = 100) -> pd.Series:
    f = df["close"].ewm(span=fast, adjust=False).mean()
    s = df["close"].ewm(span=slow, adjust=False).mean()
    out = (f > s).astype(float)
    out.iloc[:slow] = 0.0
    return out


def donchian(df: pd.DataFrame, entry: int = 50, exit: int = 20) -> pd.Series:
    """Turtle-style breakout: buy a new `entry`-day closing high,
    sell a new `exit`-day closing low."""
    hi = df["close"].rolling(entry).max().shift(1)
    lo = df["close"].rolling(exit).min().shift(1)
    pos = np.zeros(len(df))
    state = 0.0
    c = df["close"].to_numpy()
    for i in range(len(df)):
        if np.isnan(hi.iloc[i]) or np.isnan(lo.iloc[i]):
            pos[i] = 0.0
            continue
        if state == 0.0 and c[i] > hi.iloc[i]:
            state = 1.0
        elif state == 1.0 and c[i] < lo.iloc[i]:
            state = 0.0
        pos[i] = state
    return pd.Series(pos, index=df.index)


def tsmom(df: pd.DataFrame, lookback: int = 90) -> pd.Series:
    """Time-series momentum: long if the trailing `lookback`-day return > 0."""
    r = df["close"].pct_change(lookback)
    return (r > 0).astype(float).where(r.notna(), 0.0)


def rsi_reversion(df: pd.DataFrame, n: int = 2, buy_below: float = 10, sell_above: float = 70,
                  trend: int = 200) -> pd.Series:
    """Short-term mean reversion (Connors RSI-2 style), only above the trend MA."""
    d = df["close"].diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    sma = df["close"].rolling(trend).mean()
    pos = np.zeros(len(df))
    state = 0.0
    for i in range(len(df)):
        if np.isnan(sma.iloc[i]) or np.isnan(rsi.iloc[i]):
            continue
        if state == 0.0 and rsi.iloc[i] < buy_below and df["close"].iloc[i] > sma.iloc[i]:
            state = 1.0
        elif state == 1.0 and (rsi.iloc[i] > sell_above or df["close"].iloc[i] < sma.iloc[i]):
            state = 0.0
        pos[i] = state
    return pd.Series(pos, index=df.index)


def trend_ensemble(df: pd.DataFrame, lookbacks=(50, 100, 150, 200)) -> pd.Series:
    """Average of several SMA filters -> exposure in {0, .25, .5, .75, 1}.
    Deliberately parameter-light: no single lookback has to be 'right'."""
    sigs = [sma_filter(df, n) for n in lookbacks]
    return sum(sigs) / len(sigs)


def vol_target(base: pd.Series, df: pd.DataFrame, target_ann_vol: float = 0.50,
               vol_window: int = 30) -> pd.Series:
    """Scale a 0..1 signal down when realised volatility is above target (never above 1)."""
    rv = np.log(df["close"]).diff().rolling(vol_window).std() * np.sqrt(365)
    scale = (target_ann_vol / rv).clip(upper=1.0).fillna(0.0)
    return base * scale


def trend_ensemble_vt(df: pd.DataFrame, target_ann_vol: float = 0.50) -> pd.Series:
    return vol_target(trend_ensemble(df), df, target_ann_vol)


# Registry used by the bot config ("strategy: trend_ensemble")
REGISTRY = {
    "buy_and_hold": buy_and_hold,
    "sma_filter": sma_filter,
    "ema_cross": ema_cross,
    "donchian": donchian,
    "tsmom": tsmom,
    "rsi_reversion": rsi_reversion,
    "trend_ensemble": trend_ensemble,
    "trend_ensemble_vt": trend_ensemble_vt,
}


def trend_ensemble_half(df: pd.DataFrame, lookbacks=(50, 100, 150, 200)) -> pd.Series:
    """Ensemble quantised DOWN to {0, 0.5, 1}. Exists because a A$20 account
    can't place 25%-of-equity (~A$5) orders: most exchanges' minimum is ~US$5+."""
    return np.floor(trend_ensemble(df, lookbacks) * 2) / 2


REGISTRY["trend_ensemble_half"] = trend_ensemble_half


MIN_REBAL = 0.20  # ignore exposure changes smaller than 20% of equity


def should_trade(target: float, current: float, min_rebal: float = MIN_REBAL) -> bool:
    """Single source of truth for 'is this change worth paying fees for?'.
    Used by both backtest.py and bot.py so they behave identically."""
    if abs(target - current) < 1e-9:
        return False
    if target == 0.0 and current > 0.0:
        return True          # always allow a full exit
    if target == 1.0 and current < 1.0:
        return True          # always allow going fully in
    return abs(target - current) >= min_rebal
