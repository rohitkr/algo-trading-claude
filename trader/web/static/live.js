// Live prices pushed by the server (GET /api/stream, Server-Sent Events; see trader/stream.py). One stream per
// page. Ticks are merged and written to the DOM at most once per animation frame, and only into the cells
// that show them - no table re-render, no polling:
//   [data-price="NFO:SYMBOL" | "SPOT:NIFTY"]   text = price
//   [data-trade-ltp=ID] [data-trade-pnl=ID] [data-trade-pnlpct=ID] [data-strategy-pnl=ID]
// A "dashboard" event (trade/order state changed) calls the page's onDashboard handlers, which refetch
// /api/dashboard. Pages call Live.watch([...keys]) with the instruments they display; every trade of the day
// (open and closed) is streamed for every page without asking. With MARKET_DATA_PROVIDER=BREEZE nothing
// ticks: trade LTP/P&L then arrive after each monitor tick and html.live-prices is not set (manual refresh
// buttons stay visible).
"use strict";
window.Live = (() => {
  const fmtNum = (v) => (v == null ? "–" : Number(v).toLocaleString("en-IN", {maximumFractionDigits: 2}));
  const fmtMoney = (v) => (v == null ? "–" : "₹" + fmtNum(v));
  const dash = [], statusFns = [], priceFns = [], strategyFns = [];
  let client = null, keys = [], streaming = false, frame = 0, dashTimer = 0, lastStatus = null;
  let pending = {prices: {}, trades: {}, strategies: {}};

  function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
  function setSign(el, v) {
    el.classList.toggle("pos", v > 0);
    el.classList.toggle("neg", v < 0);
  }
  function each(sel, fn) { document.querySelectorAll(sel).forEach(fn); }

  function apply() {
    frame = 0;
    const p = pending;
    pending = {prices: {}, trades: {}, strategies: {}};
    for (const [k, px] of Object.entries(p.prices)) {
      each(`[data-price="${CSS.escape(k)}"]`, (el) => setText(el, el.dataset.fmt === "money" ? fmtMoney(px) : fmtNum(px)));
      priceFns.forEach((fn) => fn(k, px));
    }
    for (const [id, t] of Object.entries(p.trades)) {
      each(`[data-trade-ltp="${id}"]`, (el) => setText(el, fmtNum(t.ltp)));
      each(`[data-trade-pnl="${id}"]`, (el) => { setText(el, fmtMoney(t.pnl)); setSign(el, t.pnl); });
      each(`[data-trade-pnlpct="${id}"]`, (el) => { setText(el, t.pnl_pct == null ? "–" : t.pnl_pct + "%"); setSign(el, t.pnl); });
    }
    for (const [id, v] of Object.entries(p.strategies)) {
      each(`[data-strategy-pnl="${id}"]`, (el) => { setText(el, fmtMoney(v)); setSign(el, v); });
      strategyFns.forEach((fn) => fn(Number(id), v));
    }
  }

  const known = {prices: {}, trades: {}, strategies: {}};   // newest pushed value of everything, for reapply()
  function merge(d) {
    for (const k of ["prices", "trades", "strategies"]) Object.assign(known[k], d[k] || {});
    Object.assign(pending.prices, d.prices || {});
    Object.assign(pending.trades, d.trades || {});
    Object.assign(pending.strategies, d.strategies || {});
    if (!frame) frame = requestAnimationFrame(apply);
  }

  function fireDashboard() {          // coalesce bursts (e.g. several legs changing in one monitor tick)
    clearTimeout(dashTimer);
    dashTimer = setTimeout(() => dash.forEach((fn) => fn()), 50);
  }

  async function sendWatch() {
    if (client == null || !streaming) return;
    try {
      await fetch("/api/stream/watch", {method: "POST", headers: {"Content-Type": "application/json", "X-Trader": "1"},
                                        body: JSON.stringify({client, keys})});
    } catch (e) { /* the stream's reconnect resends */ }
  }

  function connect() {
    const es = new EventSource("/api/stream");
    es.addEventListener("hello", (ev) => {
      const d = JSON.parse(ev.data);
      client = d.client; streaming = !!d.streaming;
      document.documentElement.classList.toggle("live-prices", streaming);
      sendWatch();
      fireDashboard();                 // (re)connected: whatever changed meanwhile, show it
    });
    es.addEventListener("ticks", (ev) => merge(JSON.parse(ev.data)));
    es.addEventListener("dashboard", fireDashboard);
    es.addEventListener("status", (ev) => { lastStatus = JSON.parse(ev.data).quotes; statusFns.forEach((fn) => fn(lastStatus)); });
    es.onerror = () => { client = null; statusFns.forEach((fn) => fn({source: "stream", ok: false, last_error: "reconnecting to the trader server"})); };
  }

  function sameKeys(a, b) { return a.length === b.length && a.every((k, i) => k === b[i]); }

  return {
    watch(list) {
      const next = [...new Set(list.filter(Boolean))].sort();
      if (sameKeys(next, keys)) return;
      keys = next;
      sendWatch();
    },
    onDashboard(fn) { dash.push(fn); },
    onStatus(fn) { statusFns.push(fn); if (lastStatus) fn(lastStatus); },
    onPrice(fn) { priceFns.push(fn); },
    onStrategy(fn) { strategyFns.push(fn); },     // fn(strategyId, pnl) for every pushed strategy P&L          // fn(key, price) for every pushed price
    // After a page re-renders rows from /api/dashboard (whose prices are the last monitor tick's), put the
    // newer pushed values back so a cell never steps backwards.
    reapply() { merge(known); },
    get streaming() { return streaming; },
    start: connect,
  };
})();
