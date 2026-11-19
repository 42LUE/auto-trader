"""Replays the rule account day-by-day over 2024-01-01..now (offline) through
bot.cycle() and compares with backtest.run(). Same trades = bot trades like the backtest.
(Small constant equity gap: the bot starts one day later than the backtest window.)"""
import os, sys, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import io, contextlib
import pandas as pd
import bot, backtest as B, strategies as S

full = B.load()
tmp = tempfile.mkdtemp()
P = bot.Paths("rule", root=tmp)
cfg = bot.load_cfg(); cfg["paper_cost_per_side"] = 0.0015


class M:  # minimal stand-in for bot.Market
    pass


for d in full.loc["2024-01-01":].index:
    m = M(); m.df = full.loc[:d].iloc[-720:]; m.price = float(m.df["close"].iloc[-1])
    m.candle = str(d.date()); m.ex = None
    with contextlib.redirect_stdout(io.StringIO()):
        bot.cycle(cfg, "rule", m, bot.rule_target(cfg, m.df), "rule", P=P)
eq, tr = pd.read_csv(P.equity), pd.read_csv(P.trades)
bt = B.run(full, S.trend_ensemble_half(full), 0.0015, "2024-01-01")
print(f"bot replay : $20 -> ${eq['equity'].iloc[-1]:.2f}  trades={len(tr)}")
print(f"backtest   : $20 -> ${bt['final']:.2f}  trades={bt['trades']}")
assert len(tr) == bt["trades"], "trade count mismatch"
print("PARITY OK")
shutil.rmtree(tmp)
