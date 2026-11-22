# $20 Auto-Trader: Claude trades, Kaileb approves

**Dashboard:** https://42lue.github.io/auto-trader/

## How it works
1. **~11:41am Sydney, GitHub Actions (`daily.py`)**, with backup runs at 12:41 and 2:41pm because GitHub
   often delays scheduled runs. A backup that finds the candle already processed only refreshes the snapshot.
   - Fetches BTC/AUD from Kraken and writes `data/snapshot.json`.
   - Runs the **rule benchmark** account, which trades automatically.
   - Executes any decisions you approved.
   - Marks both accounts to market, then writes reports and the dashboard data.
2. **~11:45am Sydney, Claude's daily task**
   - Reads the snapshot and its own track record, then researches the news.
   - Decides **buy / sell / hold** for *your* account (`claude`):
     - **hold:** logged, nothing for you to do.
     - **buy/sell:** opens a pull request, which shows up as a YES/NO card on the dashboard.
3. **You**
   - Tap **Yes** to open the PR, then **Merge**. The trade executes within a few minutes.
   - Tap **No** to open the PR, then **Close**. Nothing happens.
   - Ignoring it means it expires after 36h. It's also skipped if BTC has moved more than 5% since the proposal.
4. **Sundays**
   - A weekly report compares Claude, the rule and buy & hold.
   - At 12:47pm, the Claude weekly review checks the week, flags anything off, and makes at most one PR with suggestions.

## Two accounts, $20 each
| Account | Who decides | Mode |
|---|---|---|
| `claude` (yours) | Claude proposes, you approve | paper → live only when you flip it |
| `rule` (benchmark) | `trend_ensemble_half`: 50/100/150/200-day averages, automatic | always paper |

The question this answers: **does Claude's research beat a dumb rule?** The rule backtested at
roughly 20–25%/yr with a −25% worst drawdown (2022–2026 out-of-sample); see `backtest.py`.

## Hard limits (code, not Claude's judgement)
- Spot BTC only. Exposure is 0%, 50% or 100%. No leverage, no shorting.
- A buy/sell only executes if its PR was **merged by you** (`approver: 42LUE`). It must also still be fresh and within 5% of the proposed price.
- **Drawdown pause:** −30% from peak → sells to cash and writes `state/<acct>/PAUSED`. Delete that file to resume.
- **Kill switch:** equity at $0 → sells everything and writes `state/<acct>/SHUTDOWN`.
- **Going live** needs `accounts.claude.live_approved: true` *and* the secret
  `TRADER_LIVE_CONFIRM=I_APPROVE_REAL_MONEY`, plus API keys as secrets (trade-only, withdrawals off).
  `max_capital: 20`.
- Claude's rules are in `CLAUDE.md`.

## Files
| file | purpose |
|---|---|
| `daily.py` | the daily job |
| `execute.py` | runs your approved decisions (also triggered when you merge) |
| `bot.py` | accounts, brokers, risk rails |
| `strategies.py`, `backtest.py` | rule logic and the backtest |
| `market.py` | snapshot for Claude's research |
| `summary.py` | daily and weekly reports |
| `build_site.py`, `index.html` | the dashboard |
| `decisions/` | every decision Claude made, with its reasoning |
| `state/` | ledgers (`equity.csv`, `trades.csv`, `executions.json`) |
| `reports/` | daily, weekly, and Claude's weekly reviews |

## Phone alerts (optional)
Install the free **ntfy** app and subscribe to a private topic name. Then add it as the repo secret `NTFY_TOPIC`
(**Settings → Secrets and variables → Actions**).

## Local
```
pip install -r requirements.txt
python daily.py --offline data/btc-usd-daily.csv --trust-local   # dry run, no network
python tests/replay_parity.py                                    # bot == backtest check
python backtest.py                                               # strategy research
```
