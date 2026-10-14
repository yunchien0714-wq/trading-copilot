"""READ-ONLY access to the user's broker (Tradovate) through TradingView's trading connection.

The co-pilot never trades. Only the methods in READ_METHODS are ever called; tests assert that no order /
position / account-modifying method name appears in the evaluated JavaScript.

Account summary rows have no stable ids (only localized labels), so values are matched by label; an
unrecognized label -> UNKNOWN (the risk gate then fails closed), never guessed by position.
Executions: until a real fill has been seen and its format verified, a non-empty execution list makes
trades_today UNKNOWN (empty list = 0 trades is certain).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

SOURCE = "TradingView broker connection (Tradovate)"
READ_METHODS = ["connectionStatus", "currentAccount", "currentAccountType", "accountsMetainfo",
                "positions", "orders", "allExecutions"]
FORBIDDEN = ["placeOrder", "modifyOrder", "cancelOrder", "cancelOrders", "closePosition", "closeIndividualPosition",
             "reversePosition", "editPositionBrackets", "editIndividualPositionBrackets", "createAccount",
             "resetAccount", "deleteAccount", "changeAccountSettings", "setCurrentAccount", "previewOrder",
             "createPlaceOrderContext"]
EXECUTIONS_FORMAT_VERIFIED = False   # flip only after checking a real fill (observed: none yet)

READ_JS = """
(async function(methods) {
  var ab = window.TradingViewApi.trading()._activeBroker;
  var b = (ab && typeof ab.value === 'function') ? ab.value() : ab;
  if (!b) return {connected: false};
  var out = {connected: true};
  for (var i = 0; i < methods.length; i++) {
    var n = methods[i];
    try { var r = b[n](); if (r && typeof r.then === 'function') r = await r; out[n] = r; }
    catch (e) { out[n + '_error'] = String(e && e.message || e); }
  }
  try {
    out.summary = (b.accountManagerInfo().summary || []).map(function(s) {
      var v = s.wValue; if (v && typeof v.value === 'function') v = v.value();
      return [s.text, v];
    });
  } catch (e) { out.summary_error = String(e && e.message || e); }
  try {   // the account page's table has unambiguous field ids (netLiq, openPL) -- independent of UI language
    var pages = b.accountManagerInfo().pages || [];
    for (var p = 0; p < pages.length; p++) {
      var tables = pages[p].tables || [];
      for (var t = 0; t < tables.length; t++) {
        if (tables[t].id === 'accountSummary') {
          var d = tables[t].getData(); if (d && typeof d.then === 'function') d = await d; out.account_table = d;
        }
      }
    }
  } catch (e) { out.account_table_error = String(e && e.message || e); }
  return JSON.parse(JSON.stringify(out));
})(%s)
"""


def build_read_js() -> str:
    return READ_JS % json.dumps(READ_METHODS)


@dataclass
class BrokerSnapshot:
    connected: bool
    fetched_at: int
    account_name: str | None = None
    account_type: str | None = None
    balance: float | None = None
    equity: float | None = None
    profit: float | None = None
    open_positions: int | None = None
    working_orders: int | None = None
    executions: list = field(default_factory=list)
    trades_today: int | None = None
    problems: list[str] = field(default_factory=list)
    source: str = SOURCE
    raw: dict = field(default_factory=dict, repr=False)   # positions / orders / executions for the trade monitor


CONNECTED = 1   # TradingView ConnectionStatus: 1 connected, 2 connecting, 3 disconnected, 4 error


def parse(raw: dict, now: int) -> BrokerSnapshot:
    if not raw or not raw.get("connected"):
        return BrokerSnapshot(False, now, problems=["no broker connected in TradingView"])
    # A broker object can exist while its session is down (observed: status 3 was reported as connected).
    if raw.get("connectionStatus") != CONNECTED:
        return BrokerSnapshot(False, now, problems=[f"broker connection status {raw.get('connectionStatus')} (not connected)"])
    s = BrokerSnapshot(True, now)
    s.raw = {k: raw.get(k) for k in ("positions", "orders", "allExecutions")}
    metas = raw.get("accountsMetainfo") or []
    cur = raw.get("currentAccount")
    meta = next((m for m in metas if str(m.get("id")) == str(cur)), metas[0] if len(metas) == 1 else None)
    s.account_name = meta.get("name") if meta else None
    s.account_type = raw.get("currentAccountType")
    # equity = net liquidation value (moves with open P/L), balance = realized cash = netLiq - openPL.
    # Taken from the accountSummary table's field ids. The translated summary LABELS are NOT used for these: on
    # observed: the row labelled 帳戶餘額 ("balance") moved with open P/L during a trade and 權益 ("equity") stayed
    # flat -- the opposite of their names -- which had swapped the two values in every stored mark.
    row = (raw.get("account_table") or [None])[0] if isinstance(raw.get("account_table"), list) else None
    try:
        net_liq, open_pl = float(row["netLiq"]), float(row["openPL"])
        s.equity, s.balance, s.profit = net_liq, round(net_liq - open_pl, 2), open_pl
    except (TypeError, KeyError, ValueError):
        s.problems.append("account table (netLiq / openPL) unavailable -> balance and equity UNKNOWN"
                          + (f": {raw['account_table_error']}" if raw.get("account_table_error") else ""))
    pos = raw.get("positions")
    s.open_positions = sum(1 for p in pos if (p.get("qty") or 0) != 0) if isinstance(pos, list) else None
    orders = raw.get("orders")
    s.working_orders = len(orders) if isinstance(orders, list) else None
    ex = raw.get("allExecutions")
    if isinstance(ex, list):
        s.executions = ex
        s.trades_today = 0 if not ex else None     # counting rule for real fills is added once verified
        if ex and not EXECUTIONS_FORMAT_VERIFIED:
            s.problems.append("executions present but their format is not verified yet -> trades_today UNKNOWN")
    else:
        s.problems.append("executions unavailable")
    return s


def read(evaluate_async: Callable[[str], dict], now: int) -> BrokerSnapshot:
    return parse(evaluate_async(build_read_js()), now)


# ---------------------------------------------------------------- equity history -> trailing high-water mark
SCHEMA = """
CREATE TABLE IF NOT EXISTS equity_marks (
    at INTEGER NOT NULL, account TEXT NOT NULL, balance REAL, equity REAL, source TEXT NOT NULL
);
"""


def record(conn: sqlite3.Connection, snap: BrokerSnapshot) -> None:
    conn.executescript(SCHEMA)
    if snap.connected and snap.account_name and snap.equity is not None:
        conn.execute("INSERT INTO equity_marks VALUES (?,?,?,?,?)",
                     (snap.fetched_at, snap.account_name, snap.balance, snap.equity, snap.source))
        conn.commit()


GAP_SECONDS = 180   # balance changed across a longer observation gap -> trading happened unobserved


def load_anchor(path) -> tuple[float, int] | None:
    """(trailing_threshold_usd, unix time) from config/account_state.toml, if the user filled it in."""
    import tomllib
    from datetime import datetime
    try:
        with open(path, "rb") as f:
            d = tomllib.load(f)
    except OSError:
        return None
    thr, at = d.get("trailing_threshold_usd"), d.get("threshold_as_of")
    if isinstance(thr, (int, float)) and isinstance(at, datetime) and at.tzinfo is not None:
        return float(thr), int(at.timestamp())
    return None


def high_water(conn: sqlite3.Connection, account: str, account_size: float, snap: BrokerSnapshot,
               max_dd: float | None = None, anchor: tuple[float, int] | None = None) -> tuple[float | None, str]:
    """Highest equity for the trailing drawdown.

    Start point: either the user's anchor (trailing threshold read from Apex/Tradovate at time t -> high-water =
    threshold + max_dd) or an untouched account (first observation balance == equity == size).
    From there: max of observed equity. If the balance changed across an observation gap (> GAP_SECONDS), trades
    happened unobserved and their intraday peak is unknown -> UNKNOWN until the user supplies a newer anchor
    (observed: two trades were placed while the broker connection was down).
    """
    conn.executescript(SCHEMA)
    since = 0
    if anchor is not None and max_dd is not None:
        base, since, why = anchor[0] + max_dd, anchor[1], f"user anchor: trailing threshold {anchor[0]} + {max_dd}"
    else:
        first = conn.execute("SELECT balance, equity FROM equity_marks WHERE account = ? ORDER BY at LIMIT 1",
                             (account,)).fetchone()
        if first is None:
            return None, "no equity observations yet"
        if not (first[0] == account_size and first[1] == account_size):
            return None, "first observation was not an untouched account -> high-water mark UNKNOWN (supply anchor)"
        base, why = float(account_size), "starting size (account untouched when first seen)"
    from ..trade_journal.tradovate_csv import explain_gap
    marks = conn.execute("SELECT at, balance, equity FROM equity_marks WHERE account = ? AND at >= ? ORDER BY at",
                         (account, since)).fetchall()
    gap_peaks = []
    for (t0, b0, _), (t1, b1, _) in zip(marks, marks[1:]):
        if t1 - t0 > GAP_SECONDS and b0 is not None and b1 is not None and abs(b1 - b0) > 0.005:
            ok, peak, reason = explain_gap(conn, t0, t1, b0, b1)
            if not ok or peak is None:
                return None, (f"balance changed {b0} -> {b1} while unobserved ({(t1 - t0) // 60} min gap): {reason}. "
                              "Export Tradovate Performance to Documents, or supply the trailing threshold")
            gap_peaks.append(peak)
    peak = max([base] + gap_peaks + [m[2] for m in marks if m[2] is not None])
    return peak, f"max({why}, observed equity" + (", reconciled unobserved trades' bar MFE)" if gap_peaks else ")")


def trades_today(conn: sqlite3.Connection, account: str, day_start: int, manual: tuple[int, date] | None,
                 today: date) -> tuple[int | None, str]:
    """Trades in the current trading day = trades the monitor recorded + trades the user reported.

    TradingView's execution list only covers the current broker session, so an empty list does NOT mean no
    trades (observed: two trades placed while disconnected). If the balance changed unobserved during the
    trading day and the user has not reported a count for today -> UNKNOWN.
    """
    conn.executescript(SCHEMA)
    observed = 0
    if conn.execute("SELECT name FROM sqlite_master WHERE name='trades'").fetchone():
        observed = conn.execute("SELECT COUNT(*) FROM trades WHERE account = ? AND entry_time >= ?",
                                (account, day_start)).fetchone()[0]
    from ..trade_journal.tradovate_csv import explain_gap
    reported = manual[0] if manual and manual[1] == today else None
    marks = conn.execute("SELECT at, balance FROM equity_marks WHERE account = ? AND at >= ? ORDER BY at",
                         (account, day_start - 7 * 86400)).fetchall()
    before = [m for m in marks if m[0] < day_start]
    window = ([before[-1]] if before else []) + [m for m in marks if m[0] >= day_start]
    if not window:
        return (reported, "reported by user") if reported is not None else (None, "no observations this trading day")
    for (t0, b0), (t1, b1) in zip(window, window[1:]):
        if t1 - t0 > GAP_SECONDS and b0 is not None and b1 is not None and abs(b1 - b0) > 0.005:
            ok, _, reason = explain_gap(conn, t0, t1, b0, b1)
            if not ok:
                if reported is not None:
                    return observed + reported, f"{observed} recorded + {reported} reported by user ({reason})"
                return None, f"balance changed while unobserved today: {reason} -> count UNKNOWN"
    return observed, f"{observed} recorded (live or imported), all balance changes explained"


def load_reported_trades(path) -> tuple[int, date] | None:
    import tomllib
    try:
        with open(path, "rb") as f:
            d = tomllib.load(f)
    except OSError:
        return None
    n, day = d.get("unobserved_trades_today"), d.get("unobserved_trades_date")
    return (int(n), day) if isinstance(n, int) and isinstance(day, date) else None


def gate_state(snap: BrokerSnapshot, hw: float | None, today: date, n_trades: int | None = None) -> dict:
    """n_trades: from trades_today(); falls back to the broker's own (session-limited) count only if omitted."""
    n = n_trades if n_trades is not None else None
    return {"as_of": today if snap.connected else "UNKNOWN",
            "balance_usd": snap.equity if snap.equity is not None else "UNKNOWN",
            "high_water_usd": hw if hw is not None else "UNKNOWN",
            "trades_today": n if n is not None else "UNKNOWN",
            "open_positions": snap.open_positions if snap.open_positions is not None else "UNKNOWN"}
