# Committee trading bot

A rule-based US-stock trading bot with an honest backtester, a Claude-driven
improvement loop, and a paper-trading runner for Alpaca. No finance background
assumed; every term is explained where it appears.

## How it works, in one paragraph

Each day, for each of about 100 large US stocks, five small rules ("voters") each give a
score from -1 (bearish) to +1 (bullish). If the weighted total is high enough AND
at least 3 voters agree, the bot buys at the next morning's open. It then sells
automatically at a stop (small loss), a target (larger gain, 2x the stop), or after
10 days. The backtester replays this over history with realistic costs. The loop
script asks Claude Code to propose one change at a time, retests, and keeps only
changes that improve the result without adding risk. A separate untouched window
of data is the final exam that tells you whether the tuning was real or noise.

> **Read this first.** This is an educational research and paper-trading framework, not a money machine.
> No profitable edge has been proven. It is not financial advice. See [DISCLAIMER.md](DISCLAIMER.md).

## Honest results

What makes this repo different from most trading bots is that it reports what actually happened,
including the losses.

| Test | Result |
|---|---|
| Walk-forward, trend book (re-tuned each year, then traded on the unseen next year, 2018-2025) | 6 of 8 years profitable, median profit factor ~1.3. A thin edge |
| Momentum rotation, fair universe (stocks chosen by size in 2016) | 7 of 8 years profitable, ~19.6%/yr vs SPY ~15.5% |
| Live Alpaca paper trading, 2026-09-14 to 2026-11-22 | 48 trades, profit factor 0.44, -$1,311 realised. The kill switch tripped once. Account -1.5% while SPY fell ~5.7% |
| Rejected after testing | intraday opening-range breakout, short selling, crypto trend, inverse ETFs, earnings blackout, sector caps |

Profit factor = money won / money lost; below 1.0 loses money. Ten weeks of paper trading is far too
short to judge either way. The honest summary: excellent test harness, unproven strategy.

## Quick start

```bash
pip install -r requirements.txt
python scripts/fetch_data.py          # downloads price data (not included in the repo)
python -m pytest tests -q
python scripts/run_backtest.py
```

Price data, backtest output and paper-trading logs are generated locally into `data/` and `results/`
and are not part of the repository. The scheduled-task setup is Windows-only; everything else is plain
Python 3.10+.

## Folder map

| Path | What it is |
|---|---|
| `config/settings.json` | Dates for the train and holdout windows, costs, account size, loop rules |
| `config/params.json` | Every strategy knob. This is the only file the loop tunes |
| `config/baseline_params.json` | The deliberately dumb one-rule bot every candidate must beat |
| `config/universe.json` | The ~100 stocks traded (the original 30 are in `universe_30.json`) |
| `tradingbot/voters/*.py` | One file per voter. Add a file, add a params entry, done |
| `tradingbot/backtest.py` | The engine. Entry at next open, stop-first on same-bar hits |
| `tradingbot/costs.py` | Spread, slippage, fees. The honesty layer |
| `scripts/` | The commands below |
| `tests/` | Includes the look-ahead test that catches voters peeking at the future |
| `results/` | Everything generated: runs, rounds, `report.html`, paper logs |
| `CHANGELOG.md` | Every accepted change with its hypothesis and before/after numbers |

## Setup

```bash
pip install -r requirements.txt
```

Optional but recommended: create a free account at https://alpaca.markets, open
**Paper Trading**, generate API keys, copy `.env.example` to `.env`, and paste the
keys in yourself. Without keys, price data comes from Yahoo Finance and everything
except live paper trading still works.

## Commands

Download and cache price data:

```bash
python scripts/fetch_data.py
```

Backtest the current strategy on the train window:

```bash
python scripts/run_backtest.py
```

Backtest the dumb baseline:

```bash
python scripts/run_backtest.py --params config/baseline_params.json --out baseline
```

Run the tests (do this after any hand edit to a voter):

```bash
python -m pytest tests -q
```

Live dashboard (recommended while the loop runs). Start it, then open http://localhost:8765.
It refreshes every 10 seconds, shows the loop's current phase, and is read-only:

```bash
python scripts/serve.py
```

One-screen text status:

```bash
python scripts/status.py
```

Static snapshot report, then open `results/report.html` in a browser:

```bash
python scripts/make_report.py
```

Run the improvement loop for 5 rounds (omit `--rounds` to run forever, Ctrl+C to stop):

```bash
python scripts/loop.py --rounds 5
```

The final exam, only when you are done tuning:

```bash
python scripts/final_exam.py
```

Paper trading, dry run first:

```bash
python scripts/run_paper.py --dry-run
```

## What the loop does each round

1. Snapshots `config/params.json` and the voters folder.
2. Calls Claude Code headless with the current params, the best-so-far numbers,
   the baseline numbers, the ten worst trades with each voter's vote, and the
   history of earlier rounds. Claude may change a number, toggle a voter, or add
   one new voter file. It must write a one-line hypothesis.
3. Runs the tests. A new voter that peeks at the future fails here and is reverted.
4. Reruns the backtest on the train window only.
5. Keeps the change only if all of these hold: at least 30 trades, profit factor
   above 1, total P&L above the baseline, and P&L divided by max drawdown improved
   by at least 1%. Otherwise it restores the snapshot.
6. Writes `results/rounds/round_N.json`, appends kept changes to `CHANGELOG.md`,
   rebuilds `results/report.html`.

The loop never touches the holdout window. Claude is told not to read it and has
no shell access during a round.

## Autopilot: the machine works on itself

Two scheduled tasks make the system self-running:

| Task | When | What |
|---|---|---|
| `TradingBot Paper Runner` | weekdays 17:35 local | trades the live params on the Alpaca paper account |
| `TradingBot Autopilot` | Saturdays 12:00 local | grades, protects, and retrains |

Each autopilot run: refreshes data, pulls fills from Alpaca and pairs them into trades,
compares live results with a backtest over the same days (the reality gap), and checks the
kill switch (pause new entries if live drawdown >= 10% of the sizing cap, or profit factor
< 0.8 after 30 trades). Every 4 weeks it also retunes: windows roll forward so the train set
is the ten years ending one year ago and the holdout is the most recent year; the compute
sweep produces a challenger; the challenger replaces the live champion only if it beats it on
the holdout year by a 15% margin on both universes. If the sweep stalls twice, it spends three
Claude rounds on structural ideas, judged by the same gate. Reports land in `results/reports/`
and on the dashboard. Settings live under `autopilot` in `config/settings.json`.

```bash
python scripts/autopilot.py --dry-run      # see what it would do
python scripts/autopilot.py --force-retune # retune now
```

## Books: more than one strategy at once

`settings.books` lists the strategies that trade side by side. Each has its own params file,
its own share of the $25k sizing cap (`capital_pct`), its own positions file
(`results/paper/positions_<book>.json`) and its own retune in the autopilot. A stock held by one
book is off-limits to the others. The first book is the primary: it must beat the dumb baseline
and is the only one Claude research rounds are spent on. Book 2, `mr`, is mean reversion
(`config/params_mr.json`, voters `pullback` and `rsi`): buy a sharp short dip in a stock that is in
a long-term uptrend, sell the bounce within days. It failed its holdout on stocks but passed on ETFs, so it runs as a 20% paper experiment on the ETF set (`universe: etf`); the autopilot judges it monthly on forward data.

Book 3, `xa`, is cross-asset trend following (`config/params_xa.json`, universe `xa_universe` in
settings: long bonds, gold, silver, miners, commodities, oil, gas, the dollar, euro, yen, emerging and
developed markets, Japan, Brazil, China, REITs). It exists to earn when money leaves US stocks; its
correlation with the trend book is about 0.25. Same gates as every book before it gets capital.

## Risk limits (insurance, not a return engine)

`risk_limits` in `config/settings.json` applies account-wide, across all books, live and in every backtest:
a cooling-off day after a daily or five-session loss beyond a threshold (no new entries, stops still run),
a cap on the money lost if every open stop is hit (new sizes are cut to fit), and a cap on open positions
per theme (sector for stocks, asset class for ETFs; `config/sectors.json`). The chosen levels bind only in
extremes; tighter levels were measured to cost real return on the holdout year (see CHANGELOG).

## The rotation book

`rotation` is a different mechanism from the committee: on the first session of each month it ranks ~130 large
caps by their 12-month return and holds the top 8 with inverse-volatility weights, only names whose momentum is
positive (cash otherwise). No targets or time exits; each position carries a wide 20% disaster stop so the safety
net has an order to find. Engine `tradingbot/rotation.py`, live planning `tradingbot/rotation_live.py`, research
`scripts/rotation_research.py` (grid + walk-forward; always judge it on `stocks2016`, a universe chosen by size in
2016, because today's list is made of the decade's winners). The autopilot retunes it with the same holdout gate.

## Candidate books on watch

A book in `settings.books` with `enabled: false` and `watch: true` never trades and has no capital, but the
autopilot retunes and grades it every month like a live book and the report marks it **READY** when its
params have a profit factor of at least 1.1 on every holdout start date. Enabling it is a human decision.
Books can carry their own `benchmark`, `costs` and `fractional` sizing (`tradingbot/config.book_settings`),
which is how the crypto candidates (`crypto`, `crypto_mr`) are modelled with Alpaca's 25 bps crypto fee.
Research any book with `--book <name>` on `sweep.py`, `walk_forward.py` and `run_backtest.py`.

## Order execution

Scheduled runs happen before the New York open and submit market-on-open (OPG) orders, which fill in
the opening auction at the official open, the same price the backtester assumes. Once filled, a
one-cancels-other stop/target is attached. Runs started after the open (or `/run` from Telegram) use
marketable limit bracket orders instead: a buy limit 20 bps above the ask (or above the latest trade if
the quote is stale), unfilled after two minutes -> replaced by a market order. Fills and slippage (fill vs the price the limit was built on) are logged per order in
`results/paper/orders.csv`. Settings: `paper.entry_orders` in `config/settings.json`.

Safety net, every run: the planned entries are saved to `results/paper/pending_plan.json` before anything is
submitted, and a reconciliation step at the start and end of the run compares the books with Alpaca. A position
that is held but tracked by no book is adopted; a position with no open stop order gets a fresh stop/target
attached; both are reported in the Telegram message. Stop and target are rebuilt from the actual fill price.
A completed live run writes `results/paper/done_<date>.flag`; `--dry-run` never writes it (its signal files are
suffixed `_dryrun`), so a dry run in the morning does not cancel the real run.

## Telegram updates

Create a bot with @BotFather in Telegram, paste its token into `.env` as
`TELEGRAM_BOT_TOKEN`, send the bot any message, then run:

```bash
python scripts/telegram_setup.py
```

After that every paper run sends a short message (signals, orders placed, open positions),
the Saturday autopilot sends its report, and any crash or kill-switch event alerts you.
Without a token everything runs the same, just silently.

The bot also answers commands (listener runs from logon as the task `TradingBot Telegram`):
`/balance`, `/positions`, `/status`, `/signals`, `/report`, `/pause`, `/resume`, `/run`, `/help`.
It responds only to the chat id in `.env`.

## Exits

Each book's `exit` block: `stop_atr` (initial stop below the fill), `target_atr` (profit target; 0 = none),
`trail_atr` (0 = fixed stop; otherwise the stop ratchets up to the highest high minus this many ATRs, never
down, and the runner replaces the stop order before each open), `max_hold_bars` (time exit). Stop and
target are rebuilt from the actual fill. `scripts/trade_analysis.py` measures how far every historical
trade went against you before resolving, so stop distances are chosen from data, not habit.

## Walk-forward validation

```bash
python scripts/walk_forward.py --book trend --years 2018-2025 --train-years 5 --samples 150 --seed-params config/params_seed_trend.json
python scripts/walk_forward.py --book trend --seed-params config/params_seed_trend.json --sweep-args "--fix exit.trail_atr=0" --out-name trend_fixedexit
```

Always start folds from a neutral seed file, never from the live params (they were tuned on the test years).
`--sweep-args` pins knobs (`--fix key=value`) for A/B comparisons of the search space; `--space` overrides
the knob grid (`simple` is the two-rule bot). `scripts/bench_sweep.py` times one evaluation.

For every test year it re-runs the whole search on the years before and trades the winner through
the unseen year. Eight fresh out-of-sample years say more than one holdout. Results and per-fold
sweep logs land in `results/walk_forward/`. The autopilot also measures real fills against the
backtest's cost assumptions every week and, after 30 fills, calibrates `costs.slippage_bps` to reality.

## The numbers on the dashboard

- **Total P&L**: money made or lost over the window.
- **Win rate**: share of trades that made money. Low is fine if wins are bigger than losses.
- **Profit factor**: total won divided by total lost. 1.0 is break-even.
- **Max drawdown**: the worst peak-to-trough drop in account value. How painful the ride was.
- **P&L / DD (recovery ratio)**: profit per unit of pain. This is what the loop optimises.
- **Sharpe**: return per unit of day-to-day wobble. Very high values usually mean a bug.

## Why the result is not a promise

Even with costs modelled, a backtest assumes your orders fill at the open price and
that nothing you do moves the market. It also cannot include events that were not
in the data. Treat every number as an upper bound. The order of trust is:
backtest on train < backtest on holdout < months of paper trading < small real money.

## Scheduling on Windows (all three tasks)

```bash
powershell -ExecutionPolicy Bypass -File scripts\schedule_all.ps1
```

Registers, and re-registers safely:

| Task | Triggers | Notes |
|---|---|---|
| TradingBot Paper Runner | weekdays 17:15 local, and every logon | market-on-open orders before the open, stop/target attached after the fill; skips if today already ran |
| TradingBot Autopilot | Saturdays 12:00 local, and every logon | skips if it ran within the last 6 days |
| TradingBot Dashboard | every logon | serves http://localhost:8765 |
| TradingBot Telegram | every logon | answers /balance, /positions, /status ... in Telegram; auto-restarts |

All tasks wake the machine from sleep and run late if the scheduled time was missed.
The times in `scripts/schedule_all.ps1` assume a UTC+4 machine clock with no daylight saving; **edit them for
your timezone**. The runner should start before the US open (09:30 New York), which is 17:30 UTC+4 in US
summer time and 18:30 UTC+4 in US winter time.

When the laptop must be on or asleep (not shut down):
- weekdays 17:15 to about 18:40 (the runner submits before the New York open and waits for the fills just after it; in US winter the open is 18:30 local)
- Saturdays 12:00 for up to an hour when a retune runs
If it was shut down at those times, the tasks run at the next logon instead. A logon run more than
4 hours before the open does nothing and leaves the day to the 17:15 run; a logon run after the open
trades intraday with limit orders if the market is still open.

Remove everything with `Unregister-ScheduledTask -TaskName "TradingBot *" -Confirm:$false`.

## Safety rules built in

- The paper runner refuses any account whose number does not start with `PA`.
- API keys live only in `.env`, which git ignores. Nothing in this repo prints them.
- Claude runs in the loop with file tools only, no shell, and a turn limit.
- The tests fail if any voter uses future data.

## License

MIT, see [LICENSE](LICENSE). Use at your own risk, see [DISCLAIMER.md](DISCLAIMER.md).
