"""Market data records. Every record carries source, fetch time, timeframe and data quality."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DataQuality(str, Enum):
    REALTIME = "REALTIME"      # feed reports no delay and the latest bar is current
    DELAYED = "DELAYED"        # feed reports a delay (e.g. TradingView *_DL symbols, 600s)
    STALE = "STALE"            # feed claims realtime but the latest bar is older than it should be
    UNKNOWN = "UNKNOWN"        # we could not determine delay/freshness -> treat as unusable for live decisions

    @property
    def live_usable(self) -> bool:
        """Only REALTIME data may drive live setup/trade alerts. Everything else fails closed."""
        return self is DataQuality.REALTIME


# TradingView resolution string -> bar length in seconds. Unknown resolutions are rejected, not guessed.
RESOLUTION_SECONDS = {"1": 60, "3": 180, "5": 300, "15": 900, "30": 1800, "60": 3600, "240": 14400}


@dataclass(frozen=True)
class Bar:
    time: int            # bar OPEN time, unix seconds UTC (as TradingView reports it)
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool      # False for the still-forming bar; forming bars are never used as confirmed data


@dataclass(frozen=True)
class SymbolMeta:
    full_name: str               # e.g. COMEX_MINI_DL:MGC1!
    pro_name: str | None         # e.g. COMEX_MINI:MGC1! (the non-delayed name)
    root: str                    # e.g. MGC1!
    exchange: str | None
    instrument_type: str | None
    delay_seconds: int | None    # None = TradingView did not report it -> UNKNOWN quality
    min_tick: float | None       # minmov / pricescale
    point_value: float | None    # $ per 1.0 price move per contract
    session: str | None
    timezone: str | None
    currency: str | None


@dataclass
class ChartSnapshot:
    source: str                  # "tradingview_desktop_cdp"
    fetched_at: int              # unix seconds UTC, from the page clock (Date.now()) at capture time
    symbol: SymbolMeta
    resolution: str
    chart_type: int | None
    studies: list[dict]          # [{id, name}] -- indicator discovery is Phase 2
    bars: list[Bar]
    data_quality: DataQuality
    quality_reasons: list[str] = field(default_factory=list)
    pane_index: int | None = None  # position in the TradingView layout; None = read via the "active chart" API
