# $20 Auto-Trader

A daily BTC trend-following bot. Long or cash, no leverage, no shorting.
Starts in **paper mode**. Real money needs Kaileb's explicit approval (two separate switches).

## What it does
Once a day, after the UTC daily candle closes (~11am Sydney):
1. Pulls closed daily BTC candles from the exchange (public data, no API key needed for paper).
2. Checks price against its 50/100/150/200-day moving averages.
   - above 3–4 of them → 100% BTC
   - above 2 → 50% BTC
   - above 0–1 → 100% cash
3. Rebalances only if the change is ≥20% of equity (fees matter at $20).
4. Logs to `state/` (`equity.csv`, `trades.csv`, `state.json`) and runs the kill switch.

The bot and the backtest import the same `strategies.py`, and `tests/replay_parity.py`
replays the bot day-by-day to prove it trades identically to the backtest.

## Files
| file | purpose |
|---|---|
| `strategies.py` | all signal logic + the shared rebalance rule |
| `backtest.py` | fee-aware backtest, in-sample 2014–2021 / out-of-sample 2022–now |
| `bot.py` | the trader (`run`, `status`) |
| `summary.py` | daily / weekly reports + phone push |
| `config.yaml` | mode, exchange, pair, kill switch, fees |
| `.github/workflows/daily.yml` | free daily scheduler on GitHub Actions |
| `run_daily.bat` / `run_daily.sh` | or schedule it on your own PC |
| `data/btc-usd-daily.csv` | Bitstamp daily history used for the backtest |

## Phase 1: paper trading (costs $0)
**Option A, GitHub Actions (runs while your PC is off):**
1. Make a new **private** GitHub repo and push this folder to it.
2. Repo → Actions → enable workflows → "daily-trader" → *Run workflow* once to test.
3. Each day it commits `state/` back, so the ledger history stays in the repo.
4. Optional: install the free **ntfy** app, pick a random topic name, and add it as repo secret `NTFY_TOPIC` to get trade alerts on your Pixel.

**Option B, your PC:**
```
pip install -r requirements.txt
python bot.py run        # one cycle
python bot.py status     # scoreboard
```
Then schedule `run_daily.bat` in Windows Task Scheduler (daily, 11:30am).

Check `python bot.py status` (or `state/equity.csv` on GitHub) whenever you like.

## Phase 2: live with A$20. Needs Kaileb's approval.
1. Pick an exchange (see report: Binance AU 0.10% vs Kraken AU 0.80% taker at tier 1).
2. Deposit A$20 via PayID.
3. Create an API key with **trade permission only, withdrawals OFF**, IP-restricted if possible.
4. In `config.yaml`: set `mode: live`, `live_approved: true`, the exchange and symbol, and move the old `state/` folder somewhere else.
5. Set env/secrets: `TRADER_API_KEY`, `TRADER_API_SECRET`, `TRADER_LIVE_CONFIRM=I_APPROVE_REAL_MONEY`.
   - Binance blocks US IPs, so run Binance from your PC (Option B), not GitHub Actions.
6. `max_capital: 20` means the bot never manages more than $20, even if the account holds more.

## Summaries
- **Daily** (every run): `reports/daily/YYYY-MM-DD.md` + phone push if `NTFY_TOPIC` is set.
- **Weekly** (Sundays): `reports/weekly/YYYY-Www.md` with this week vs buy & hold, drawdown,
  fees, and automatic "what could I be wrong about?" checks against the backtest.
- `python summary.py daily|weekly` regenerates them by hand.

## Approvals
- Changing anything (strategy, risk limits, going live) = a pull request. Merging it is your approval.
  The bot only runs what's on `main`.
- **Drawdown pause:** if equity falls `pause_drawdown` (30%) from its peak, the bot goes to cash and
  writes `state/PAUSED`. Deleting that file is how you approve a restart.

## Kill switch
If equity ≤ `kill_switch_equity` (your rule: $0), it sells everything, writes `state/SHUTDOWN`,
and refuses to run again until you delete that file yourself.

## Re-running the research
```
python backtest.py              # summary table -> results/summary.csv
python tests/replay_parity.py   # bot vs backtest parity check
```
