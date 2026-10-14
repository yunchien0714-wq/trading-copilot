# Trading Co-Pilot

An AI co-pilot for discretionary **futures day traders** (CME micros / minis such as MNQ and MGC) that watches your
TradingView chart, applies **your** strategy rules, and tells you on Telegram when a setup qualifies -- with entry,
stop, target, size and the reasons. It keeps a trade journal, grades your trades against your plan, warns about
emotional trading, and keeps testing new strategy ideas in the background.

**It never places, modifies or cancels orders.** Analysis and alerts only. You trade.

## Fastest way to set it up

Give this repository's link to an AI coding assistant (e.g. Claude Code) and say:
*"Set this up for me."* The assistant follows [AGENTS.md](AGENTS.md): its first question is how you trade, then it
configures the system around your strategy and your account rules, step by step.

No strategy of your own yet? The default is a common **ICT model**: 5-minute market structure for bias,
London / New York AM killzones, entry on a 1-minute rejection inside a 5-minute fair value gap, 2R target
([config/strategy.toml](config/strategy.toml)).

## What it does

| Area | What you get |
|---|---|
| Setup alerts | Zones (fair value gaps, optional volume POC), market structure (BOS / CHoCH), 1m confirmation; Telegram alert with entry / stop / target / contracts, or the exact reason a setup was rejected |
| Risk gate | Fails closed: drawdown buffer (incl. intraday trailing), per-trade loss cap, contract limits, trades per day, news blackout, last entry time. Any unknown value blocks the alert |
| Live trade watch | Read-only positions / orders / fills from TradingView's broker panel; urgent alert when a position has no stop |
| Journal | Every trade graded against your plan (A-D), optional Notion journal with your own "reason" and "emotion" fields read back |
| Tilt warnings | Losing streaks, fast re-entry after a loss, too many trades, instrument switching after a loss |
| Briefings | 5 minutes before Asia / London / NY AM: key levels, zones, events, account state |
| Research | Automatic walk-forward backtests of rule variants (net of fees), forward tests, opening-range-breakout system, daily research journal. Nothing changes your live rules without your approval |
| Ops | Watchdog restart, daily verified backups, keep-awake while monitoring, autostart at logon, local HTML dashboard |

## Requirements

- Windows 10/11, Python 3.12+
- TradingView Desktop (real-time data for your instruments recommended; delayed data is detected and blocks alerts)
- A Telegram bot (free) for alerts; Notion is optional
- Optional: a broker connected inside TradingView (e.g. Tradovate) for read-only account data

## Manual setup (short version)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest
```
Then follow the numbered steps in [AGENTS.md](AGENTS.md#setup-steps-windows-1011): TradingView with
`--remote-debugging-port=9222` and a 5m + 1m layout, `config/account.toml`, Telegram token via
`scripts\SET_TELEGRAM_TOKEN.cmd`, start with `scripts\start_monitor.ps1 -EnsureTradingView`.

## Layout

```
config/        strategy.toml (your rules), account.toml (firm + personal limits), account_state.toml
scripts/       start / stop / autostart, token setup (clipboard -> user environment, never in files)
src/copilot/   the system (see AGENTS.md "Code map")
tests/         pytest suite
data/          created at runtime: database, logs, reports, backups (git-ignored)
```

## Disclaimer

For education and personal decision support only. Not financial advice. Futures trading carries a high risk of
loss. Backtests and forward tests do not guarantee future results. You are responsible for every trade you place.
