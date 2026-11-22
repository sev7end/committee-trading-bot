# Strategy change log

Every accepted change from the iteration loop is appended here with its version,
the one-line hypothesis, and the train-window numbers before and after.
Rejected rounds are listed in results/rounds/ but not here.

## v1 — initial committee
Five rule-based voters (trend, breakout, volume, volatility, market regime),
3-of-5 agreement, 1 ATR stop / 2 ATR target, 1% risk per trade.

## v2 — round 1 (2026-09-14)
Raised market_regime weight 1.0->2.0 and min_score 2.5->3.5 so a bearish SPY regime vetoes entries because all 7 counter-regime trades netted -$115 and one sat inside the max drawdown.

| | before | after |
|---|---|---|
| trades | 192 | 185 |
| total P&L | $9,780 | $10,128 |
| profit factor | 1.61 | 1.66 |
| max drawdown | $2,576 | $2,432 |
| P&L / DD | 3.80 | 4.16 |

## v3 — round 4 (2026-09-14)
Sped up the market_regime average (sma 50->20) because the whole max drawdown sits in the Sep-Nov chop where SPY stayed above its slow average, so the veto never fired.

| | before | after |
|---|---|---|
| trades | 185 | 168 |
| total P&L | $10,128 | $10,709 |
| profit factor | 1.66 | 1.71 |
| max drawdown | $2,432 | $2,384 |
| P&L / DD | 4.16 | 4.49 |

## v4 — round 6 (2026-09-14)
Added a momentum voter (60-bar return, weight 1.0) because 865 signals were dropped for lack of slots and today's scores tie at 4.8/6.3, so slot picks were arbitrary.

| | before | after |
|---|---|---|
| trades | 168 | 165 |
| total P&L | $10,709 | $10,288 |
| profit factor | 1.71 | 1.67 |
| max drawdown | $2,384 | $2,219 |
| P&L / DD | 4.49 | 4.64 |

## v5 — round 7 (2026-09-14)
Cut breakout weight 1.5->0.5 because the 59 breakout=0 trades averaged $91 vs $46 for the 106 breakout=1 trades, so ranking slots by breakout picks extended tops.

| | before | after |
|---|---|---|
| trades | 165 | 172 |
| total P&L | $10,288 | $10,688 |
| profit factor | 1.67 | 1.68 |
| max drawdown | $2,219 | $2,185 |
| P&L / DD | 4.64 | 4.89 |

## v6 — round 8 (2026-09-14)
Widened momentum.full_scale 0.3->0.5 because ~45 of 171 trades tie at momentum 1.0, so scarce slots pick arbitrarily; un-saturating ranks the strongest movers (INTC/AMD) first.

| | before | after |
|---|---|---|
| trades | 172 | 177 |
| total P&L | $10,688 | $12,623 |
| profit factor | 1.68 | 1.78 |
| max drawdown | $2,185 | $2,423 |
| P&L / DD | 4.89 | 5.21 |

## v7 — round 12 (2026-09-14)
Sped up market_regime sma 20->15 because SPY hugged its 20-day average on 5 chop days that waved through 12 trades losing a net $845, $627 of it in the weak first half.

| | before | after |
|---|---|---|
| trades | 177 | 173 |
| total P&L | $12,623 | $11,560 |
| profit factor | 1.78 | 1.69 |
| max drawdown | $2,423 | $2,105 |
| P&L / DD | 5.21 | 5.49 |

## Phase 2 — long-history search (2026-09-14)
Phase 1 (9-month train window, Claude loop, 12 rounds, v7 best) is archived in results_phase1/.
Round 10's record was lost during the stop; it was a reverted round (version stayed v6 until round 12).
Phase 2 retunes on 2016-01-01 -> 2025-08-31 with a compute-only parameter search and holds out
2025-09-01 -> 2026-09-12. Validation on an ETF universe guards against survivorship bias.

## v8 — parameter sweep (2026-09-14)
Compute-only search, 1627 evaluations on 2016-01-01 -> 2025-08-31, both universes.

| | stocks | ETFs |
|---|---|---|
| trades | 729 | 429 |
| profit factor | 1.57 | 1.58 |
| total P&L | $51,048 | $16,085 |
| max drawdown | $5,528 | $2,175 |
| P&L / DD | 9.23 | 7.40 |
| positive years | 90% | |

### Final exam for v8 (holdout 2025-09-01 -> 2026-09-12, run once on 2026-09-14)
| | stocks v8 | stocks baseline | ETFs v8 | ETFs baseline |
|---|---|---|---|---|
| trades | 83 | 214 | 67 | 140 |
| profit factor | 1.19 | 1.08 | 1.48 | 1.02 |
| total P&L | $1,482 | $1,524 | $2,074 | $127 |
| max drawdown | $1,510 | $3,666 | $1,330 | $1,738 |
| P&L / DD | 0.98 | 0.42 | 1.56 | 0.07 |
Decision: profitable on unseen data on both universes with lower risk than the baseline -> start Alpaca PAPER trading with v8.
Train->holdout profit factor fell 1.58 -> 1.19 on stocks: expect modest returns. The holdout is now used; any further tuning needs fresh forward data.

## Universe widened to 100 stocks, 8 position slots (2026-09-14)
User asked for more variation. v8 tested on 98 stocks vs 30, with 5/8/12 slots, over train and three holdout starts.
98 stocks + 8 slots: train PF 1.47, holdout PF 1.18-1.28 and +$1,800..+$3,245 across starts (30 stocks + 5 slots: PF 0.97-1.19).
~130 trades/yr instead of ~80. Old list saved as config/universe_30.json. Autopilot retune forced on the new universe.

## v9 — autopilot retune (2026-09-14) via sweep
challenger score 1.63 beats champion 1.21 by >= 1.15x
Holdout 2025-09-15 -> 2026-09-11: stocks PF 1.36 (champion 1.26), ETF PF 1.32 (champion 1.32).

## v9 — autopilot retune on the 100-stock universe (2026-09-14 21:55)
Sweep challenger replaced v8: target 2.5->2.0 ATR, regime SMA 15->100, trend fast 20->10, ATR% max 4%->6%.
Holdout 2025-09-15 -> 2026-09-11: stocks PF 1.36, +$4,558, DD $1,703 (v8: PF 1.26, +$2,426); ETFs PF 1.32.

### Tested and rejected: sector caps (2026-09-14)
account.max_per_sector 2 or 3 cut holdout PF from 1.18-1.36 to 0.84-1.10 across four start dates (gains were
concentrated in one sector; the cap forced weaker entries). Left in the engine as an option, set to 0.

### Process: plateau scoring added to the sweep (2026-09-14)
After random search and refinement, the top 8 passing candidates are re-ranked by the average score of
themselves plus every one-step neighbour in the search grid (failing neighbours count 0). The winner is the
broadest plateau, not the sharpest peak. Verified end to end; used by every autopilot retune from now on.

### 2026-09-15: scheduler fix and first paper orders
All three scheduled tasks failed on their first firing (exit 1): cmd.exe /c stripped the outer quotes around the
quoted python path, so nothing ran and no log was written. Fixed by removing quotes (paths have no spaces) and
re-registering via scripts/schedule_all.ps1. Runner started manually at 19:25 local (11:25 New York): placed the
first two paper bracket orders (T x157, VZ x97) under v9.

## v2 — parameter sweep (2026-09-16)
Compute-only search, 1133 evaluations on 2015-09-14 -> 2025-09-14, both universes.

| | stocks | ETFs |
|---|---|---|
| trades | 2171 | 1980 |
| profit factor | 1.24 | 1.17 |
| total P&L | $40,426 | $15,273 |
| max drawdown | $10,687 | $5,181 |
| P&L / DD | 3.78 | 2.95 |
| positive years | 82% | |

## Book 2 "mr" — mean reversion (2026-09-16)
Sweep (1133 evals, SPACE_MR, no baseline check) -> params_mr.json v2: pullback 1.5 ATR below 50d SMA + RSI(3)<=15 + SPY>100d, stop 2.5 / target 2.0 ATR / 10 bars.
Train: stocks PF 1.26 ratio 4.21, ETFs PF 1.17 ratio 3.01. HOLDOUT on stocks: PF 1.00 at 3 of 4 start dates -> FAILED on stocks; blending into the account hurt at every split.
HOLDOUT on ETFs: PF 1.23-1.46 at all four start dates. Trend(stocks)+MR(ETFs) at 80/20: holdout ratio 2.27/2.18/2.60 vs trend-only 2.68/2.14/2.44, drawdown ~10-15% lower, P&L ~15% lower, correlation 0.60.
Decision: run MR on ETFs as a 20% paper experiment (settings.books), autopilot retunes/judges it monthly (primary universe = etf). Not a proven edge yet.

## v2 — parameter sweep (2026-09-18)
Compute-only search, 1078 evaluations on 2015-09-14 -> 2025-09-14, both universes.

| | stocks | ETFs |
|---|---|---|
| trades | 425 | 453 |
| profit factor | 1.53 | 1.68 |
| total P&L | $13,284 | $18,243 |
| max drawdown | $2,307 | $3,013 |
| P&L / DD | 5.76 | 6.05 |
| positive years | 82% | |

## Book 3 "xa" — cross-asset trend following (2026-09-18)
Universe: TLT IEF TIP LQD HYG GLD SLV GDX DBC USO UNG UUP FXE FXY EEM EFA EWJ EWZ FXI VNQ. No SPY regime filter.
Sweep (1078 evals, SPACE_XA, primary xa / secondary etf) -> params_xa.json v2: trend 100/250 + 55d breakout + 180d momentum,
all 4 agree, stop 4 ATR / target 6 ATR / 20 bars. Train: PF 1.53, ratio 5.76, 82% positive years, +$1,477 in 2022.
HOLDOUT at 4 starts: PF 1.20-1.40, all positive. Correlation with trend book 0.22.
Combined account (trend/mr/xa 60/20/20) vs trend-only on holdout: P&L/DD 3.02/2.42/2.82 vs 2.93/2.38/2.67; drawdown ~30% lower, P&L ~30% lower.
Decision: enable xa at 20%; books now trend 60 / mr 20 / xa 20.

## Volatility sizing (2026-09-18) — trend v10, xa v3
New tradingbot/sizing.py: risk per trade scaled by a volatility factor, clipped to [0.5, 1.5].
Tested none / benchmark 12,15,20% / self on all books, train + 3 holdout starts.
- trend: benchmark target 15% (SPY 20d vol). Train: PF 1.60->1.67, same P&L, DD $13.1k->$10.9k. Holdout roughly neutral. ADOPTED.
- mr: every variant hurt holdout. NOT adopted.
- xa: self (own 20d vol vs 1y median). Train PF 1.53->1.71, Sharpe 0.74->0.89; holdout mixed (2 of 3 starts better). ADOPTED with caveat.
Combined 60/20/20: train P&L $78k->$79k with DD $8.3k->$5.6k; holdout ratio 3.02/2.42/2.82 -> 2.19/2.96/3.49.

### Tested and rejected: earnings-date blackout (2026-09-18)
tradingbot/earnings.py (Yahoo earnings dates, cached in data/earnings/, blackout_mask, runner check) is in place but OFF.
Trend book with no new entries around earnings, train 2015-2025: no filter PF 1.67 / $115k / ratio 10.6;
2 before-0 after PF 1.56 / $88k; 3-1 PF 1.46 / $70k; 5-1 PF 1.48 / $75k; 3-3 PF 1.43 / $61k. Holdout worse at every setting too.
Reason: this is a breakout system; many of its best entries ARE post-earnings breakouts. Blocking them removes edge, not risk.
Kept as an option (params.entry.earnings_blackout) for any future non-breakout book.

## Execution: marketable-limit entries (2026-09-18)
New tradingbot/execution.py. Entry orders are bracket LIMIT orders 20 bps above the ask (sane quote: spread <= 0.5%),
else above the latest trade, else above the previous close. Polled up to 120 s; unfilled -> cancelled and replaced by a
market order so no signal is lost. Every order logs order_type, limit_price, bid/ask/last used, fill_price, slippage_bps
(fill vs the base price) and status in results/paper/orders.csv. Off-hours IEX quotes were found to be stale and absurdly
wide, hence the sanity check. Backtest cost model unchanged (already charges half-spread + 5 bps); this is a live-fill
safety and measurement improvement, judged via slippage_bps and the autopilot reality gap.

## Process: slippage feedback + walk-forward validation (2026-09-18)
- autopilot.execution_quality(): every Saturday measures real entry fills vs that day's open (the quantity the backtest
  models as half-spread + slippage) and stop fills vs the stop price; written to results/paper/execution_quality.json and
  the weekly report. calibrate_costs(): once >= 30 fills exist, settings.costs.slippage_bps is set to the measured median
  (bounded 2..30 bps) so future retunes are judged on real costs. Config: autopilot.calibrate_costs.
- scripts/walk_forward.py: for each test year, re-run the full sweep on the preceding N years and trade the winner
  through the unseen year; compares with the dumb baseline and the current live params. Results in results/walk_forward/.

### Day trading, step 1: Opening Range Breakout research (2026-09-18) — REJECTED after costs
Data: Alpaca IEX 5-minute bars, available only from 2020-07-27 (99% of days complete). tradingbot/intraday.py (ORB engine,
5 tests), scripts/orb_research.py. Grid: range 5/15/30 min x long/both x stop range/ATR 10%/ATR 25% x target none/2R,
universes SPY+QQQ / 10 mega-caps / all, costs zero / base (3+5 bps per side) / double.
Result: 0 of 108 configurations profitable at base costs (best PF 0.96). At ZERO costs the best reach PF 1.22-1.23 with
5-7 positive years out of 7 -> a small gross edge exists but is smaller than one round trip of realistic costs.
Decision: no intraday book. Engine kept for future research (e.g. with a paid consolidated feed and lower cost assumptions).

### Tested and rejected: inverse index ETFs in the cross-asset book (2026-09-18)
Adding SH/PSQ/DOG/RWM to xa_universe: 13 trades in 10 years, all losers (-$1,238; -$1,093 of it in 2022). Train ratio 6.80 -> 4.68,
holdout unchanged (none triggered). Slow trend filters enter inverse funds late and get whipsawed. Not adopted.

### Process: multi-start holdout gate (2026-09-18)
autopilot.holdout_scores now scores at holdout start + 0/14/42 days; a challenger must beat the champion by the margin on a
majority of starts, be profitable on a majority, and never fall below PF 0.9 at any start. Config: autopilot.holdout_start_offsets_days.

## Execution: opening-auction orders (2026-09-18)
Scheduled runner moved to 17:15 local (before the New York open) with --auction: entries are market-on-open (OPG) orders that
fill in the opening cross at the official open (the backtester's assumed fill); time exits are OPG sells. After the open the
runner waits for the fills and attaches a one-cancels-other stop/target (tradingbot/execution.py: submit_auction_entries,
submit_auction_exit, attach_brackets_after_open). Runs started after ~09:27 NY fall back to marketable-limit brackets.
run_paper.py restructured: plan_book() -> submit (per mode) -> record_entries().

### Walk-forward validation of the trend book's search process (2026-09-18)
8 folds, test years 2018-2025, 5-year train each (data from 2015 -> early folds shorter), 150-sample sweep + refinement + plateau per fold.
See results/walk_forward/trend.json. Summary below in the report; process produced profitable out-of-sample years in 7 of 8, above the
dumb baseline in every year, with the single loss in 2022 (bear market, baseline lost more).

### Walk-forward, clean rerun from a NEUTRAL seed (2026-09-18, supersedes the table above)
The first run seeded every fold's sweep with the LIVE params (tuned on 2015-2025 data, i.e. on the test years), and the sweep
keeps its seed as a candidate -> contamination. Rerun with --seed-params config/params_seed_trend.json (textbook defaults, never
tuned), --out-name trend_neutral. Results (results/walk_forward/trend_neutral.json), winner PF / P&L on unseen year vs baseline:
2018 1.06 +$1,021 (base 1.14) | 2019 NO CANDIDATE passed (only 4y of data, baseline ratio 3.50 unbeatable) | 2020 1.84 +$8,131 (1.06)
| 2021 1.17 +$2,258 (1.10) | 2022 0.85 -$1,530 (0.78, -$3,945) | 2023 1.25 +$2,669 (1.44, +$7,598) | 2024 1.02 +$295 (1.28, +$4,865)
| 2025 1.12 +$1,432 (0.94, -$1,289). 7 folds: profitable 6/7, total OOS +$14,277 vs baseline +$12,143 over the same years,
median PF 1.12 vs 1.10, beat baseline on P&L/DD in 3/7. Conclusion: the search process finds a small, fairly consistent edge
(6/7 profitable years, never worse than the baseline's bad years) but on average only marginally better than the dumb one-rule
bot; the first run's 7/8 / +$26.7k was inflated by the seed. Honest forward expectation for the live trend book: PF ~1.1,
low single-digit thousands per year on $25k, $3-5k drawdowns, losing years possible. The live v10 params' own numbers on these
years (PF 1.14-2.28) are NOT evidence, they were tuned on them.

### Fixes from the first weekly review (2026-09-22)
- Autopilot live stats showed 0 completed trades although three stops had fired (VZ, COP, CVX). Cause: run_paper.sync_fills only
  recorded top-level orders; bracket stop/target sells are returned as legs of the parent buy. Legs now included; the Monday
  report's "live 0 trades / reality gap" line was wrong for that reason.
- Runner: in auction mode a logon run more than --max-minutes-before-open (240) minutes before the open now exits without doing
  anything and without writing the signals file, so the scheduled 17:15 run is not skipped. Before, a morning logon would have
  submitted market-on-open orders and then been killed by the task's 5-hour limit before it could attach the stops.
- Saturday's autopilot did not run (laptop off); it ran at Monday's logon as designed.

### Code review: 27 defects found and fixed (2026-09-22)
Two independent reviews (live trading path; research engine) plus a pass over the Claude escalation loop. Every finding was
verified against the code before fixing; regression tests added in tests/test_review_fixes.py and tests/test_runner_paths.py
(the latter runs the runner's order paths against a fake Alpaca client).

ENGINE (changes backtest numbers; live params re-judged by a forced retune, see below)
- Sizing look-ahead: shares for a fill at day t's OPEN were sized off equity marked at day t's CLOSE. Now yesterday's close.
- vol_scale also multiplied max_position_pct (a 20% cap became 30% in calm markets). Now it dials risk only; the cap is a cap.
- A voter with weight 0 still counted toward min_voters_agree (8.6% of live entries were admitted by "silent" voters). Weight 0 = off.
- RSI was NaN on all-gain windows (3-day RSI: common), so the mean-reversion book never got its "stretched, don't buy" veto. RSI 100.
- Positions in a symbol with no bar on the last date were never closed (missing from the trade list). Closed at their last close.
- data_start gave ~170 bars before the train window; the slowest voters need 250+, so the xa book could not trade for the first
  4 months of every train window. data_start = train start - 450 days (autopilot.history_days_before_train); history refetched.
- data.py: a refetch re-downloaded from data_start and threw away older cached history. It now refetches from the earlier of the two.
- sweep: yearly P&L started the first year from the end of day 1, and counted 3-month stubs as years. Now from starting cash, years
  with < 200 bars ignored. The secondary universe needs >= max(30, min_trades/5) trades (a lucky handful inflated the score).
- autopilot: leap-day crash in roll_windows; holdout offset 0 always kept; a losing champion (score 0) made the 15% margin vacuous
  (challenger now needs an absolute score >= 0.5 there); a sweep crash looked like "nothing better" and counted as a stall; the
  Claude-loop challenger's new voter file was deleted before it was judged; a failure in a later book reverted an already applied
  replacement of an earlier one (the new champion is now re-snapshotted immediately); the kill switch resumed when ANY book was
  replaced (now only when the primary is); loop.py compared candidates against a best-so-far computed on a previous train window.
Effect on the live params (same data, corrected engine):  trend v10 train PF 1.64 -> 1.57, P&L/DD 13.2 -> 9.0; HOLDOUT PF 1.37 -> 1.06,
P&L $4,694 -> $836.  mr v2 holdout PF 1.32 -> 1.36.  xa v3 holdout PF 1.45 -> 1.35. The old trend numbers were inflated by the
engine bugs (mostly the position-cap scaling and the sizing look-ahead); the quorum fix makes v10 stricter (all 4 real voters must
agree), which fits the train window better and the holdout worse. Forced retune of all books under the corrected engine started.

LIVE RUNNER (safety)
- OCO stop/target request was built without take_profit and would have been rejected by the client on the first auction fill
  (found by the new fake-broker test; the code had never run live).
- A fill whose bracket failed, a partial fill on a cancelled OPG order, or a fill missed by the 5-minute poll (sleep, network) was
  marked unfilled and dropped: a held position with NO stop that no later run would ever look at. Now every filled quantity is
  tracked, stop/target are rebuilt from the FILL price (as in the backtest), timeouts cancel and re-read the order, and a
  reconciliation step at the start and end of every run adopts any held-but-untracked position and attaches a stop wherever none is open.
- The plan is persisted before submission (results/paper/pending_plan.json) so a run killed mid-way can be repaired next run.
- Time exit: the stop/target were cancelled before the sell, and a failed sell was ignored while the position was dropped from the
  book. Now cancels settle first, the position leaves the book only when the sell order exists, otherwise the bracket is re-attached.
- Time exit fired ~2 sessions later than the backtest (counted bars after entry, sold at the next open). Now counts from the entry day.
- --dry-run wrote the "already ran today" marker: a morning dry run silently cancelled the real 17:15 run. Marker is now done_<date>.flag,
  written only by a completed live run; dry-run signal files are suffixed _dryrun.
- OPG cutoff re-checked right before submission (data download + scoring take minutes); falls back to intraday orders if too late.
- Intraday partial limit fill was followed by a FULL-size market fallback (double position). Fallback only when nothing filled.
- SPY was removed from the tradable ETF universe by the benchmark pop (13 of 14 instead of 15). Stale-data guard: run aborts with an
  alert if the benchmark's last bar is older than the last session; symbols lagging behind are dropped for the day.
- Alerts for runs with failures/exits/repairs are no longer sent silently; the feed-failure alert is no longer sent twice.
- fills.csv merged with history (API pages at 500 orders). Dashboard: positions from all books, only live primary signal files,
  /api/state returns a JSON error instead of dropping the connection. Telegram /status and /signals ignore dry-run/secondary files.
- schedule_all.ps1 checked $root for spaces before assigning it.

### Sweep / autopilot speed (2026-09-22)
Profiling one candidate evaluation (100 stocks, 5 years) showed >50% of the time in pandas label lookups inside the
engine's day loop (126k row fetches + 534k series lookups per evaluation). Changes, all with trade-for-trade identical results
(verified against the old loop on stocks and ETFs and against the running sweep's logged metrics):
- backtest.py: every series is aligned to the master calendar once as numpy arrays; the day loop uses integer indexing.
- tradingbot/memo.py: opt-in memo of voter / ATR / vol-scale series per (data object, knobs); enabled in sweep workers
  (each voter depends only on its own knobs, so a few thousand entries cover the whole search).
- sweep.py: secondary universe skipped when the primary already failed its hard constraints; refinement sends all ~70
  one-knob neighbours in one parallel batch (was 2-6 at a time, leaving most workers idle); workers run below normal CPU
  priority; default workers = cores - 6.
Benchmark (scripts/bench_sweep.py, same 6 candidates, laptop under load): 9.71 s/eval -> 1.67 s (arrays) -> 0.91 s (arrays + warm memo).
The forced retune started at 00:36 on the old code was stopped in its refinement phase and relaunched on the new code.

## [xa] v4 — autopilot retune (2026-09-22) via sweep
challenger wins 2/3 starts won by >= 1.15x, profitable on 3/3; median score 1.39 vs champion 0.88
Holdout 2025-09-23 -> 2026-09-21: stocks PF 1.38 (champion 1.36), ETF PF 1.59 (champion 1.44).

### Account-level risk limits (2026-09-22)
New tradingbot/risklimits.py, settings.risk_limits, applied both live (across all books, from Alpaca positions + portfolio
history) and in every backtest so research includes the insurance cost:
- daily_loss_pct / weekly_loss_pct: after losing more than this (of the $25k sizing base) yesterday / over 5 sessions, no
  new entries today (cooling-off). Exits and stops always run.
- max_open_risk_pct: money lost if every open stop is hit, plus today's entries, capped; new sizes are cut to fit.
- max_per_theme: open positions per theme across books (sectors for stocks; ETFs mapped to asset classes in sectors.json).
Cost measured on the corrected engine (holdout year, PF): tight set (theme 3 / open risk 6% / loss 2%/4%) cut the trend book
1.11 -> 0.97, mr 1.10 -> 1.02, xa 1.38 -> 1.18 -- most of it from the loss cooling-off (the day after a bad day was a good
day to enter). Chosen: theme 4 / open risk 10% / loss 3% / 6%, which binds only in extremes (holdout trend 1.17, mr 1.10,
xa 1.36; train trend ratio 8.8 vs 9.1). The Telegram summary shows the limit state each run.
Consistency fix: run_backtest.py, autopilot.eval_params and walk_forward.py removed SPY from the tradable ETF universe
(benchmark pop) while sweep.py and the live runner kept it; the mr book was judged on 14 ETFs after being optimised on 15.
All three now keep the benchmark tradable when the universe lists it.

### Stops from the data: excursion analysis (2026-09-22)
scripts/trade_analysis.py measures, for every backtest trade, the worst dip (MAE) and best run (MFE) in ATR units.
Trend book v10, train window, 1,475 trades: winners dipped a median 0.8 ATR before winning, 22% dipped > 1.5 ATR and 11% > 2 ATR;
losers were rarely worth saving (only 15% were ever up > 1.5 ATR). What-if every trade had used a different stop: 1.0 ATR
-$64k, 1.5 ATR -$30k, 2.0 ATR -$14k, 2.5 ATR +$0.4k, 3.0 ATR (actual) +$3k, 4.0 ATR -$5k. Verdict: the "pros use tight stops"
intuition is wrong for this strategy; the 3 ATR stop the search found is where the data says it should be. Kept.

### Trailing exits: clean walk-forward, fixed vs trailing allowed (2026-09-22)
Two neutral-seed walk-forwards of the trend book (8 folds 2018-2025, 5-year train, 150 samples, corrected engine, risk limits on):
  fixed exits only (--fix exit.trail_atr=0):  profitable 6/8, total OOS +$18,213, median PF 1.13, summed DD $29,098
  trailing allowed (space adds exit.trail_atr 0/2/3/4/6, target_atr 0 = none, hold up to 120):
                                              profitable 7/8, total OOS +$19,235, median PF 1.19, summed DD $27,523
The search chose a trailing stop in 6 of 8 folds, almost always WITH a profit target kept (2-5 ATR); the pure
"no target, let it run" form was never the winner. Verdict: a small, consistent improvement, inside noise on any one
year. trail_atr stays in the search space; the next retune decides whether the live params adopt it (gate unchanged).
Per-fold winners: results/walk_forward/trend_fixedexit.json, trend_trailing.json. Each fold now runs in 1-2 minutes.

### Fewer knobs: the two-rule bot walk-forward (2026-09-22)
SPACE_SIMPLE (config/params_simple.json): breakout lookback + market-filter SMA + stop/target/trail/hold, six knobs, no weights,
both rules must agree. Neutral-seed walk-forward, 8 folds 2018-2025 (100 samples, 3 refinement passes, plateau top 4, ~1 min/fold):
  two-rule bot: profitable 6/8, total OOS +$18,915, median PF 1.31, summed DD $23,686
  committee (fixed exits): 6/8, +$18,213, median PF 1.13, summed DD $29,098; committee (trailing allowed): 7/8, +$19,235, 1.19, $27,523
Same profit, smaller drawdowns, a third of the knobs. Decision: the trend book's search now uses the simple space (settings
books[0].space = "simple", seed = params_simple.json); the live params change only if the simple challenger passes the holdout
gate against the committee champion (retune running). The runner/dashboard handle any voter set.

### Tested and rejected: a short stock book (2026-09-22)
Engine gained a short side (params.entry.side = "short": directional votes flipped, filters as-is; stops above, targets below,
collateral accounting; tests/test_short_side.py). Sweep over the trend knob grid, 400 candidates on 100 stocks 2015-2025 with the
positive-years constraint relaxed to 30%: 360 candidates traded, 0% had PF > 1 (best real candidate PF 0.92, -$2,236; median PF 0.64).
Shorting large US stocks on daily trend rules lost money in this decade even before asking it to hedge anything. Not adopted;
the engine support stays for future research (e.g. a short book on a different universe).

### Fixes (2026-09-22, late)
- sweep.py: refinement/plateau crashed with KeyError when the seed params predated a knob (trail_atr); knobs missing from a seed now
  get their neutral first value, and lookups are tolerant. The autopilot correctly reported the crash and kept the champion.
- autopilot: a book may name a separate "seed" file for its search (the simple space must start from 2 voters, not the live 6).
- sweep.py: --refine-passes / --plateau-top-k for lighter research runs; autopilot --books to retune a subset.
- scripts/combine_books.py: judge a candidate book by the combined account (correlation, combined P&L/DD, funded from the primary).

### First nine days unattended: review and fixes (2026-10-01)
What happened 2026-09-22..30: auction mode worked (DHR, INTC, XLU filled at the open with OCO stops attached; four stops fired
correctly), reconciliation and risk limits ran every day, the autopilot ran at Monday logon after a missed Saturday. Realised
-$521 after 6 round trips (1 win), account -1.1%; far too early to judge.
Problems found and fixed:
- 5 of 8 market-on-open orders EXPIRED unfilled on Alpaca's paper venue (XLP twice, UUP twice, QCOM; XLU filled 1 of 2), so the
  books were not getting into their signals. The runner now re-enters every entry that got no auction fill with a marketable-limit
  bracket right after the open (run_paper.reenter_unfilled; tests/test_reenter.py); partial auction fills are kept as they are.
- 2026-09-23: Yahoo had not published the previous day's bar by 09:15 NY; the stale-data guard aborted the whole day (no entries,
  no exits). Now: two 90-second retries, then the missing session is taken from Alpaca's daily bars, and only then an abort.
- 2026-09-24: no run at all (laptop off at 17:15; the 11:45 logon run was 5.7 h before the open and deferred). A logon run now
  trades if the open is within 7 hours (task limit raised 5 -> 8 h; --max-minutes-before-open 420). A shut-down laptop at 17:15
  still means no run; the autopilot reports every missed day.
- Tiny lots when a book is nearly full (XLU x1, XLP x1): paper.min_position_notional = $250.
- Execution-quality "fill vs open" is now measured on auction fills only (intraday re-entries are measured against their quote).
- Brackets for target-less (trailing) entries park the take-profit far away so Alpaca accepts the order.

## v1 — parameter sweep (2026-10-26)
Compute-only search, 864 evaluations on 2015-10-24 -> 2025-10-24, both universes.

| | stocks | ETFs |
|---|---|---|
| trades | 614 | 321 |
| profit factor | 1.47 | 2.07 |
| total P&L | $316,695 | $119,645 |
| max drawdown | $93,097 | $17,959 |
| P&L / DD | 3.40 | 6.66 |
| positive years | 90% | |

## v1 — parameter sweep (2026-10-27)
Compute-only search, 839 evaluations on 2015-10-24 -> 2025-10-24, both universes.

| | stocks | ETFs |
|---|---|---|
| trades | 524 | 1692 |
| profit factor | 1.32 | 1.12 |
| total P&L | $40,725 | $85,848 |
| max drawdown | $10,257 | $64,068 |
| P&L / DD | 3.97 | 1.34 |
| positive years | 70% | |

### Crypto vertical: research (2026-10-26/27)
Plumbing: per-book settings (benchmark, cost model, fractional sizing: tradingbot/config.book_settings), fee_bps in the cost model
(Alpaca crypto: 25 bps a side -> ~70 bps per round trip with spread/slippage), crypto universes (10 majors with history to 2017,
9 alts from 2020 as the bias check), --book on sweep / walk_forward / run_backtest, knob grids SPACE_CRYPTO (no profit target:
the venue has no OCO, exits are a trailing stop order + time) and SPACE_CRYPTO_MR (single-coin dip buying, BTC/ETH).
Results (corrected engine, crypto costs):
- crypto trend (10 majors): train 2015-2025 PF 1.47, +1267%, DD 29% (the 2017 and 2020-21 bull runs); HOLDOUT year PF 0.57, -12%;
  walk-forward 2022-2025 profitable 1/4 (2024 only). FAILS.
- crypto_mr (BTC/ETH dip buying): train PF 1.32, +163%, DD 15%; HOLDOUT PF 0.78, -5%; walk-forward 2019-2024 profitable 5/6
  (2022 loss), total OOS +$4,207 on a $25k base over 6 years, beat the baseline on P&L/DD in 2/6. "Small and fairly consistent"
  but too small to clear 70 bps round trips with margin, and the last year lost. NOT READY.
Both books stay in settings with enabled=false and watch=true: the autopilot retunes and grades them monthly like live books and
the report marks a candidate READY when it has PF >= 1.1 on every holdout start date (and PF >= 1 on the bias set). A human
enables it. No live crypto execution exists yet; it is built only once a candidate is ready.

### Kill switch: a pause could last forever (2026-10-27)
The switch tripped 2026-10-24 (live PF 0.39 after 34 trades; the same days simulated also lost -$1,456, a choppy market: SPY
755 -> 779 -> 760). Once paused, no new trades happen, so the live profit factor could never recover and resuming depended only
on a challenger beating the champion by 15% on a weak holdout year. Now: the switch judges a rolling window (trades closed since
the last resume, min 30), and after kill_switch.resume_after_days (28) it resumes if the champion still has PF > 1 on the rolled
holdout year, starting a fresh statistics window. The runner was unaffected: exits, stops and reconciliation ran throughout.
Per book, the 34 live trades: trend 16 (7 targets +$90 avg, 8 stops -$190 avg, PF 0.37), mr 18 (PF 0.55, tiny lots).
Crypto trend walk-forward rerun with --min-trades 80 (fair for 10 coins): see results/walk_forward/crypto_fair.json; summary in the line below.
crypto trend fair WF: 5 folds scored, profitable 2/5, total OOS $23,611, median PF 0.98

### Strategy campaign (2026-10-27): two candidates cleared the bar
Bar set in advance: neutral-seed walk-forward profitable in >= 6 of 8 years with median OOS PF >= 1.2, a profitable untouched
holdout year at all 3 start dates, and an improvement to the whole account when combined.
1. CHOP FILTER for the trend book (tradingbot/voters/chop.py: Kaufman efficiency ratio of the index over N bars, a filter voter;
   breakout.confirm_bars also added). Walk-forward 2018-2025: profitable 6/8, total OOS +$21,482 (best of any committee variant),
   median PF 1.30 (was 1.13-1.19), summed DD $23,917 (lowest), worst year -$859 (was -$2,328). The search picked the filter in
   ALL 8 folds with the same setting (weight 1.0, min_er 0.3, 20 bars); confirmation bars never helped. On the live Sep-Oct window
   it would have cut 24 trades to 4 and PF 0.57 to 2.52 (after the fact, but consistent with the mechanism). Trend book search
   space set back to "trend" (committee + chop), neutral seed; the holdout gate decides the live params (retune running).
2. MOMENTUM ROTATION, a new mechanism (tradingbot/rotation.py, rotation_live.py, scripts/rotation_research.py): rank ~130 large
   caps by 12-month return on the first session of each month, hold the top 8 with inverse-volatility weights (cap 25%), only
   names with positive momentum, cash otherwise. Survivorship bias handled explicitly: on today's 100-stock list it shows an
   absurd 30-70%/yr (85% of profit from TSLA/PLTR/NVDA, which are in the list because they won); on a universe chosen by SIZE
   in 2016 (config stocks2016_universe, 98 survivors incl. GE/IBM/INTC/WFC) it is 7/8 years, 19.6%/yr vs SPY 15.5%, median PF
   2.08, worst year -5.4% (2022, SPY -18%), drawdowns 11-33%; holdout year +17-20% vs SPY +13% at all 3 starts (PF ~1.8).
   Correlation with the chop-trend book 0.57, with SPY 0.73; 8-year OOS P&L/DD 4.0 vs SPY 3.2. Enabled as book "rotation" with
   30% of capital (trend 60 -> 40, mr 20 -> 10): monthly rebalance in the runner (sells leavers, buys entrants, trims, 20% disaster
   stop per position, no theme cap), grid retune + holdout gate in the autopilot (engine == "rotation"). First rebalance: the
   first session of November, once the kill-switch pause lifts.
   ETF rotation (35 ETFs) was mediocre (5/8, 8-11%/yr, below SPY) and is not used.
Rejected this round: breakout confirmation (never chosen), ETF rotation. Data fix: a single unknown Yahoo symbol no longer breaks a fetch.

## [trend] v11 — proposed challenger judged by the holdout gate (2026-10-27)
challenger wins 3/3 starts won by >= 1.15x, profitable on 3/3; median score 1.03 vs champion 0.60
Holdout 2025-10-28 -> 2026-10-26: primary PF 1.64 (champion 0.90), bias set PF 1.85 (champion 1.29). Source: results/_cand_chop_0.3.json

### Incident: runner crashed every day 2026-10-27 .. 11-06 (KeyError: 'exit')
trail_stops() read params["exit"] for every book; the rotation book's params have no exit section. Every live run after the
rotation book was enabled crashed at that line, after reconciliation but before trailing/planning/orders: no entries, no time
exits for ten sessions. Stops and reconciliation did run each day; every held position stayed protected (verified 11-06:
2 positions, both with sell orders). The Oct 27 dry run did not catch it because trail_stops/reconcile only execute with a
broker connection. Fixed: trail_stops and reconcile tolerate books without an exit section (rotation: 20% disaster stop, no
target). New tests/test_live_steps_all_books.py runs those live-only steps against the fake broker with EVERY configured book,
so a new book type cannot pass the suite while breaking them. Lesson recorded: a dry run is not a test of the live path.
