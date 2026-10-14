"""Manual source reviews of the indicators on the user's chart (Phase 2).

Each review is bound to the script's pine digest. If TradingView reports a different digest
(the author updated the script), the review is STALE and must be redone -- it is not reused.

Evidence classes (spec section 4):
  verified   -- follows from the source code itself (line numbers given)
  empirical  -- observed on the user's live chart (how/when given)
  hypothesis -- plausible, not yet tested
  unknown    -- cannot be determined yet (e.g. no trade history to correlate against)
"""
from __future__ import annotations

from dataclasses import dataclass, field

REVIEW_VERSION = "indicator_review-1.0"
REVIEWED_AT = "initial review"

NO_TRADE_HISTORY = ("unknown", "Correlation with the user's successful trades: UNKNOWN -- no trade history recorded yet "
                               "(Phase 9-11).")


@dataclass(frozen=True)
class Review:
    name: str
    script_id: str
    digest: str
    calculates: str
    repaints: str                 # NO | INTRABAR_ONLY | YES
    lookahead: str                # NO | YES
    signal_delay: str
    roles: dict                   # role -> USEFUL | WEAK | NOT_SUITABLE
    findings: list = field(default_factory=list)   # (evidence_class, text)
    duplicates: list = field(default_factory=list)
    recommendation: str = ""


REVIEWS = [
    Review(
        name="ICT Killzones & Pivots [TFO]",
        script_id="PUB;d0b8be94f1b042bf9f2cb20f92cefc57",
        digest="a053f638a94970249d4f67c9a65c0b18dfef3575",
        calculates="Session (killzone) boxes and their highs/lows in America/New_York time (Asia 20:00-00:00, London "
                   "02:00-05:00, NY AM 09:30-11:00, NY PM 13:30-16:00), previous day/week/month high/low/mid built by "
                   "aggregating chart bars between timeframe.change() boundaries, opening-price lines, and hit-rate "
                   "statistics for those levels.",
        repaints="INTRABAR_ONLY",
        lookahead="NO",
        signal_delay="None: levels are known as soon as the session/period closes; the live session box grows by design.",
        roles={"context": "USEFUL", "direction": "NOT_SUITABLE", "entry": "NOT_SUITABLE",
               "exit": "WEAK", "risk": "WEAK", "confirmation": "NOT_SUITABLE"},
        findings=[
            ("verified", "No request.security and no lookahead; all levels come from bars already closed (lines 470-481)."),
            ("verified", "'Previous day' resets on timeframe.change('D'), i.e. the exchange session (18:00 ET for CME/COMEX), "
                         "not the calendar day -- correct for futures."),
            ("verified", "Hit-rate % in the table is a descriptive count over at most data_lookback=20 sessions of loaded "
                         "history (line 103, 977). It is not a forecast and the sample is small."),
            ("hypothesis", "Previous week/month levels need that whole period in the script's history. If TradingView "
                           "gives the script less history than a full week (especially on the 1m pane), PWH/PWL could be "
                           "computed from a partial week. To verify in Phase 3 by recomputing from our own stored bars."),
            NO_TRADE_HISTORY,
        ],
        duplicates=["Session and previous-day levels are exactly what our session_engine/support_resistance modules "
                    "must compute (spec section 5). Keep the indicator as an independent cross-check source."],
        recommendation="KEEP on both panes as context. Use as a second source for PDH/PDL/session levels and flag "
                       "disagreements with our own computation.",
    ),
    Review(
        name="Support and Resistance Logistic Regression | Flux Charts",
        script_id="PUB;9d844269d6c9493594d64c11b59ebb81",
        digest="a7d663bb47dd892f470d79299df297c7770333c2",
        calculates="Swing pivots (ta.pivothigh/low, length 14) become S/R lines; a line counts a 'respect' each "
                    "closed bar that wicks through and closes back, and ends when a bar closes through. A "
                    "self-described logistic-regression model scores each new pivot from two binary features "
                    "(RSI>50, body>ATR) and only pivots scoring >= 70% are drawn, with that score as a % label.",
        repaints="NO",
        lookahead="NO",
        signal_delay="14 bars (70 min on 5m, 14 min on 1m) before a pivot exists; the line is then drawn back from "
                     "the pivot bar, so history shows it earlier than it was knowable.",
        roles={"context": "WEAK", "direction": "NOT_SUITABLE", "entry": "NOT_SUITABLE",
               "exit": "WEAK", "risk": "WEAK", "confirmation": "NOT_SUITABLE"},
        findings=[
            ("verified", "Pivot confirmation lag of pivotLength=14 bars; line anchored at time[14] (lines 113-121, 212)."),
            ("verified", "The 'logistic regression' is not a valid one: labels are +1/-1 instead of 1/0 (line 82); the "
                         "weight update uses (p + loss) instead of the log-loss gradient (p - y) (lines 89-90); the "
                         "intercept is never trained (line 65); weights restart from 1.0 for every new pivot (lines 65-67)."),
            ("verified", "Features are binary (+/-1), so the model can output at most 4 distinct scores per training "
                         "state. With the starting weights the scores are 95%, 73% or 27% -- the % label is not a "
                         "calibrated probability that the level will hold."),
            ("verified", "Respect counting increments on every qualifying bar with no cooldown (line 162), so one slow "
                         "multi-bar test can count as several 'respects'."),
            ("verified", "Pivot length also sets the RSI and ATR lengths (lines 116, 125), so they cannot be tuned independently."),
            NO_TRADE_HISTORY,
        ],
        duplicates=["Swing-high/low levels overlap with Breakout Volume Delta's swing levels and with our own "
                    "market_structure engine (Phase 3)."],
        recommendation="Treat the drawn lines as ordinary swing S/R only. IGNORE the % labels. Candidate for removal "
                       "from the 1m pane once our own swing/S&R engine exists.",
    ),
    Review(
        name="PoC Migration Map [BackQuant]",
        script_id="PUB;365f301014324b8fa22167773d3254ff",
        digest="442ea640a34c5ee96d5ae284e94cb6dc00bab26e",
        calculates="On the last bar only: a 30-bin histogram of volume by CLOSE price over the last 300 bars, plus "
                   "the highest-volume bin (PoC) of each 10-bar segment for the last 20 segments, joined by lines and "
                   "coloured by EMA(12) vs EMA(21).",
        repaints="YES",
        lookahead="NO",
        signal_delay="Not applicable: no historical signals exist, only a snapshot of the current window.",
        roles={"context": "WEAK", "direction": "NOT_SUITABLE", "entry": "NOT_SUITABLE",
               "exit": "NOT_SUITABLE", "risk": "NOT_SUITABLE", "confirmation": "NOT_SUITABLE"},
        findings=[
            ("verified", "Everything runs inside 'if barstate.islast' (line 55): the map is recomputed from a rolling "
                         "300-bar window each bar and nothing is stored per bar, so it cannot be backtested and past "
                         "states cannot be reconstructed."),
            ("verified", "Segments are counted back from the current bar (line 127), so every segment boundary shifts by "
                         "one bar on every new bar and each 'PoC n' can move."),
            ("verified", "All of a bar's volume goes to the bin of its close (lines 74-82), not spread across its "
                         "high-low range: a close-weighted histogram, not a true volume profile. Bins are range/30 wide "
                         "and change as the 300-bar range changes."),
            ("verified", "Boxes and lines are created every recompute and never deleted (no .delete calls)."),
            ("empirical", "Review session via CDP: the 5m pane held 204 PoC boxes and 114 lines = 6 stacked copies "
                          "of one map (19 lines each); the freshly reloaded 1m pane held 1 copy (37 boxes, 19 lines). "
                          "Matches the overlapping volume text in the user's screenshot."),
            ("verified", "The window is 300 bars on both panes: about 25 hours on 5m but only 5 hours on 1m, so the two "
                         "panes show different profiles."),
            NO_TRADE_HISTORY,
        ],
        duplicates=["High-volume areas are better computed by our own volume module from stored bars (Phase 3), "
                    "with proper range distribution and a fixed session anchor."],
        recommendation="Context glance only. Do not use for signals, backtests or level drawing. Recommend REMOVING "
                       "from the 1m pane (largest clutter source; 5-hour window is too short to be meaningful).",
    ),
    Review(
        name="Breakout Volume Delta | Flux Charts",
        script_id="PUB;aa1f1df5f3674130a549581bf813d403",
        digest="fb34de09734ae4b4cc9843b03e94860d6a8d5422",
        calculates="Swing highs/lows (10 left / 10 right bars), keeps the 5 most recent of each, marks a breakout "
                   "when a bar closes beyond one, and colours the breakout candle by the share of 1-minute volume "
                   "that came from up-close vs down-close 1m bars.",
        repaints="INTRABAR_ONLY",
        lookahead="NO",
        signal_delay="Swing levels exist only 10 bars after the swing (50 min on 5m, 10 min on 1m); breakouts are "
                     "final only at bar close.",
        roles={"context": "WEAK", "direction": "WEAK", "entry": "NOT_SUITABLE",
               "exit": "NOT_SUITABLE", "risk": "WEAK", "confirmation": "WEAK"},
        findings=[
            ("verified", "'Volume delta' = sum of volume of 1m bars with close>open vs close<open (lines 116-134). "
                         "This is not bid/ask aggressor delta; doji bars are dropped entirely."),
            ("verified", "With the default LTF '1' on a 1m chart, each bar has exactly one sub-bar, so the split can only "
                         "be 0%, 100% or 50% -- the delta carries no information on the 1m pane."),
            ("empirical", "Chart screenshots: 1m pane labels '284 (100%) | 0 (0%)', '0 (0%) | 183 (100%)', "
                          "'93 (100%) | 0 (0%)'; 5m pane label '162 (12.3%) | 1.15K (87.7%)'."),
            ("verified", "The input is labelled 'Show Nearest' but keeps the 5 most RECENT levels, not the nearest to "
                         "price (fTrim drops the oldest, lines 148-156; its tooltip does say 'most recent')."),
            ("verified", "Breakout = close beyond the level. A wick through and close back (a liquidity sweep) is not "
                         "flagged at all."),
            NO_TRADE_HISTORY,
        ],
        duplicates=["Swing levels overlap with the S/R Logistic Regression lines.",
                    "Its volume split is weaker than real order flow and overlaps TRVO as a volume read."],
        recommendation="5m pane: usable as a swing-break (BOS) marker; read the volume split as a rough proxy only. "
                       "1m pane: the delta is meaningless at LTF=1 -- either remove it there or set its LTF to a lower "
                       "timeframe (TradingView supports seconds only on some plans; UNKNOWN for this account).",
    ),
    Review(
        name="Time Relative Volume Oscillator | Flux Charts",
        script_id="PUB;c667f1d898e041e3b97146a1457fde7e",
        digest="08a42324dce7cdd3ddfba7d68c3cffaa4c4526f8",
        calculates="Relative volume = this bar's volume / average volume of the bar at the same position in each of "
                   "the previous 6 sessions. Classic mode (the user's setting) plots a sigmoid of that ratio scaled "
                   "by the 200-bar average volume.",
        repaints="INTRABAR_ONLY",
        lookahead="NO",
        signal_delay="None after bar close.",
        roles={"context": "USEFUL", "direction": "NOT_SUITABLE", "entry": "NOT_SUITABLE",
               "exit": "NOT_SUITABLE", "risk": "NOT_SUITABLE", "confirmation": "USEFUL"},
        findings=[
            ("verified", "Time-of-day matching uses bar offsets (volume[barsInSession * i], line 32), not timestamps. "
                         "Missing bars, holidays, early closes or the Sunday open shift the comparison to the wrong "
                         "time of day."),
            ("verified", "The sigmoid 4/(1+e^(3-2*RV)) - 0.18 (line 57) saturates near 3.82: relative volume above "
                         "about 3x looks almost the same as 5x or 10x."),
            ("verified", "Buy/sell split (only in Buy/Sell mode) is a candle-shape proxy (close position in the range, "
                         "line 35), not order flow."),
            ("verified", "The forming bar compares a partial bar's volume with full bars, so live values read low until "
                         "the bar closes. Our system only uses closed bars."),
            NO_TRADE_HISTORY,
        ],
        duplicates=[],
        recommendation="KEEP (5m pane; hidden on 1m by user choice). Best volume-confirmation input of the five. "
                       "Our volume module will compute the raw relative-volume ratio itself, without the sigmoid cap, "
                       "using timestamps for alignment.",
    ),
]

ORDER_FLOW_STATUS = ("No indicator on the chart provides real order flow (bid/ask aggressor volume, footprint, "
                     "absorption). Per spec section 2, order-flow features are UNKNOWN for this setup.")


def review_for(script_id: str, digest: str) -> tuple[Review | None, str]:
    """Return (review, status). status: CURRENT | STALE (script updated since review) | MISSING."""
    for r in REVIEWS:
        if r.script_id == script_id:
            return (r, "CURRENT") if r.digest == digest else (r, "STALE")
    return None, "MISSING"
