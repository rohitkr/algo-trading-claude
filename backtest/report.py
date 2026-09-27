"""Self-contained HTML report for a backtest run (inline SVG charts, no external data)."""
from __future__ import annotations

import html
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .engine import Metrics, Trade, metrics

CSS = """
:root{--bg:#f4f6f5;--surface:#ffffff;--ink:#15201b;--muted:#5b6a63;--rule:#d8dfdb;--accent:#1f5f8b;
--pos:#1b7a4b;--neg:#b5392d;--pos-soft:#e3f2ea;--neg-soft:#f8e5e2;--series2:#b7791f;--grid:#e7ece9}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1412;--surface:#151d1a;--ink:#e4ebe7;
--muted:#93a39b;--rule:#2a3531;--accent:#74aee0;--pos:#4cc38a;--neg:#f07a6b;--pos-soft:#15301f;--neg-soft:#3a1d19;
--series2:#e0a94a;--grid:#1f2926;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0e1412;--surface:#151d1a;--ink:#e4ebe7;--muted:#93a39b;--rule:#2a3531;--accent:#74aee0;
--pos:#4cc38a;--neg:#f07a6b;--pos-soft:#15301f;--neg-soft:#3a1d19;--series2:#e0a94a;--grid:#1f2926;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding-inline:20px;padding-block:28px 56px;display:grid;gap:28px}
h1,h2,h3{margin:0;text-wrap:balance;font-weight:600;letter-spacing:-.01em}
h1{font-size:clamp(24px,4vw,34px)}h2{font-size:20px}h3{font-size:16px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.muted{color:var(--muted)}
.num,.mono,td.n{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
header{display:grid;gap:8px}
header p{margin:0;max-width:72ch;color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
@media (max-width:640px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
.kpi{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:14px 16px;display:grid;gap:2px}
.kpi .v{font-size:24px;font-weight:600}
.pos{color:var(--pos)}.neg{color:var(--neg)}
section{display:grid;gap:14px}
.panel{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:16px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.chart{width:100%;height:auto;display:block}
.chart text{fill:var(--muted);font:11px "IBM Plex Mono",ui-monospace,monospace}
.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .zero{stroke:var(--rule);stroke-width:1.5}
.l-all{stroke:var(--ink);stroke-width:2.2;fill:none}
.l-a{stroke:var(--accent);stroke-width:1.6;fill:none}
.l-b{stroke:var(--series2);stroke-width:1.6;fill:none}
.b-pos{fill:var(--pos)}.b-neg{fill:var(--neg)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--muted)}
.legend i{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px;border-radius:2px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid var(--rule);text-align:left;white-space:nowrap}
th{font-weight:600;color:var(--muted);font-size:12px;letter-spacing:.03em}
td.n,th.n{text-align:right}
tr.re td:first-child::after{content:" re-entry";font-size:11px;color:var(--muted)}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;border:1px solid var(--rule)}
.chip.pos{background:var(--pos-soft);border-color:transparent}.chip.neg{background:var(--neg-soft);border-color:transparent}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px 20px}
.stats div{display:grid}.stats span:first-child{font-size:12px;color:var(--muted)}
ul.notes{margin:0;padding-left:18px;display:grid;gap:6px;max-width:80ch}
.heat td{text-align:center;font-family:"IBM Plex Mono",monospace;font-size:12px}
"""


def _fmt(x, sign=True):
    if x is None:
        return "–"
    s = f"{abs(x):,.0f}"
    return ("+" if x > 0 and sign else "−" if x < 0 else "") + "₹" + s


def _cls(x):
    return "pos" if x and x > 0 else "neg" if x and x < 0 else ""


def _equity_svg(series: list[tuple[str, list[tuple[datetime, float]], str]], w=1060, h=300) -> str:
    pts = [p for _, s, _ in series for p in s]
    if not pts:
        return "<p class='muted'>No trades.</p>"
    t0 = min(p[0] for p in pts).timestamp()
    t1 = max(p[0] for p in pts).timestamp()
    vals = [p[1] for p in pts] + [0]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad
    L, R, T, B = 78, 16, 14, 34
    X = lambda t: L + (t - t0) / ((t1 - t0) or 1) * (w - L - R)
    Y = lambda v: T + (hi - v) / (hi - lo) * (h - T - B)
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Cumulative net P&L">']
    step = _nice_step(hi - lo)
    v = (lo // step + 1) * step
    while v < hi:
        out.append(f'<line class="grid" x1="{L}" x2="{w-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                   f'<text x="{L-8}" y="{Y(v)+4:.1f}" text-anchor="end">{v/1000:,.0f}k</text>')
        v += step
    out.append(f'<line class="zero" x1="{L}" x2="{w-R}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
    # month ticks
    d = datetime.fromtimestamp(t0).date().replace(day=1)
    while True:
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        ts = datetime.combine(d, datetime.min.time()).timestamp()
        if ts > t1:
            break
        out.append(f'<line class="grid" x1="{X(ts):.1f}" x2="{X(ts):.1f}" y1="{T}" y2="{h-B}"/>'
                   f'<text x="{X(ts):.1f}" y="{h-12}" text-anchor="middle">{d:%d %b}</text>')
    for name, s, cls in series:
        if not s:
            continue
        path = [f"M{X(s[0][0].timestamp()):.1f},{Y(0):.1f}"]
        for t, val in s:
            path.append(f"H{X(t.timestamp()):.1f}V{Y(val):.1f}")
        out.append(f'<path class="{cls}" d="{"".join(path)}"><title>{html.escape(name)}</title></path>')
        tx, tv = s[-1]
        out.append(f'<circle cx="{X(tx.timestamp()):.1f}" cy="{Y(tv):.1f}" r="3.5" class="{cls}" '
                   f'style="fill:var(--surface)"/>')
    out.append("</svg>")
    return "".join(out)


def _daily_svg(daily: list[tuple[date, float]], w=1060, h=220) -> str:
    if not daily:
        return ""
    vals = [v for _, v in daily] + [0]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad
    L, R, T, B = 78, 16, 10, 30
    n = len(daily)
    bw = (w - L - R) / n
    Y = lambda v: T + (hi - v) / (hi - lo) * (h - T - B)
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Net P&L per day">']
    step = _nice_step(hi - lo)
    v = (lo // step + 1) * step
    while v < hi:
        out.append(f'<line class="grid" x1="{L}" x2="{w-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                   f'<text x="{L-8}" y="{Y(v)+4:.1f}" text-anchor="end">{v/1000:,.0f}k</text>')
        v += step
    for i, (d, val) in enumerate(daily):
        x = L + i * bw + bw * 0.15
        y0, y1 = sorted((Y(0), Y(val)))
        out.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bw*0.7:.1f}" height="{max(1, y1-y0):.1f}" '
                   f'class="{"b-pos" if val >= 0 else "b-neg"}"><title>{d:%a %d %b}: {_fmt(val)}</title></rect>')
        if i % max(1, n // 10) == 0:
            out.append(f'<text x="{x + bw*0.35:.1f}" y="{h-10}" text-anchor="middle">{d:%d %b}</text>')
    out.append(f'<line class="zero" x1="{L}" x2="{w-R}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/></svg>')
    return "".join(out)


def _nice_step(span: float) -> float:
    raw = span / 5
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def _cum(trades: list[Trade]) -> list[tuple[datetime, float]]:
    out, total = [], 0.0
    for t in sorted((t for t in trades if t.exit_ts), key=lambda t: t.exit_ts):
        total += t.net_pnl
        out.append((t.exit_ts, total))
    return out


def _stats_html(m: Metrics, capital: float) -> str:
    pf = "–" if m.profit_factor is None else f"{m.profit_factor:.2f}"
    items = [("Net P&L", f'<span class="num {_cls(m.net_pnl)}">{_fmt(m.net_pnl)}</span>'),
             ("Return on capital", f'<span class="num {_cls(m.net_pnl)}">{100*m.net_pnl/capital:+.2f}%</span>'),
             ("Trades (legs)", f'<span class="num">{m.trades}</span>'),
             ("Win rate", f'<span class="num">{m.win_rate:.1f}%</span>'),
             ("Avg win / loss", f'<span class="num">{_fmt(m.avg_win)} / {_fmt(m.avg_loss)}</span>'),
             ("Profit factor", f'<span class="num">{pf}</span>'),
             ("Max drawdown", f'<span class="num neg">{_fmt(m.max_drawdown)}</span>'),
             ("Gross P&L / costs", f'<span class="num">{_fmt(m.gross_pnl)} / ₹{m.costs:,.0f}</span>'),
             ("Stops hit / re-entries", f'<span class="num">{m.stop_losses} / {m.reentries}</span>'),
             ("Best / worst trade", f'<span class="num">{_fmt(m.best)} / {_fmt(m.worst)}</span>')]
    return '<div class="stats">' + "".join(f"<div><span>{k}</span>{v}</div>" for k, v in items) + "</div>"


def _trades_table(trades: list[Trade], show_spot: bool) -> str:
    head = ["Trade", "Expiry", "Option", "Entry", "Sold at", "Exit", "Bought at"]
    if show_spot:
        head += ["NIFTY in", "NIFTY out"]
    head += ["Exit reason", "Net P&L"]
    rows = []
    for t in sorted(trades, key=lambda t: t.entry_ts):
        cells = [html.escape(t.trade_id), t.expiry, f"{t.strike:,.0f} {'CE' if t.right == 'CALL' else 'PE'}",
                 f"{t.entry_ts:%d %b %H:%M}", f"{t.entry_price:,.2f}",
                 f"{t.exit_ts:%d %b %H:%M}" if t.exit_ts else "–", f"{t.exit_price:,.2f}" if t.exit_price else "–"]
        if show_spot:
            cells += [f"{t.spot_entry:,.2f}" if t.spot_entry else "–", f"{t.spot_exit:,.2f}" if t.spot_exit else "–"]
        cells += [html.escape(t.exit_reason), f'<span class="{_cls(t.net_pnl)}">{_fmt(t.net_pnl)}</span>']
        num = {4, 6} | ({7, 8} if show_spot else set())
        tds = "".join(f'<td class="n">{c}</td>' if (i in num or i == len(cells) - 1) else f"<td>{c}</td>"
                      for i, c in enumerate(cells))
        rows.append(f'<tr class="{"re" if t.is_reentry else ""}">{tds}</tr>')
    ths = "".join(f'<th class="n">{h}</th>' if h in ("Sold at", "Bought at", "NIFTY in", "NIFTY out", "Net P&L")
                  else f"<th>{h}</th>" for h in head)
    return f'<div class="scroll"><table><thead><tr>{ths}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def write_html(path, underlying, start, end, capital, results) -> dict:
    all_trades = [t for _, trades, _ in results for t in trades]
    total = metrics(all_trades)
    (pos_strat, pos_trades, pos_m), (zd_strat, zd_trades, zd_m) = results
    daily = defaultdict(float)
    for t in all_trades:
        if t.exit_ts:
            daily[t.exit_ts.date()] += t.net_pnl
    daily_list = sorted(daily.items())

    kpis = [("Net P&L", _fmt(total.net_pnl), _cls(total.net_pnl)),
            ("Return on ₹" + f"{capital/1e5:,.0f}L", f"{100*total.net_pnl/capital:+.2f}%", _cls(total.net_pnl)),
            ("Max drawdown", f"{_fmt(total.max_drawdown)} ({100*total.max_drawdown/capital:.1f}%)", "neg"),
            ("Win rate", f"{total.win_rate:.1f}%", ""),
            ("Trades (legs)", f"{total.trades}", ""),
            ("Charges + slippage", f"₹{total.costs:,.0f}", "")]
    kpi_html = "".join(f'<div class="kpi"><span class="eyebrow">{k}</span><span class="v num {c}">{v}</span></div>'
                       for k, v, c in kpis)

    # 0DTE: entry-time choices and average P&L by candidate time over the test days
    choice_rows = "".join(
        f'<tr><td>{c["expiry"]}</td><td class="n">{c["entry_time"]}</td><td class="n">{_fmt(c["training_pnl"])}</td>'
        f'<td class="n {_cls(c["day_pnl"])}">{_fmt(c["day_pnl"])}</td></tr>' for c in zd_strat.choices)
    times = sorted({t for (_, t) in zd_strat.grid})
    test_days = [date.fromisoformat(c["expiry"]) for c in zd_strat.choices]
    heat_cells = []
    for t in times:
        vals = [zd_strat.grid[(d, t)] for d in test_days if (d, t) in zd_strat.grid]
        avg = sum(vals) / len(vals) if vals else 0
        heat_cells.append((t, avg))
    mx = max((abs(v) for _, v in heat_cells), default=1) or 1
    heat = "".join(
        f'<td title="{t:%H:%M}: avg {_fmt(v)} per expiry day" style="background:color-mix(in srgb, '
        f'var({"--pos" if v >= 0 else "--neg"}) {int(12 + 60*abs(v)/mx)}%, transparent)">{t:%H:%M}<br>{v/1000:+.1f}k</td>'
        for t, v in heat_cells)

    signals = pos_strat.signals
    taken = sum(s["taken"] for s in signals)
    skipped_open = sum(s["reason"] == "previous position still open" for s in signals)
    no_break = sum(s["signal"] == "none" for s in signals)

    body = f"""<title>NIFTY Option Sellers</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header>
  <span class="eyebrow">Backtest · {underlying} weekly options · 1-minute data from Breeze</span>
  <h1>NIFTY option selling, {start:%d %b} – {end:%d %b %Y}</h1>
  <p>Two rule-based option-selling strategies run on stored NIFTY spot and option candles, 5 lots (325 qty) per leg,
  ₹{capital:,.0f} capital. All P&amp;L is net of charges and 0.5-point slippage per side.</p>
</header>
<div class="kpis">{kpi_html}</div>

<section class="panel">
  <h2>Cumulative net P&amp;L</h2>
  <div class="legend"><span><i style="background:var(--ink)"></i>Combined</span>
  <span><i style="background:var(--accent)"></i>{html.escape(pos_strat.name)}</span>
  <span><i style="background:var(--series2)"></i>{html.escape(zd_strat.name)}</span></div>
  {_equity_svg([("Combined", _cum(all_trades), "l-all"), (pos_strat.name, _cum(pos_trades), "l-a"),
                (zd_strat.name, _cum(zd_trades), "l-b")])}
</section>

<section class="panel">
  <h2>Net P&amp;L by exit day</h2>
  {_daily_svg(daily_list)}
</section>

<section>
  <div class="two">
    <div class="panel" style="display:grid;gap:12px"><h3>{html.escape(pos_strat.name)}</h3>{_stats_html(pos_m, capital)}</div>
    <div class="panel" style="display:grid;gap:12px"><h3>{html.escape(zd_strat.name)}</h3>{_stats_html(zd_m, capital)}</div>
  </div>
</section>

<section class="panel">
  <h2>Positional range breakout: trades</h2>
  <p class="muted" style="margin:0">{len(signals)} trading days: {len(signals)-no_break} broke the 09:15–11:15 range,
  {taken} were traded, {skipped_open} were skipped because a position was already open, {no_break} had no breakout.</p>
  {_trades_table(pos_trades, True)}
</section>

<section class="panel">
  <h2>0DTE ITM straddle: trades</h2>
  {_trades_table(zd_trades, False)}
</section>

<section class="two">
  <div class="panel" style="display:grid;gap:10px">
    <h3>Chosen entry time per expiry</h3>
    <p class="muted" style="margin:0">Picked from the best total P&amp;L over the previous 8 expiry days (walk-forward, no future data).</p>
    <div class="scroll"><table><thead><tr><th>Expiry</th><th class="n">Entry</th><th class="n">Training P&amp;L</th>
    <th class="n">Day P&amp;L</th></tr></thead><tbody>{choice_rows}</tbody></table></div>
  </div>
  <div class="panel" style="display:grid;gap:10px">
    <h3>Average P&amp;L per expiry day by entry time</h3>
    <p class="muted" style="margin:0">Over the {len(test_days)} test expiry days, if that time had been used every week (hindsight view).</p>
    <div class="scroll"><table class="heat"><tr>{heat}</tr></table></div>
  </div>
</section>

<section class="panel">
  <h2>Rules and assumptions</h2>
  <ul class="notes">
    <li><b>Positional:</b> range = NIFTY high/low from 09:15 to 11:14. The first 1-minute close above the high sells a PUT, below the low sells a CALL, 100 points in the money (ATM ± 100), on the next weekly expiry after the entry day. If both sides qualify on the same bar, the PE sale wins.</li>
    <li><b>Positional stop:</b> NIFTY closes 0.5% against the entry level. Otherwise the position is held to 15:15 on expiry day. One re-entry per signal when NIFTY returns to the original entry level, with a new 0.5% stop. One position at a time.</li>
    <li><b>0DTE:</b> on each weekly expiry day, sell a CALL 100 points ITM and a PUT 100 points ITM at the chosen time. Each leg has a stop 30% above its premium, triggered on the 1-minute high and filled at the stop price (or the open if it gaps through). One re-entry per leg when its premium returns to the entry price. Exit at 15:15.</li>
    <li><b>Fills:</b> the option's 1-minute close at the signal minute, or its last traded price if that minute had no trade. Slippage 0.5 point per side per unit.</li>
    <li><b>Charges per leg round trip:</b> ₹20 brokerage per order, STT 0.1% of sell premium, NSE fee 0.03503%, SEBI fee, stamp duty 0.003% on buys, 18% GST.</li>
    <li><b>Margin</b> is not modelled; 5 lots of short ITM options on NIFTY can need more margin than a ₹10L account has for overlapping positions.</li>
    <li><b>Data:</b> Breeze's NIFTY index feed often freezes from 15:17 to 15:19 and jumps at 15:20 (20 of 726 days in the stored history). Positional stops and re-entries are therefore only evaluated up to 15:15.</li>
    <li>A position still open on the last data day is marked to market at that day's close.</li>
  </ul>
</section>
<p class="muted" style="font-size:12px">Generated {datetime.now():%d %b %Y %H:%M} by scripts/run_backtest.py.</p>
</div>"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(body, encoding="utf-8")
    return {"underlying": underlying, "start": str(start), "end": str(end), "capital": capital,
            "combined": asdict(total),
            "strategies": [{"name": s.name, "metrics": asdict(m), "trades": [asdict(t) for t in tr]}
                           for s, tr, m in results],
            "zero_dte_choices": zd_strat.choices, "positional_signals": pos_strat.signals}
