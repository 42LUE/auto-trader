"""Kaileb's $20 auto-trader.

Runs once per day (after the UTC daily candle closes). Each run:
  1. fetch closed daily candles from the exchange (public data, no keys needed)
  2. compute target BTC exposure with the SAME strategy code the backtest used
  3. rebalance if strategies.should_trade() says the change is worth the fees
  4. log everything; enforce the kill switch

Modes
  paper : simulated fills against live prices, with fees + slippage charged.
  live  : real orders. Refuses to run unless BOTH config `live_approved: true`
          AND env var TRADER_LIVE_CONFIRM == "I_APPROVE_REAL_MONEY". Never manages
          more than `max_capital` regardless of account balance.

Commands
  python bot.py run                 one daily cycle
  python bot.py status              scoreboard
  python bot.py run --offline data/btc-usd-daily.csv   dry run without network
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import yaml

import strategies as S

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "state")
STATE = os.path.join(STATE_DIR, "state.json")
TRADES = os.path.join(STATE_DIR, "trades.csv")
EQUITY = os.path.join(STATE_DIR, "equity.csv")
SHUTDOWN = os.path.join(STATE_DIR, "SHUTDOWN")
PAUSED = os.path.join(STATE_DIR, "PAUSED")
LIVE_PASSPHRASE = "I_APPROVE_REAL_MONEY"


# ----------------------------------------------------------------------------- utils
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc():%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def load_cfg(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_state(cfg: dict) -> dict:
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {
        "mode": cfg["mode"],
        "started": now_utc().isoformat(),
        "start_capital": float(cfg["start_capital"]),
        "cash": float(cfg["start_capital"]),   # paper only
        "base": 0.0,                            # paper only (BTC held)
        "fees_paid": 0.0,
        "first_price": None,                    # for buy-and-hold comparison
        "last_run_candle": None,
        "runs": 0,
    }


def save_state(st: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE)


def append_csv(path: str, row: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def notify(cfg: dict, title: str, msg: str) -> None:
    """Optional phone push via ntfy.sh (free). Set ntfy_topic in config or NTFY_TOPIC env."""
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if not topic:
        return
    try:
        import urllib.request
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(),
                                     headers={"Title": title})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # never let notifications break trading
        log(f"notify failed: {e}")


# ----------------------------------------------------------------------------- market data
def make_exchange(cfg: dict, live: bool):
    import ccxt
    klass = getattr(ccxt, cfg["exchange"])
    params = {"enableRateLimit": True}
    if live:
        params["apiKey"] = os.environ["TRADER_API_KEY"]
        params["secret"] = os.environ["TRADER_API_SECRET"]
    ex = klass(params)
    ex.load_markets()
    return ex


def closed_daily_candles(ex, symbol: str, limit: int = 400) -> pd.DataFrame:
    raw = ex.fetch_ohlcv(symbol, timeframe="1d", limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.set_index("date").drop(columns="ts")
    # drop the still-forming candle
    cutoff = pd.Timestamp(now_utc().replace(tzinfo=None)).normalize()
    return df[df.index < cutoff]


def offline_candles(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    return df[["open", "high", "low", "close"]].astype(float).iloc[-400:]


# ----------------------------------------------------------------------------- brokers
class PaperBroker:
    def __init__(self, st: dict, cost: float):
        self.st, self.cost = st, cost

    def balances(self, price: float):
        return self.st["cash"], self.st["base"]

    def trade(self, side: str, quote_amount: float, price: float) -> dict:
        fee = quote_amount * self.cost
        if side == "buy":
            qty = (quote_amount - fee) / price
            self.st["cash"] -= quote_amount
            self.st["base"] += qty
        else:
            qty = min(quote_amount / price, self.st["base"])
            self.st["base"] -= qty
            self.st["cash"] += qty * price - fee
        self.st["fees_paid"] += fee
        return {"qty": qty, "price": price, "fee": fee}


class LiveBroker:
    def __init__(self, ex, cfg: dict, st: dict):
        self.ex, self.cfg, self.st = ex, cfg, st
        self.symbol = cfg["symbol"]
        self.base_ccy, self.quote_ccy = self.symbol.split("/")
        self.market = ex.market(self.symbol)

    def balances(self, price: float):
        bal = self.ex.fetch_balance()
        cash = float(bal.get(self.quote_ccy, {}).get("free") or 0.0)
        base = float(bal.get(self.base_ccy, {}).get("free") or 0.0)
        # never manage more than max_capital, whatever is in the account
        cap = float(self.cfg["max_capital"])
        base_val = base * price
        cash = max(0.0, min(cash, cap - base_val))
        return cash, base

    def trade(self, side: str, quote_amount: float, price: float) -> dict:
        amount = float(self.ex.amount_to_precision(self.symbol, quote_amount / price))
        lim = self.market.get("limits", {})
        min_amt = (lim.get("amount") or {}).get("min") or 0
        min_cost = (lim.get("cost") or {}).get("min") or 0
        if amount < min_amt or amount * price < min_cost:
            raise ValueError(f"order below exchange minimum (amount {amount}, min {min_amt}, "
                             f"cost {amount * price:.2f}, min cost {min_cost})")
        order = self.ex.create_order(self.symbol, "market", side, amount)
        time.sleep(2)
        try:
            order = self.ex.fetch_order(order["id"], self.symbol)
        except Exception:
            pass
        fee = sum(float(f.get("cost") or 0) for f in (order.get("fees") or [])) or \
            float((order.get("fee") or {}).get("cost") or 0)
        self.st["fees_paid"] += fee
        return {"qty": float(order.get("filled") or amount),
                "price": float(order.get("average") or price), "fee": fee}


# ----------------------------------------------------------------------------- core
def run(cfg: dict, offline: str | None = None) -> None:
    if os.path.exists(SHUTDOWN):
        log("SHUTDOWN file present - kill switch was triggered. Delete state/SHUTDOWN to restart "
            "(that's a decision for Kaileb, not the bot).")
        return

    if os.path.exists(PAUSED):
        log("PAUSED (drawdown limit hit). Waiting for Kaileb to delete state/PAUSED.")
        return

    live = cfg["mode"] == "live"
    if live:
        if not cfg.get("live_approved") or os.environ.get("TRADER_LIVE_CONFIRM") != LIVE_PASSPHRASE:
            log("LIVE mode blocked: needs live_approved: true in config AND "
                f"TRADER_LIVE_CONFIRM={LIVE_PASSPHRASE}. Nothing done.")
            sys.exit(2)

    st = load_state(cfg)
    if st["mode"] != cfg["mode"]:
        log(f"state file is from {st['mode']} mode but config says {cfg['mode']}. "
            "Move state/ aside before switching modes. Nothing done.")
        sys.exit(2)

    if offline:
        df = offline_candles(offline)
        price = float(df["close"].iloc[-1])
        ex = None
    else:
        ex = make_exchange(cfg, live)
        df = closed_daily_candles(ex, cfg["symbol"])
        price = float(ex.fetch_ticker(cfg["symbol"])["last"])

    last_candle = str(df.index[-1].date())
    if st["last_run_candle"] == last_candle and not offline:
        log(f"already acted on the {last_candle} candle - nothing to do.")
        return

    strat = S.REGISTRY[cfg["strategy"]]
    target = float(strat(df, **(cfg.get("strategy_params") or {})).iloc[-1])
    close = float(df["close"].iloc[-1])
    st["last_signal"] = {"candle": str(df.index[-1].date()), "close": close, "target": target,
                         "mas": {n: round(float(df["close"].rolling(n).mean().iloc[-1]), 2)
                                 for n in (50, 100, 150, 200)}}

    broker = LiveBroker(ex, cfg, st) if live else PaperBroker(st, cfg["paper_cost_per_side"])
    cash, base = broker.balances(price)
    equity = cash + base * price
    current = (base * price / equity) if equity > 0 else 0.0
    if st["first_price"] is None:
        st["first_price"] = price

    log(f"{cfg['mode'].upper()} {cfg['symbol']} candle={last_candle} price={price:,.2f} "
        f"equity={equity:.2f} exposure={current:.2f} target={target:.2f}")

    # ---- kill switch (checked before AND after trading)
    floor = float(cfg.get("kill_switch_equity", 0.0))
    if equity <= max(floor, 0.01):
        kill(cfg, broker, st, price, equity, base, "equity at/below kill floor before trading")
        return

    st["peak_equity"] = max(st.get("peak_equity", st["start_capital"]), equity)
    dd_limit = cfg.get("pause_drawdown")
    if dd_limit and equity < st["peak_equity"] * (1 - dd_limit):
        pause(cfg, broker, st, price, equity, base,
              f"drawdown {equity / st['peak_equity'] - 1:.0%} from peak ${st['peak_equity']:.2f} "
              f"exceeds pause limit {dd_limit:.0%}")
        return

    action = "hold"
    if S.should_trade(target, current, cfg.get("min_rebalance", S.MIN_REBAL)):
        delta_val = (target - current) * equity
        side = "buy" if delta_val > 0 else "sell"
        quote_amt = min(abs(delta_val), cash) if side == "buy" else abs(delta_val)
        if side == "sell" and target == 0.0:
            quote_amt = base * price
        try:
            fill = broker.trade(side, quote_amt, price)
            action = side
            append_csv(TRADES, {"time": now_utc().isoformat(), "mode": cfg["mode"], "side": side,
                                "qty": round(fill["qty"], 8), "price": round(fill["price"], 2),
                                "quote": round(quote_amt, 4), "fee": round(fill["fee"], 4),
                                "target": target, "prev_exposure": round(current, 3)})
            log(f"{side.upper()} {fill['qty']:.8f} @ {fill['price']:,.2f} fee={fill['fee']:.4f}")
            notify(cfg, f"Trader {side}", f"{side} {fill['qty']:.6f} BTC @ {fill['price']:,.0f}; "
                   f"target exposure {target:.0%}")
        except Exception as e:
            action = f"skip ({e})"
            log(f"trade skipped: {e}")

    cash, base = broker.balances(price)
    equity = cash + base * price
    bh = st["start_capital"] * price / st["first_price"]
    append_csv(EQUITY, {"time": now_utc().isoformat(), "candle": last_candle, "price": round(price, 2),
                        "cash": round(cash, 4), "btc": round(base, 8), "equity": round(equity, 4),
                        "buy_hold_equity": round(bh, 4), "target": target, "action": action})
    st["last_run_candle"] = last_candle
    st["runs"] += 1
    save_state(st)

    if equity <= max(floor, 0.01):
        kill(cfg, broker, st, price, equity, base, "equity at/below kill floor after trading")


def pause(cfg, broker, st, price, equity, base, why):
    """Softer than the kill switch: go to cash and wait for Kaileb's approval."""
    log(f"PAUSE: {why}. Going to cash; delete state/PAUSED to resume.")
    if base * price > 0:
        try:
            broker.trade("sell", base * price, price)
        except Exception as e:
            log(f"flatten failed: {e}")
    with open(PAUSED, "w") as f:
        f.write(f"{now_utc().isoformat()} {why}\n")
    save_state(st)
    notify(cfg, "Trader PAUSED - needs your approval", why)


def kill(cfg, broker, st, price, equity, base, why):
    log(f"KILL SWITCH: {why} (equity {equity:.2f}). Flattening and shutting down.")
    if base * price > 0:
        try:
            broker.trade("sell", base * price, price)
        except Exception as e:
            log(f"flatten failed: {e}")
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SHUTDOWN, "w") as f:
        f.write(f"{now_utc().isoformat()} {why}; equity {equity:.2f}\n")
    save_state(st)
    notify(cfg, "Trader SHUT DOWN", f"{why}. Equity {equity:.2f}.")


def status(cfg: dict) -> None:
    if not os.path.exists(EQUITY):
        print("No runs yet.")
        return
    eq = pd.read_csv(EQUITY)
    st = load_state(cfg)
    last = eq.iloc[-1]
    start = st["start_capital"]
    n_trades = len(pd.read_csv(TRADES)) if os.path.exists(TRADES) else 0
    pnl = last["equity"] - start
    print(f"Mode:            {cfg['mode']}  ({cfg['exchange']} {cfg['symbol']}, strategy {cfg['strategy']})")
    print(f"Started:         {st['started'][:10]} with ${start:.2f}")
    print(f"Equity now:      ${last['equity']:.2f}  ({pnl:+.2f}, {pnl / start:+.1%})")
    print(f"Buy & hold BTC:  ${last['buy_hold_equity']:.2f}")
    print(f"BTC exposure:    {last['btc'] * last['price'] / max(last['equity'], 1e-9):.0%}")
    print(f"Trades / fees:   {n_trades} / ${st['fees_paid']:.4f}")
    print(f"Days run:        {st['runs']}")
    print(f"Kill switch:     {'TRIGGERED' if os.path.exists(SHUTDOWN) else 'armed'} "
          f"(floor ${cfg.get('kill_switch_equity', 0)})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "status"])
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--offline", help="CSV of daily candles for a no-network dry run")
    a = ap.parse_args()
    c = load_cfg(a.config)
    run(c, a.offline) if a.cmd == "run" else status(c)
