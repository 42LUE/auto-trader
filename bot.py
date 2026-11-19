"""Kaileb's $20 auto-trader: two paper/live accounts side by side.

  claude : trades for Kaileb. Executes ONLY decisions Claude proposed and Kaileb
           approved (merged PR -> decisions/*.json on main). See execute.py.
  rule   : benchmark. trend_ensemble_half runs automatically, paper only, so we can
           see whether Claude's calls beat a dumb rule.

Both share the same hard risk rails (cannot be overridden by Claude):
  - spot BTC only, long or cash, exposure in {0, 0.5, 1}
  - max_capital cap, drawdown pause (needs Kaileb), kill switch (needs Kaileb)

Commands
  python bot.py run    --account rule      daily rule cycle
  python bot.py mark   --account claude    daily mark-to-market + risk checks, no new trades
  python bot.py status --account claude
  add --offline data/btc-usd-daily.csv for a no-network dry run
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
LIVE_PASSPHRASE = "I_APPROVE_REAL_MONEY"
ACCOUNTS = ("claude", "rule")


class Paths:
    def __init__(self, account: str, root: str | None = None):
        self.dir = os.path.join(root or os.path.join(HERE, "state"), account)
        self.state = os.path.join(self.dir, "state.json")
        self.trades = os.path.join(self.dir, "trades.csv")
        self.equity = os.path.join(self.dir, "equity.csv")
        self.shutdown = os.path.join(self.dir, "SHUTDOWN")
        self.paused = os.path.join(self.dir, "PAUSED")
        self.executions = os.path.join(self.dir, "executions.json")


# ----------------------------------------------------------------------------- utils
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc():%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def load_cfg(path: str | None = None) -> dict:
    with open(path or os.path.join(HERE, "config.yaml")) as f:
        return yaml.safe_load(f)


def acct_mode(cfg: dict, account: str) -> str:
    if account == "rule":
        return "paper"                      # benchmark is never live
    return cfg["accounts"]["claude"]["mode"]


def load_state(cfg: dict, P: Paths, account: str) -> dict:
    if os.path.exists(P.state):
        with open(P.state) as f:
            return json.load(f)
    return {
        "account": account,
        "mode": acct_mode(cfg, account),
        "started": now_utc().isoformat(),
        "start_capital": float(cfg["start_capital"]),
        "cash": float(cfg["start_capital"]),
        "base": 0.0,
        "fees_paid": 0.0,
        "first_price": None,
        "last_run_candle": None,
        "runs": 0,
    }


def save_state(P: Paths, st: dict) -> None:
    os.makedirs(P.dir, exist_ok=True)
    tmp = P.state + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, P.state)


def append_csv(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def notify(cfg: dict, title: str, msg: str) -> None:
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if not topic:
        return
    try:
        import urllib.request
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(),
                                     headers={"Title": title.encode("ascii", "ignore").decode()})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"notify failed: {e}")


# ----------------------------------------------------------------------------- market data
def make_exchange(cfg: dict, live: bool):
    import ccxt
    params = {"enableRateLimit": True}
    if live:
        params["apiKey"] = os.environ["TRADER_API_KEY"]
        params["secret"] = os.environ["TRADER_API_SECRET"]
    ex = getattr(ccxt, cfg["exchange"])(params)
    ex.load_markets()
    return ex


def closed_daily_candles(ex, symbol: str, limit: int = 720) -> pd.DataFrame:
    raw = ex.fetch_ohlcv(symbol, timeframe="1d", limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.set_index("date").drop(columns="ts")
    cutoff = pd.Timestamp(now_utc().replace(tzinfo=None)).normalize()
    return df[df.index < cutoff]


def offline_candles(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    return df[["open", "high", "low", "close"]].astype(float).iloc[-720:]


class Market:
    """Fetch once, share between accounts in one process."""

    def __init__(self, cfg: dict, offline: str | None = None, live: bool = False):
        self.cfg = cfg
        if offline:
            self.ex = None
            self.df = offline_candles(offline)
            self.price = float(self.df["close"].iloc[-1])
        else:
            self.ex = make_exchange(cfg, live)
            self.df = closed_daily_candles(self.ex, cfg["symbol"])
            self.price = float(self.ex.fetch_ticker(cfg["symbol"])["last"])
        self.candle = str(self.df.index[-1].date())


# ----------------------------------------------------------------------------- brokers
class PaperBroker:
    def __init__(self, st: dict, cost: float):
        self.st, self.cost = st, cost

    def balances(self, price: float):
        return self.st["cash"], self.st["base"]

    def trade(self, side: str, quote_amount: float, price: float) -> dict:
        fee = quote_amount * self.cost
        if side == "buy":
            quote_amount = min(quote_amount, self.st["cash"])
            fee = quote_amount * self.cost
            qty = (quote_amount - fee) / price
            self.st["cash"] -= quote_amount
            self.st["base"] += qty
        else:
            qty = min(quote_amount / price, self.st["base"])
            fee = qty * price * self.cost
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
        cap = float(self.cfg["max_capital"])
        cash = max(0.0, min(cash, cap - base * price))
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
def _gate_live(cfg: dict) -> None:
    c = cfg["accounts"]["claude"]
    if not c.get("live_approved") or os.environ.get("TRADER_LIVE_CONFIRM") != LIVE_PASSPHRASE:
        log("LIVE mode blocked: needs accounts.claude.live_approved: true AND "
            f"TRADER_LIVE_CONFIRM={LIVE_PASSPHRASE}. Nothing done.")
        sys.exit(2)


def cycle(cfg: dict, account: str, mkt: Market, target: float | None, reason: str,
          P: Paths | None = None, force: bool = False) -> dict:
    """One accounting cycle for `account`.
    target=None  -> mark-to-market + risk checks only (no discretionary trade).
    Returns a dict describing what happened."""
    P = P or Paths(account)
    if os.path.exists(P.shutdown):
        log(f"[{account}] SHUT DOWN (kill switch). Kaileb must delete {P.shutdown} to restart.")
        return {"status": "shutdown"}
    if os.path.exists(P.paused):
        log(f"[{account}] PAUSED (drawdown limit). Kaileb must delete {P.paused} to resume.")
        return {"status": "paused"}

    mode = acct_mode(cfg, account)
    live = mode == "live"
    if live:
        _gate_live(cfg)

    st = load_state(cfg, P, account)
    if st["mode"] != mode:
        log(f"[{account}] state is {st['mode']} but config says {mode}. Move state/{account} aside first.")
        sys.exit(2)
    if not force and st["last_run_candle"] == mkt.candle:
        log(f"[{account}] already ran on the {mkt.candle} candle.")
        return {"status": "noop"}

    price = mkt.price
    broker = LiveBroker(mkt.ex, cfg, st) if live else PaperBroker(st, cfg["paper_cost_per_side"])
    cash, base = broker.balances(price)
    equity = cash + base * price
    current = (base * price / equity) if equity > 0 else 0.0
    if st["first_price"] is None:
        st["first_price"] = price
    log(f"[{account}] {mode.upper()} {cfg['symbol']} candle={mkt.candle} price={price:,.2f} "
        f"equity={equity:.2f} exposure={current:.2f} target={target}")

    floor = float(cfg.get("kill_switch_equity", 0.0))
    if equity <= max(floor, 0.01):
        _kill(cfg, P, broker, st, price, equity, base, account, "equity at/below kill floor")
        return {"status": "killed"}

    st["peak_equity"] = max(st.get("peak_equity", st["start_capital"]), equity)
    dd_limit = cfg.get("pause_drawdown")
    if dd_limit and equity < st["peak_equity"] * (1 - dd_limit):
        _pause(cfg, P, broker, st, price, equity, base, account,
               f"drawdown {equity / st['peak_equity'] - 1:.0%} from peak ${st['peak_equity']:.2f} "
               f"exceeds {dd_limit:.0%}")
        return {"status": "paused"}

    action, fill = "hold", None
    if target is not None:
        target = float(min(max(target, 0.0), 1.0))
        if S.should_trade(target, current, cfg.get("min_rebalance", S.MIN_REBAL)):
            delta = (target - current) * equity
            side = "buy" if delta > 0 else "sell"
            quote = min(abs(delta), cash) if side == "buy" else abs(delta)
            if side == "sell" and target == 0.0:
                quote = base * price
            try:
                fill = broker.trade(side, quote, price)
                action = side
                append_csv(P.trades, {"time": now_utc().isoformat(), "mode": mode, "side": side,
                                      "qty": round(fill["qty"], 8), "price": round(fill["price"], 2),
                                      "quote": round(quote, 4), "fee": round(fill["fee"], 4),
                                      "target": target, "prev_exposure": round(current, 3),
                                      "reason": reason})
                log(f"[{account}] {side.upper()} {fill['qty']:.8f} @ {fill['price']:,.2f} fee={fill['fee']:.4f}")
                notify(cfg, f"{account} {side}", f"{account}: {side} {fill['qty']:.6f} BTC @ "
                       f"{fill['price']:,.0f} -> {target:.0%} BTC ({reason})")
            except Exception as e:
                action = f"skip: {e}"
                log(f"[{account}] trade skipped: {e}")

    cash, base = broker.balances(price)
    equity = cash + base * price
    append_csv(P.equity, {"time": now_utc().isoformat(), "candle": mkt.candle, "price": round(price, 2),
                          "cash": round(cash, 4), "btc": round(base, 8), "equity": round(equity, 4),
                          "buy_hold_equity": round(st["start_capital"] * price / st["first_price"], 4),
                          "target": "" if target is None else target, "action": action,
                          "reason": reason})
    if target is None or not force:
        st["last_run_candle"] = mkt.candle
    st["runs"] += 1
    save_state(P, st)
    if equity <= max(floor, 0.01):
        _kill(cfg, P, broker, st, price, equity, base, account, "equity at/below kill floor after trade")
        return {"status": "killed"}
    return {"status": "ok", "action": action, "equity": equity, "fill": fill}


def _flatten(broker, base, price, account):
    if base * price > 0:
        try:
            broker.trade("sell", base * price, price)
        except Exception as e:
            log(f"[{account}] flatten failed: {e}")


def _pause(cfg, P, broker, st, price, equity, base, account, why):
    log(f"[{account}] PAUSE: {why}. Going to cash.")
    _flatten(broker, base, price, account)
    os.makedirs(P.dir, exist_ok=True)
    with open(P.paused, "w") as f:
        f.write(f"{now_utc().isoformat()} {why}\n")
    save_state(P, st)
    notify(cfg, f"{account} PAUSED - needs your approval", why)


def _kill(cfg, P, broker, st, price, equity, base, account, why):
    log(f"[{account}] KILL SWITCH: {why} (equity {equity:.2f}).")
    _flatten(broker, base, price, account)
    os.makedirs(P.dir, exist_ok=True)
    with open(P.shutdown, "w") as f:
        f.write(f"{now_utc().isoformat()} {why}; equity {equity:.2f}\n")
    save_state(P, st)
    notify(cfg, f"{account} SHUT DOWN", f"{why}. Equity {equity:.2f}.")


def rule_target(cfg: dict, df: pd.DataFrame) -> float:
    r = cfg["accounts"]["rule"]
    return float(S.REGISTRY[r["strategy"]](df, **(r.get("strategy_params") or {})).iloc[-1])


def status(cfg: dict, account: str) -> None:
    P = Paths(account)
    if not os.path.exists(P.equity):
        print(f"[{account}] no runs yet.")
        return
    eq = pd.read_csv(P.equity)
    st = load_state(cfg, P, account)
    last = eq.iloc[-1]
    n = len(pd.read_csv(P.trades)) if os.path.exists(P.trades) else 0
    pnl = last["equity"] - st["start_capital"]
    flag = "SHUT DOWN" if os.path.exists(P.shutdown) else "PAUSED" if os.path.exists(P.paused) else "running"
    print(f"[{account}] {st['mode']} · {flag} · equity ${last['equity']:.2f} ({pnl:+.2f}) · "
          f"buy&hold ${last['buy_hold_equity']:.2f} · trades {n} · fees ${st['fees_paid']:.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "mark", "status"])
    ap.add_argument("--account", choices=ACCOUNTS, required=True)
    ap.add_argument("--config")
    ap.add_argument("--offline")
    a = ap.parse_args()
    c = load_cfg(a.config)
    if a.cmd == "status":
        status(c, a.account)
        sys.exit(0)
    m = Market(c, a.offline, live=(acct_mode(c, a.account) == "live"))
    if a.cmd == "run":
        if a.account != "rule":
            sys.exit("Only the rule account runs automatically. Claude's account trades via execute.py.")
        cycle(c, "rule", m, rule_target(c, m.df), f"rule {c['accounts']['rule']['strategy']}")
    else:
        cycle(c, a.account, m, None, "daily mark")
