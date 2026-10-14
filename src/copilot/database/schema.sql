-- Local source of truth. Notion is a downstream journal, never the store for live logic.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

-- One row per chart capture (what was on screen, how fresh it was).
CREATE TABLE IF NOT EXISTS chart_snapshots (
    id              INTEGER PRIMARY KEY,
    source          TEXT    NOT NULL,
    fetched_at      INTEGER NOT NULL,           -- unix s UTC (page clock)
    feed_symbol     TEXT    NOT NULL,           -- e.g. COMEX_MINI_DL:MGC1!  (delayed and realtime feeds are different symbols)
    pro_symbol      TEXT,
    resolution      TEXT    NOT NULL,
    delay_seconds   INTEGER,                    -- NULL = UNKNOWN
    data_quality    TEXT    NOT NULL CHECK (data_quality IN ('REALTIME','DELAYED','STALE','UNKNOWN')),
    quality_reasons TEXT    NOT NULL,           -- JSON array
    min_tick        REAL,
    point_value     REAL,
    studies         TEXT    NOT NULL,           -- JSON [{id,name}]
    bar_count       INTEGER NOT NULL,
    last_bar_time   INTEGER,
    last_close      REAL,
    last_bar_closed INTEGER,
    feature_version TEXT    NOT NULL,
    pane_index      INTEGER                     -- layout position; NULL = captured via the "active chart" API
);

-- Closed bars only. A forming bar is never persisted as fact.
CREATE TABLE IF NOT EXISTS bars (
    feed_symbol   TEXT    NOT NULL,
    resolution    TEXT    NOT NULL,
    time          INTEGER NOT NULL,             -- bar open, unix s UTC
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume        REAL    NOT NULL,
    source        TEXT    NOT NULL,
    first_seen_at INTEGER NOT NULL,
    data_quality  TEXT    NOT NULL,
    PRIMARY KEY (feed_symbol, resolution, time)
);

-- One row per indicator source version seen on the chart (Phase 2).
CREATE TABLE IF NOT EXISTS indicator_scans (
    id               INTEGER PRIMARY KEY,
    scanned_at       INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    script_id        TEXT,
    digest           TEXT,                      -- TradingView pine digest; changes when the author updates the script
    panes            TEXT    NOT NULL,          -- JSON [pane_index]
    source_status    TEXT    NOT NULL CHECK (source_status IN ('AVAILABLE','UNAVAILABLE')),
    status_reason    TEXT    NOT NULL,
    source_sha256    TEXT,
    static_hits      TEXT    NOT NULL,          -- JSON [{check,severity,lines,detail,evidence}]
    analyzer_version TEXT    NOT NULL,
    review_status    TEXT    NOT NULL CHECK (review_status IN ('CURRENT','STALE','MISSING')),
    review_version   TEXT
);

-- Same closed bar reported with different values later: flagged, never silently overwritten.
CREATE TABLE IF NOT EXISTS data_discrepancies (
    id          INTEGER PRIMARY KEY,
    detected_at INTEGER NOT NULL,
    feed_symbol TEXT    NOT NULL,
    resolution  TEXT    NOT NULL,
    time        INTEGER NOT NULL,
    stored      TEXT    NOT NULL,               -- JSON ohlcv
    observed    TEXT    NOT NULL,               -- JSON ohlcv
    source      TEXT    NOT NULL
);
