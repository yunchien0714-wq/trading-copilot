"""Read the active TradingView chart: symbol metadata, resolution, studies, bars.

The in-page paths below are the ones the TradingView MCP verified by live probing
(window.TradingViewApi._activeChartWidgetWV ...). They are undocumented internals and may break
on a TradingView update -- failures surface as EvaluationError, never as made-up values.
"""
from __future__ import annotations

from typing import Callable

from ..market_data.models import ChartSnapshot, SymbolMeta
from ..market_data.quality import assess, mark_closed

SOURCE = "tradingview_desktop_cdp"
MAX_BARS = 10000

# Bars to have loaded per resolution. 5m: ~4 weeks (previous week + 6-session time-of-day volume).
# 1m: ~6 days (previous day + 6-session time-of-day volume needs ~8,300 1m bars; we accept UNKNOWN if short).
HISTORY_TARGET = {"5": 6000, "1": 9000}

# Loads older history into each pane (series.requestMoreData). Prepending bars shifts TradingView's view back
# in time (it keeps logical bar indices), which was observed -- so the view is captured first and
# restored afterwards: back to the latest bars if the user was watching realtime, else the same time window.
ENSURE_HISTORY_JS = """
(async function(targets) {
  var api = window.TradingViewApi, ws = api._chartWidgetCollection.getAll(), out = [];
  for (var p = 0; p < ws.length; p++) {
    var m = ws[p].model(), s = m.mainSeries();
    var res = s.properties().childs().interval.value(), target = targets[res] || 0, rounds = 0;
    var b0 = s.bars(), vr = api.chart(p).getVisibleRange();
    var sec = (s.bars().size() > 1) ? (b0.valueAt(b0.lastIndex())[0] - b0.valueAt(b0.lastIndex() - 1)[0]) : 60;
    var atRealtime = vr.to >= b0.valueAt(b0.lastIndex())[0] - 2 * sec;
    var nVisible = Math.max(30, Math.round((vr.to - vr.from) / sec));
    while (s.bars().size() < target && s.requestMoreDataAvailable() && rounds < 30) {
      var n0 = s.bars().size();
      s.requestMoreData(Math.min(2000, target - n0));
      for (var t = 0; t < 40 && s.bars().size() === n0; t++) await new Promise(function(r){ setTimeout(r, 250); });
      rounds++;
      if (s.bars().size() === n0) break;
    }
    if (rounds > 0) {
      var b = s.bars(), end = b.lastIndex();
      if (atRealtime) {
        m.timeScale().zoomToBarsRange(end - nVisible + 1, end + 3);
      } else {
        var fromIdx = null, toIdx = end;
        for (var i = b.firstIndex(); i <= end; i++) {
          var t = b.valueAt(i)[0];
          if (fromIdx === null && t >= vr.from) fromIdx = i;
          if (t <= vr.to) toIdx = i;
        }
        m.timeScale().zoomToBarsRange(fromIdx === null ? end - nVisible + 1 : fromIdx, toIdx);
      }
    }
    out.push({pane: p, resolution: res, bars: s.bars().size(), more_available: s.requestMoreDataAvailable(),
              view_restored: rounds > 0 ? (atRealtime ? 'realtime' : 'same_window') : 'unchanged'});
  }
  return out;
})(%s)
"""


def ensure_history(evaluate_async: Callable[[str], list], targets: dict | None = None) -> list[dict]:
    import json
    return evaluate_async(ENSURE_HISTORY_JS % json.dumps(targets or HISTORY_TARGET))

CHART_API = "window.TradingViewApi._activeChartWidgetWV.value()"

# One round trip, so symbol, bars and page clock all come from the same instant.
SNAPSHOT_JS = """
(function(maxBars) {
  var chart = %s;
  var series = chart._chartWidget.model().mainSeries();
  var si = null; try { si = series.symbolInfo(); } catch (e) {}
  var studies = [];
  try { studies = chart.getAllStudies().map(function(s){ return {id: s.id, name: s.name || s.title || null}; }); } catch (e) {}
  var b = series.bars(), out = [];
  if (b && typeof b.lastIndex === 'function' && b.size() > 0) {
    var end = b.lastIndex(), start = Math.max(b.firstIndex(), end - maxBars + 1);
    for (var i = start; i <= end; i++) {
      var v = b.valueAt(i);
      if (v) out.push({time: v[0], open: v[1], high: v[2], low: v[3], close: v[4], volume: v[5]});
    }
  }
  var ct = null; try { ct = chart.chartType(); } catch (e) {}
  return {
    now_ms: Date.now(),
    symbol: chart.symbol(),
    resolution: chart.resolution(),
    chart_type: ct,
    studies: studies,
    symbol_info: si ? {
      full_name: si.full_name, pro_name: si.pro_name, name: si.name, exchange: si.exchange,
      type: si.type, delay: si.delay, minmov: si.minmov, pricescale: si.pricescale,
      pointvalue: si.pointvalue, session: si.session, timezone: si.timezone, currency_code: si.currency_code
    } : null,
    bars: out
  };
})(%d)
"""


# Every pane in the layout, each read from its own chart widget. The "active chart" API only
# follows one pane (verified: it kept returning the 5m pane after focusing the 1m pane).
PANES_JS = """
(function(maxBars) {
  var ws = window.TradingViewApi._chartWidgetCollection.getAll();
  var now = Date.now();
  return ws.map(function(w, idx) {
    var series = w.model().mainSeries();
    var props = series.properties().childs();
    var si = null; try { si = series.symbolInfo(); } catch (e) {}
    var studies = [];
    try {
      w.model().model().dataSources().forEach(function(s) {
        if (!s.metaInfo || !s.id) return;
        var id = s.id();
        if (String(id).indexOf('ESD$') === 0) return;  // built-in dividends/splits/earnings/rolldates markers
        var mi = s.metaInfo(), vis = null;
        try { vis = s.properties().childs().visible.value(); } catch (e) {}
        studies.push({id: id, name: mi.description || mi.shortDescription || null, visible: vis});
      });
    } catch (e) {}
    var b = series.bars(), out = [];
    if (b && typeof b.lastIndex === 'function' && b.size() > 0) {
      var end = b.lastIndex(), start = Math.max(b.firstIndex(), end - maxBars + 1);
      for (var i = start; i <= end; i++) {
        var v = b.valueAt(i);
        if (v) out.push({time: v[0], open: v[1], high: v[2], low: v[3], close: v[4], volume: v[5]});
      }
    }
    var ct = null; try { ct = props.style.value(); } catch (e) {}
    return {
      pane_index: idx,
      now_ms: now,
      symbol: (si && si.full_name) || props.symbol.value(),
      resolution: props.interval.value(),
      chart_type: ct,
      studies: studies,
      symbol_info: si ? {
        full_name: si.full_name, pro_name: si.pro_name, name: si.name, exchange: si.exchange,
        type: si.type, delay: si.delay, minmov: si.minmov, pricescale: si.pricescale,
        pointvalue: si.pointvalue, session: si.session, timezone: si.timezone, currency_code: si.currency_code
      } : null,
      bars: out
    };
  });
})(%d)
"""


def build_snapshot_js(max_bars: int = MAX_BARS) -> str:
    return SNAPSHOT_JS % (CHART_API, int(max_bars))


def build_panes_js(max_bars: int = MAX_BARS) -> str:
    return PANES_JS % int(max_bars)


def _symbol_meta(raw: dict) -> SymbolMeta:
    si = raw.get("symbol_info") or {}
    minmov, pricescale = si.get("minmov"), si.get("pricescale")
    delay = si.get("delay")
    return SymbolMeta(
        full_name=si.get("full_name") or raw["symbol"],
        pro_name=si.get("pro_name"),
        root=si.get("name") or raw["symbol"].split(":")[-1],
        exchange=si.get("exchange"),
        instrument_type=si.get("type"),
        delay_seconds=int(delay) if isinstance(delay, (int, float)) else None,
        min_tick=(minmov / pricescale) if minmov and pricescale else None,
        point_value=si.get("pointvalue"),
        session=si.get("session"),
        timezone=si.get("timezone"),
        currency=si.get("currency_code"),
    )


def parse_snapshot(raw: dict) -> ChartSnapshot:
    if not raw:
        raise ValueError("empty response from TradingView page")
    now = int(raw["now_ms"]) // 1000
    meta = _symbol_meta(raw)
    resolution = str(raw["resolution"])
    bars = mark_closed(raw.get("bars") or [], resolution, now, meta.delay_seconds)
    quality, reasons = assess(bars, resolution, now, meta.delay_seconds, meta.full_name)
    if raw.get("symbol_info") is None:
        reasons.append("symbolInfo unavailable; tick size / point value / delay are UNKNOWN")
    return ChartSnapshot(SOURCE, now, meta, resolution, raw.get("chart_type"),
                         raw.get("studies") or [], bars, quality, reasons, raw.get("pane_index"))


def read_snapshot(evaluate: Callable[[str], dict], max_bars: int = MAX_BARS) -> ChartSnapshot:
    return parse_snapshot(evaluate(build_snapshot_js(max_bars)))


def read_panes(evaluate: Callable[[str], list], max_bars: int = MAX_BARS) -> list[ChartSnapshot]:
    raw = evaluate(build_panes_js(max_bars))
    if not raw:
        raise ValueError("TradingView returned no chart panes")
    return propagate_inferred_realtime([parse_snapshot(p) for p in raw])


def propagate_inferred_realtime(panes: list[ChartSnapshot]) -> list[ChartSnapshot]:
    """A 5m pane cannot prove its own freshness; it inherits REALTIME only from a 1m pane on the SAME feed
    symbol that did (and only when its own delay is unreported too and its bars are current)."""
    from ..market_data.models import DataQuality, RESOLUTION_SECONDS
    proven = {p.symbol.full_name for p in panes
              if p.resolution == "1" and p.data_quality is DataQuality.REALTIME and p.symbol.delay_seconds is None}
    for p in panes:
        if (p.data_quality is DataQuality.UNKNOWN and p.symbol.delay_seconds is None
                and p.symbol.full_name in proven and p.bars
                and 0 <= p.fetched_at - p.bars[-1].time <= RESOLUTION_SECONDS.get(p.resolution, 0) + 90):
            p.data_quality = DataQuality.REALTIME
            p.quality_reasons = [f"delay not reported; realtime INFERRED from the 1m pane on the same feed "
                                 f"({p.symbol.full_name}); newest {p.resolution}m bar is current"]
    return panes


CONTEXT_RES, EXECUTION_RES = "5", "1"


def _instrument(s: ChartSnapshot) -> str:
    # pro_name is the same for delayed and realtime feeds (COMEX_MINI:MGC1!); fall back to full_name.
    return s.symbol.pro_name or s.symbol.full_name


def check_mtf_layout(panes: list[ChartSnapshot]) -> dict:
    """Pair a 5m context pane with a 1m execution pane on the same instrument.

    Returns {"ok": bool, "issues": [...], "pairs": {instrument: {"5": pane_index, "1": pane_index}}}.
    A pair is only usable when both timeframes exist for the same instrument; mixing NQ context
    with a GC trigger would be silently wrong, so mismatches are reported, never auto-paired.
    """
    by_inst: dict[str, dict[str, int]] = {}
    issues: list[str] = []
    for s in panes:
        slot = by_inst.setdefault(_instrument(s), {})
        if s.resolution in slot:
            issues.append(f"{_instrument(s)} has two {s.resolution}m panes ({slot[s.resolution]} and {s.pane_index})")
        slot[s.resolution] = s.pane_index
    pairs = {inst: tf for inst, tf in by_inst.items() if CONTEXT_RES in tf and EXECUTION_RES in tf}
    for inst, tf in by_inst.items():
        if inst in pairs:
            continue
        have = ", ".join(f"{r}m" for r in sorted(tf))
        need = [f"{r}m" for r in (CONTEXT_RES, EXECUTION_RES) if r not in tf]
        issues.append(f"{inst}: has {have}, missing {' and '.join(need)} -> no 5m/1m pair")
    return {"ok": bool(pairs) and not issues, "issues": issues, "pairs": pairs}
