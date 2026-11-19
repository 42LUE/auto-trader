"""Replays the bot day-by-day over 2024-01-01..now (offline) and compares its
paper equity with backtest.run() over the same window. They should agree closely
(bot fills at the signal-day close, backtest at next-day open)."""
import os, sys, tempfile, shutil, io, contextlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import bot, backtest as B, strategies as S

full = B.load()
tmp = tempfile.mkdtemp()

bot.STATE_DIR = tmp
bot.STATE = os.path.join(tmp, "state.json"); bot.TRADES = os.path.join(tmp, "trades.csv")
bot.EQUITY = os.path.join(tmp, "equity.csv"); bot.SHUTDOWN = os.path.join(tmp, "SHUTDOWN")
cfg = bot.load_cfg("config.yaml"); cfg["paper_cost_per_side"] = 0.0015
start = "2024-01-01"
days = full.loc[start:].index
for d in days:
    frame = full.loc[:d].reset_index()
    p = os.path.join(tmp, "f.csv"); frame.to_csv(p, index=False)
    with contextlib.redirect_stdout(io.StringIO()):
        bot.run(cfg, offline=p)
eq = pd.read_csv(bot.EQUITY)
tr = pd.read_csv(bot.TRADES)
bt = B.run(full, S.trend_ensemble_half(full), 0.0015, start)
print(f"bot replay   : $20 -> ${eq['equity'].iloc[-1]:.2f}  trades={len(tr)}  fees=${pd.read_json(bot.STATE, typ='series')['fees_paid']:.3f}")
print(f"backtest     : $20 -> ${bt['final']:.2f}  trades={bt['trades']}")
print(f"buy & hold   : $20 -> ${eq['buy_hold_equity'].iloc[-1]:.2f}")
print(tr.tail(6).to_string())
shutil.rmtree(tmp)
