# Instructions for AI assistants setting up this project

You (the AI assistant) have been given this repository by a trader who wants their own copy of this trading
co-pilot running. Your job is to set it up **for them, around their own strategy and account**, step by step.
Talk to the trader in **their** language. Ask instead of guessing.

## Your very first message

Before installing or changing anything, ask the trader to describe their trading strategy. Use this question
(translate it to their language):

> Before I set anything up, please describe how you trade, in your own words:
> 1. Which markets / contracts (e.g. MNQ, MGC, NQ, GC) and do you trade micros or minis?
> 2. Which chart timeframes do you use for direction (bias) and for the entry?
> 3. How do you decide the direction (e.g. market structure / BOS-CHoCH, a moving average, higher-timeframe levels)?
> 4. Where do you enter (e.g. fair value gap, order block, volume POC, previous day high/low) and what must happen
>    before you click (e.g. a rejection candle, a close back inside, a liquidity sweep)?
> 5. Where is your stop, and where is your target (fixed R multiple, next liquidity, a level)?
> 6. Which hours / sessions do you trade, and when do you never trade (news, lunch, ...)?
> 7. Do you also take counter-trend / reversal trades? What makes one valid?
>
> If you don't have a fixed strategy yet, I will start with a common ICT model (5-minute structure for bias,
> London and New York AM killzones, entry on a 1-minute rejection inside a 5-minute fair value gap, 2R target).

Then:
1. Summarise their strategy back as numbered rules and get a "yes, that's it".
2. Map it onto `config/strategy.toml` (see "Strategy settings" below). If something they do is not supported by the
   engine, say so plainly, use the closest supported setting, and offer to implement it (as a research variant
   first -- see "Research"). Never pretend a rule is enforced when it isn't.
3. Only then continue with the setup steps.

If they have no strategy, keep the shipped `config/strategy.toml` (common ICT model) and tell them what it does.

## Non-negotiable rules (apply to everything you do in this project)

- **The system never places, modifies or cancels orders.** Broker access is read-only (TradingView's trading panel is
  only read). Do not add order code, ever, even if asked -- explain why instead.
- **Never invent data.** Unknown values stay `"UNKNOWN"`; the risk gate fails closed on them, so no trade alert is
  sent until the trader supplies the real numbers. Note the source of every value in a comment.
- **Secrets never go into files, code, logs or chat.** Telegram / Notion tokens are saved only in the Windows user
  environment through the clipboard scripts in `scripts/` (`SET_TELEGRAM_TOKEN.cmd`, `SET_NOTION_TOKEN.cmd`).
  If the trader pastes a token into chat, tell them to revoke it and create a new one.
- **Research never changes the live strategy.** A new rule reaches `config/strategy.toml` only after it passed the
  backtest AND the forward test AND the trader explicitly agreed.
- Don't click "connect", accept terms, enter passwords or create accounts on the trader's behalf -- they do that.
- Don't delete the trader's own chart drawings.

## Setup steps (Windows 10/11)

1. **Python 3.12+** -- `python --version`. In the repo root:
   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python -m pytest
   ```
   All tests must pass (a few are skipped: they need data that only exists on a running installation).
2. **TradingView Desktop** (Microsoft Store or tradingview.com) must run with the Chrome DevTools port:
   `TradingView.exe --remote-debugging-port=9222` (`scripts\start_monitor.ps1 -EnsureTradingView` does this).
   - Make a chart layout with **two panes of the same instrument**: 5-minute (left) and 1-minute (right), continuous
     contract (e.g. `MNQ1!`). Put the layout id from the URL (`tradingview.com/chart/<id>/`) into
     `config/account.toml` `[tradingview] primary_layout`.
   - More instruments for research: open them in another tab (5m + 1m panes); those tabs are only read for bars.
   - Real-time data matters: delayed data is detected and blocks trade alerts.
   - Optional: connect the broker in TradingView's trading panel (e.g. Tradovate) -- the trader does this themselves.
     It gives the system read-only balance, positions and fills.
3. **Account & risk** -- fill `config/account.toml` with the trader (firm rules, drawdown type, contract limits,
   personal limits, commission). Every value from the trader or the firm's written rules; ask for screenshots /
   rule text when needed. Set `[instruments] traded` and `[research] roots`.
   The monitor refuses to start (and lists the missing keys) while a required number is still `UNKNOWN`.
4. **Telegram alerts** -- the trader creates a bot with @BotFather, copies the token, double-clicks
   `scripts\SET_TELEGRAM_TOKEN.cmd`, sends any message to the bot, then: `python -m copilot telegram-setup`
   (prints the chat id) -> they set `COPILOT_TELEGRAM_CHAT_ID` in their user environment -> `python -m copilot telegram-test`.
   (Run CLI commands from `src\` with the venv python: `cd src` then `..\.venv\Scripts\python -m copilot <command>`.)
5. **Notion journal (optional)** -- the trader creates an internal integration (notion.so/my-integrations), shares a
   page with it, copies the secret, runs `scripts\SET_NOTION_TOKEN.cmd`, then
   `python -m copilot notion-setup --parent <page id>` creates the 交易日誌 and 研究日誌 databases; put the printed ids
   into `[notion]`.
6. **Start** -- `powershell -ExecutionPolicy Bypass -File scripts\start_monitor.ps1 -EnsureTradingView`
   (log: `data\logs\monitor.log`). Autostart at logon: `scripts\install_autostart.ps1`. Stop: `scripts\stop_monitor.ps1`.
7. **Verify** -- heartbeat lines in the log every 10 min (`broker connected`, `data 5m REALTIME`), a test Telegram
   message, `python -m copilot dashboard --open` for the local dashboard.

## Strategy settings (`config/strategy.toml`)

`[rules]` keys map 1:1 to `RuleSet` in `src/copilot/setup_engine/poc_fvg.py` (unknown keys stop the program):

| key | meaning |
|---|---|
| `use_structure` | trade with the 5m market structure (SMC BOS/CHoCH replica) |
| `allow_reversal` | counter-structure trades allowed when price retraces into the zone and a 1m candle rejects it |
| `setup_types` | `["TREND", "REVERSAL"]` or only one of them |
| `require_poc` | `true`: zone = 5m FVG overlapping a volume-profile POC; `false`: any 5m FVG (ICT) |
| `use_volume_support` | volume-profile / candle-volume filter on the zone |
| `use_opposing_fvg` | skip when an opposing 5m FVG sits between entry and target |
| `confirmations` | `"REJECTION"` (1m wick into the zone, close back out) and/or `"HOLD"` (N 1m closes holding the zone) |
| `hold_bars` | N for `HOLD` |
| `rejection_wick_frac` | minimum rejection wick as a fraction of the 1m bar |
| `target_mode` / `target_r` | `"fixed"` with `target_r` (e.g. 2.0) or `"structural"` (next volume node / opposing FVG) |
| `sessions` | killzones allowed for the confirmation: `ASIA` 20-24, `LONDON` 02-05, `NY_AM` 09:30-11, `NY_PM` 13:30-16 (ET); omit = any time |
| `min_fvg_atr` / `max_fvg_atr` | FVG size limits in 5m ATR (0 = off) |
| `retest_reversal`, `reversal_fvg_only` | FVG second-test reversal; reversals may use an FVG without POC |

`[plan]` holds extra rules used only for grading the trader's own trades. Stop = beyond the confirming candle's
wick (never inside the FVG). After editing, restart the monitor.

A strategy that needs a different engine (e.g. pure opening-range breakout, VWAP) can be built like
`src/copilot/research/orb.py`: research first, alerts only after it proves itself and the trader agrees.

## Research (runs by itself)

- `research/auto.py` keeps generating and backtesting variants of the live rules on the stored bars (walk-forward,
  R net of commission, paired-day bootstrap). Viable = >= 30 out-of-sample trades, > +0.10R in both halves, better
  than the live rules, P >= 0.95.
- Passing variants are forward-tested on days after their selection (`research/shadow.py`, replay of stored MGC/MNQ
  days): >= 30 trades over >= 10 trading days. Passing that -> a PROPOSAL on Telegram; the trader decides.
- `research/orb.py` tests a second system (opening-range breakout) on 5m and 15m bars.
- Daily research journal: Notion 研究日誌 + dashboard. Nothing here touches live alerts.

## Code map

`src/copilot/`: `monitor.py` (main loop), `tradingview/` (CDP reads), `market_structure/` (SMC replica, swings),
`fvg/`, `volume/` (profile / POC), `setup_engine/` (rules + tracker), `risk_engine/gate.py` (fail-closed gate),
`trade_monitor/` + `broker/` (read-only positions, fills), `trade_journal/` (grader, Notion, tilt warnings),
`notifications/` (Telegram, commands), `reports/` (dashboard, pre-session briefing), `research/`, `ops/`
(watchdog, backups, release log), `session_engine/` (ET trading day, killzones).
Messages to the trader (Telegram, Notion) are in Traditional Chinese (`trade_journal/zh.py`,
`notifications/formatter.py`); translate them if the trader prefers another language.

Tests: `python -m pytest` from the repo root. Keep them green; add a test with every change.
