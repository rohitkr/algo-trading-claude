// Trader UI: renders what the server returns and posts user intent. No trading logic lives here;
// every check shown in the browser is repeated (authoritatively) on the server.
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const money = (v) => (v == null ? "–" : "₹" + Number(v).toLocaleString("en-IN", {maximumFractionDigits: 2}));
const num = (v) => (v == null || v === "" ? "–" : Number(v).toLocaleString("en-IN", {maximumFractionDigits: 2}));
const tm = (s) => (s ? String(s).replace("T", " ").slice(5, 19) : "–");
let META = null, MODE = "PAPER", LOT = null, SYMBOL = null, SPOT = null, TICK = null;

// ---------------------------------------------------------------- persisted form (survives refresh)
const STORE_KEY = "trader:form:v1";
function loadStored() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null") || {}; } catch (e) { return {}; }
}
function saveStored() {
  // raw typed values (points stay points) + the chosen modes, not the converted prices
  const raw = {...Object.fromEntries(new FormData(form).entries()),
               trail_enabled: $("#trail_enabled").checked, partial_enabled: $("#partial_enabled").checked};
  try { localStorage.setItem(STORE_KEY, JSON.stringify(raw)); } catch (e) { /* private window etc: ignore */ }
}
const STORED = loadStored();
function restoreRadio(name, fallback) {
  const want = STORED[name] ?? fallback;
  const el = form.querySelector(`input[name="${name}"][value="${want}"]`);
  if (el) el.checked = true;
}
function restoreValue(name) {
  if (STORED[name] == null || STORED[name] === "") return;
  const el = form.elements[name];
  if (!el) return;
  if (el.type === "checkbox") el.checked = STORED[name] === true || STORED[name] === "true";
  else el.value = STORED[name];
}

async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-Trader": "1"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({error: "bad response"}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// ---------------------------------------------------------------- form
const form = $("#form");
// SL / target can be typed as an absolute ₹ price (BUY @420: SL 400, TP 500) or as points from the entry
// (BUY @420: SL 20, TP 80 -> 400 / 500; SELL legs the other way round). Points become a tick-rounded price here,
// so everything downstream (preview, checks, server) only ever sees prices.
const toTickPrice = (v) => { const t = TICK || 0.05; return Math.round(Math.round(v / t) * t * 100) / 100; };
function pointsToPrice(d, key, mode, dir) {
  if (d[mode] !== "points" || d[key] === "" || d[key] == null || d.entry_price === "") return;
  const entry = Number(d.entry_price), pts = Number(d[key]);
  if (!Number.isFinite(entry) || !Number.isFinite(pts)) return;
  d[key] = String(toTickPrice(entry + dir * pts));
}
function formData() {
  const d = Object.fromEntries(new FormData(form).entries());
  d.trail_enabled = $("#trail_enabled").checked; d.partial_enabled = $("#partial_enabled").checked;
  const buy = d.side === "BUY";
  pointsToPrice(d, "stop_loss", "sl_mode", buy ? -1 : 1);
  pointsToPrice(d, "target", "tp_mode", buy ? 1 : -1);
  delete d.sl_mode; delete d.tp_mode;
  return d;
}

async function loadMeta() {
  META = await api("/api/meta");
  MODE = META.mode;
  const u = $("#underlying");
  u.innerHTML = Object.keys(META.underlyings).map((k) => `<option>${esc(k)}</option>`).join("");
  if (STORED.underlying && Object.keys(META.underlyings).includes(STORED.underlying)) u.value = STORED.underlying;
  restoreRadio("option_type", "CE"); restoreRadio("side", "BUY");
  restoreRadio("product", "NRML");                    // NRML by default, not MIS
  for (const f of ["lots", "entry_price", "stop_loss", "target", "trail_type", "trail_value", "trail_step",
                    "partial_lots", "partial_price", "auto_exit_time"]) restoreValue(f);
  restoreValue("trail_enabled"); restoreValue("partial_enabled"); restoreValue("sl_mode"); restoreValue("tp_mode");
  await onUnderlying();
}

async function onUnderlying() {
  const info = META.underlyings[$("#underlying").value] || {};
  $("#expiry").innerHTML = (info.expiries || []).map((e) => `<option>${esc(e)}</option>`).join("");
  if (STORED.expiry && (info.expiries || []).includes(STORED.expiry)) $("#expiry").value = STORED.expiry;
  if (info.error) $("#form-hint").textContent = info.error;
  $("#price-symbol").textContent = $("#underlying").value;
  $("#price-value").textContent = "–";
  $("#price-value").dataset.price = `SPOT:${$("#underlying").value}`;     // live.js keeps it current
  $("#price-value").dataset.fmt = "money";
  watchPrices();
  SPOT = null;
  await refreshSpot();               // the strike list below is built from THIS spot, so wait for it first
  await onExpiry();
}

async function refreshSpot(force) {
  try {
    const s = await api(`/api/spot?ltp=1${force ? "&force=1" : ""}&underlying=${encodeURIComponent($("#underlying").value)}`);
    SPOT = s.spot;
    $("#price-value").textContent = s.spot == null ? "no price" : money(s.spot);
    $("#price-value").title = s.error || "";
    if (s.error) $("#form-hint").textContent = s.error;
  } catch (e) { SPOT = null; $("#price-value").textContent = "–"; }
}

async function refreshOptionLtp(force) {
  const d = formData();
  if (!d.expiry || !d.strike) return;
  try {
    const c = await api(`/api/contract?ltp=1${force ? "&force=1" : ""}&underlying=${encodeURIComponent(d.underlying)}&expiry=${d.expiry}&strike=${d.strike}&option_type=${d.option_type}`);
    $("#opt-ltp").textContent = c.ltp == null ? "no price" : money(c.ltp);
  } catch (e) { $("#opt-ltp").textContent = "–"; }
}

async function onExpiry() {
  const u = $("#underlying").value, e = $("#expiry").value;
  if (!e) { $("#strike").innerHTML = ""; return; }
  const r = await api(`/api/strikes?underlying=${encodeURIComponent(u)}&expiry=${encodeURIComponent(e)}`);
  const all = r.strikes;                                // ascending, the exchange's own strike interval
  // ATM = the actual tradable strike closest to the real spot (generated only once spot is known); falls
  // back to the middle of the chain when no spot price is available yet.
  const center = SPOT == null ? Math.floor(all.length / 2)
    : all.reduce((best, k, i) => Math.abs(k - SPOT) < Math.abs(all[best] - SPOT) ? i : best, 0);
  const lo = Math.max(0, center - 20), hi = Math.min(all.length, center + 21);
  const strikes = all.slice(lo, hi);                    // an equal number of strikes above and below ATM
  const atmIndex = center - lo;
  const s = $("#strike"), prev = s.value;
  s.innerHTML = strikes.map((k) => `<option value="${k}">${k}</option>`).join("");
  if (STORED.strike && strikes.includes(Number(STORED.strike))) s.value = STORED.strike;
  else if (prev && strikes.includes(Number(prev))) s.value = prev;
  else s.selectedIndex = Math.min(atmIndex, strikes.length - 1);   // default: the ATM strike itself
  await onContract();
}

async function onContract() {
  const d = formData();
  if (!d.expiry || !d.strike) return;
  try {
    const c = await api(`/api/contract?underlying=${encodeURIComponent(d.underlying)}&expiry=${d.expiry}&strike=${d.strike}&option_type=${d.option_type}`);
    LOT = c.lot_size; SYMBOL = c.tradingsymbol; TICK = c.tick_size;
    $("#opt-ltp").textContent = "–";
    $("#opt-ltp").dataset.price = `${c.exchange}:${c.tradingsymbol}`;
    $("#opt-ltp").dataset.fmt = "money";
    watchPrices();
    $("#pp-symbol").value = c.tradingsymbol;
    refreshOptionLtp();               // auto: this contract's own LTP, no click needed
  } catch (e) { LOT = null; $("#opt-ltp").textContent = "–"; $("#form-hint").textContent = e.message; }
  check();
}

function check() {
  const d = formData(), p = (k) => (d[k] === "" || d[k] == null ? null : Number(d[k]));
  const lots = p("lots"), entry = p("entry_price"), sl = p("stop_loss"), tgt = p("target");
  $("#qty").textContent = LOT && lots ? `${lots} × ${LOT} = ${lots * LOT}` : "–";
  const errs = [];
  if (entry && sl) {
    if (d.side === "BUY" && sl >= entry) errs.push("BUY: stop-loss must be below entry");
    if (d.side === "SELL" && sl <= entry) errs.push("SELL: stop-loss must be above entry");
  }
  if (entry && tgt) {
    if (d.side === "BUY" && tgt <= entry) errs.push("BUY: target must be above entry");
    if (d.side === "SELL" && tgt >= entry) errs.push("SELL: target must be below entry");
  }
  if (d.partial_enabled && lots && p("partial_lots") >= lots) errs.push("partial lots must be fewer than total lots");
  $("#trail_opts").classList.toggle("off", !d.trail_enabled);
  $("#partial_opts").classList.toggle("off", !d.partial_enabled);
  $("#form-hint").textContent = errs.join(" · ");
  $("#preview-btn").disabled = errs.length > 0 || !LOT;
  $("#preview-btn").textContent = `Review ${d.side} order`;

  const qty = LOT && lots ? lots * LOT : null;
  const loss = qty && entry != null && sl != null ? Math.abs(entry - sl) * qty : null;
  const gain = qty && entry != null && tgt != null ? Math.abs(tgt - entry) * qty : null;
  $("#pnl-preview").classList.toggle("hidden", loss == null && gain == null);
  $("#pnl-loss").textContent = loss == null ? "–" : money(loss);
  $("#pnl-gain").textContent = gain == null ? "–" : money(gain);
}

$("#get-ltp").onclick = () => { refreshSpot(true); refreshOptionLtp(true); };   // force=1: a click always gets a fresh Breeze call

form.addEventListener("input", (ev) => {
  saveStored();
  if (ev.target.name === "underlying") onUnderlying();
  else if (ev.target.name === "expiry") onExpiry();
  else if (["strike", "option_type"].includes(ev.target.name)) onContract();
  else check();
});

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#preview-btn").disabled = true;
  try {
    const r = await api("/api/preview", formData());
    if (!r.ok) {
      const failed = (r.risk || []).filter((c) => !c.passed).map((c) => `${c.name}: ${c.detail}`);
      alertBox("Trade not allowed", [...(r.errors || []), ...failed]);
      return;
    }
    const pending = dialog(`Place ${r.summary.side} order?`, summaryHtml(r.summary, r.risk), MODE === "LIVE");
    okButton(r.summary.side === "BUY" ? "Buy" : "Sell", r.summary.side);
    const ok = await pending;
    if (!ok) return;
    const c = await api(`/api/trades/${r.trade_id}/confirm`, {token: r.token});
    toast(`Trade #${r.trade_id}: entry order ${c.order_status}`);
    refresh();
  } catch (e) { alertBox("Error", [e.message]); }
  finally { check(); }
});

function summaryHtml(s, risk) {
  const rows = [
    ["Mode", `<b>${esc(s.mode)}</b>`], ["Contract", `${esc(s.tradingsymbol)} (${esc(s.exchange)})`],
    ["Product", esc(s.product)], ["Quantity", `${s.lots} lot(s) × ${s.lot_size} = <b>${s.quantity}</b>`],
    ["Entry (LIMIT)", money(s.entry_price)], ["Order value", `<b>${money(s.order_value)}</b>`],
    ["Stop-loss", `${money(s.stop_loss)} (loss at SL ${money(s.max_loss_at_sl)})`],
    ["Target", s.target ? `${money(s.target)} (gain ${money(s.reward_at_target)})` : "none"],
    ["Trailing SL", s.trailing ? `${s.trailing.value} ${s.trailing.type === "PERCENT" ? "%" : "pts"}, step ${s.trailing.step}` : "off"],
    ["Partial booking", s.partial ? `${s.partial.lots} lot(s) = ${s.partial.qty} @ ${money(s.partial.price)}` : "off"],
    ["Auto-exit", s.auto_exit_at ? tm(s.auto_exit_at) : "–"], ["Square-off", s.square_off_time || "off"],
    ["LTP now", s.ltp == null ? "no price" : money(s.ltp)],
  ];
  const warn = (risk || []).filter((c) => c.passed && c.name === "entry_near_ltp").map((c) => esc(c.detail)).join("<br>");
  return `<div class="big-side ${esc(s.side)}">${esc(s.side)} ${esc(s.tradingsymbol)}</div>` +
    `<table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>` + (warn ? `<p>${warn}</p>` : "");
}

// ---------------------------------------------------------------- dialogs
function dialog(title, html, live = false) {
  const d = $("#dlg");
  $("#dlg-title").textContent = title;
  $("#dlg-body").innerHTML = html;
  $("#dlg-live").classList.toggle("hidden", !live);      // "LIVE order: real money" notice, no typing
  okButton("Confirm");
  return new Promise((resolve) => {
    d.onclose = () => resolve(d.returnValue === "ok");
    d.returnValue = ""; d.showModal();
  });
}
// The dialog's OK button: "Buy" / "Sell" (coloured by side) for an order, "Confirm" otherwise.
function okButton(text, side) {
  const b = $("#dlg-ok");
  b.classList.remove("hidden", "ok-buy", "ok-sell");
  b.textContent = text;
  if (side) b.classList.add(side === "BUY" ? "ok-buy" : "ok-sell");
}
function alertBox(title, lines) {
  dialog(title, `<ul>${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`);
  $("#dlg-ok").classList.add("hidden");
}
function toast(msg) { $("#form-hint").textContent = msg; }

async function action(tid, act) {
  try {
    if (act === "EXIT") return await exitPosition(tid);
    const p = await api(`/api/trades/${tid}/prepare`, {action: act});
    const t = p.trade;
    const ok = await dialog("Cancel entry order?",
      `<div class="big-side ${esc(t.side)}">${esc(t.side)} ${esc(t.tradingsymbol)}</div>
       <p>Cancel the unfilled entry order of ${esc(t.tradingsymbol)}.</p>`, MODE === "LIVE");
    if (!ok) return;
    await api(`/api/trades/${tid}/cancel`, {token: p.token});
    refresh();
  } catch (e) { alertBox("Error", [e.message]); }
}

// Exiting less than the full open quantity goes through the partial-exit API (prepare+confirm); exiting
// all of it uses the normal full exit. The qty field defaults to the full open amount but is editable down.
async function exitPosition(tid) {
  const t = (await api(`/api/trades/${tid}`)).trade;
  const lots = Math.floor(t.open_qty / t.lot_size);
  const html = `<div class="big-side ${esc(t.side)}">${esc(t.side)} ${esc(t.tradingsymbol)}</div>
    <p>Filled ${t.filled_qty}/${t.quantity}, open ${t.open_qty}, P&amp;L ${money(t.pnl)}</p>
    <label>Lots to exit (of ${lots} open) <input id="exit-lots" type="number" min="1" max="${lots}" step="1" value="${lots}"></label>`;
  const ok = await dialog("Exit position?", html, MODE === "LIVE");
  if (!ok) return;
  const chosen = Math.max(1, Math.min(lots, Math.floor(Number($("#exit-lots").value)) || lots));
  const qty = chosen * t.lot_size;
  if (qty >= t.open_qty) {
    const p = await api(`/api/trades/${tid}/prepare`, {action: "EXIT"});
    await api(`/api/trades/${tid}/exit`, {token: p.token});
  } else {
    const p = await api(`/api/trades/${tid}/partial/prepare`, {qty});
    await api(`/api/trades/${tid}/partial/confirm`, {token: p.token});
  }
  refresh();
}

// ---------------------------------------------------------------- edit
const LIVE_ST = new Set(["ENTRY_ORDER_PLACED", "ENTRY_PENDING", "ENTRY_EXECUTED", "POSITION_ACTIVE"]);
const canEdit = (t) => LIVE_ST.has(t.status) && !t.pending_exit_reason;

async function edit(tid) {
  try { await editFlow(tid); } catch (e) { alertBox("Edit failed", [e.message]); }   // never fail silently
}

async function editFlow(tid) {
  const t = (await api(`/api/trades/${tid}`)).trade;
  const entryOpen = ["ENTRY_ORDER_PLACED", "ENTRY_PENDING"].includes(t.status);
  const f = (name, label, val, attrs = 'type="number" step="0.05"') =>
    `<label>${label} <input name="${name}" ${attrs} value="${val ?? ""}"></label>`;
  // NOT a <form>: the dialog is already a <form>, and browsers silently drop a nested one.
  const html = `<div id="edit-form" class="grid">
    ${entryOpen ? f("entry_price", `<span>Entry limit ₹ <button type="button" class="ltp-fill" title="Fill the live price (LTP)">↻</button></span>`, t.entry_price) + f("lots", `Lots (filled ${t.filled_qty})`, t.lots, 'type="number" step="1" min="1"') : ""}
    ${f("stop_loss", "Stop-loss ₹", t.current_sl)}${f("target", "Target ₹ (blank = none)", t.target)}
    <label>Trailing <select name="trail_enabled"><option value="false">off</option><option value="true" ${t.trail_enabled ? "selected" : ""}>on</option></select></label>
    <label>Trail type <select name="trail_type"><option ${t.trail_type === "POINTS" ? "selected" : ""}>POINTS</option><option ${t.trail_type === "PERCENT" ? "selected" : ""}>PERCENT</option></select></label>
    ${f("trail_value", "Trail by", t.trail_value)}${f("trail_step", "Trail step ₹", t.trail_step)}
    ${t.partial_done ? "" : `<label>Partial booking <select name="partial_enabled"><option value="false">off</option><option value="true" ${t.partial_enabled ? "selected" : ""}>on</option></select></label>
      ${f("partial_lots", "Partial lots", t.partial_lots, 'type="number" step="1" min="1"')}${f("partial_price", "Partial at ₹", t.partial_price)}`}
    ${f("auto_exit_time", "Auto-exit time", t.auto_exit_at ? t.auto_exit_at.slice(11, 16) : "", 'type="time"')}
  </div>`;
  const pendingEdit = dialog(`Edit trade #${tid}: ${t.side} ${t.tradingsymbol}`, html);
  const fill = $("#edit-form .ltp-fill");            // ↻: the contract's live price into the entry limit
  if (fill) fill.onclick = async (ev) => {
    ev.preventDefault();
    fill.disabled = true;
    try {
      const c = await api(`/api/contract?ltp=1&force=1&underlying=${encodeURIComponent(t.underlying)}&expiry=${t.expiry}&strike=${t.strike}&option_type=${t.option_type}`);
      if (c.ltp == null) throw new Error(c.price_error || "no live price");
      const tick = Number(t.tick_size) || 0.05, box = $("#edit-form input[name=entry_price]");
      box.value = (Math.round(Number(c.ltp) / tick) * tick).toFixed(2);
      box.focus();
    } catch (e) { fill.title = `No price: ${e.message}`; }
    finally { fill.disabled = false; }
  };
  const go = await pendingEdit;
  if (!go) return;
  const box = $("#edit-form");
  if (!box) throw new Error("edit form not found");
  const changes = {};
  box.querySelectorAll("input[name], select[name]").forEach((el) => { changes[el.name] = el.value; });
  {
    const p = await api(`/api/trades/${tid}/edit/prepare`, {changes});
    if (!p.ok && (p.errors || []).join() === "nothing changed") return;   // saved without changes: just close
    if (!p.ok) return alertBox("Edit not allowed", p.errors || []);
    const rows = Object.entries(p.diff).map(([k, [a, b]]) => `<tr><td>${esc(k)}</td><td>${esc(a ?? "–")}</td><td>→ <b>${esc(b ?? "–")}</b></td></tr>`).join("");
    const ok = await dialog("Apply these changes?", `<table>${rows}</table>` +
      (p.modifies_entry_order ? "<p>The entry order at Zerodha will be modified.</p>" : ""), MODE === "LIVE");
    if (!ok) return;
    const res = await api(`/api/trades/${tid}/edit/apply`, {token: p.token});
    toast(`Trade #${tid} updated: SL ${res.trade.current_sl}, target ${res.trade.target ?? "none"}`);
    refresh();
  }
}

// ---------------------------------------------------------------- dashboard
const BAD = new Set(["ERROR", "UNKNOWN_REQUIRES_RECONCILIATION", "MANUALLY_EXITED", "REJECTED"]);
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

// Pushed prices: the form's spot + contract (trades stream for every page without asking).
function watchPrices() { Live.watch([$("#price-value").dataset.price, $("#opt-ltp").dataset.price]); }
Live.onPrice((k, px) => { if (k === $("#price-value").dataset.price) SPOT = px; });
// Only touch the DOM when a table's markup actually changed (a dashboard refetch after an unrelated change
// must not rebuild every row, lose hover/selection or restart CSS transitions).
function setHTML(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }
const ltpCell = (t) => `<span data-trade-ltp="${t.id}">${num(t.kite_ltp ?? t.last_ltp)}</span>`;

// Completed trades: filtered by the date they closed and paged (open trades are the Active table, never filtered).
let COMPLETED = [];
const DONE_FILTER = HistoryFilter.create($("#completed-filter"), "completed-trades", () => renderCompleted());
const completedRow = (t) => `<tr class="clickable" data-id="${t.id}"><td>${t.id}</td>
    <td>${esc(t.tradingsymbol)}</td><td class="${t.side}">${t.side}</td><td class="num">${num(t.entry_avg_price ?? t.entry_price)}</td>
    <td class="num">${num(t.exit_avg_price)}</td>
    <td class="num">${ltpCell(t)}</td>
    <td class="num">${t.filled_qty}/${t.quantity}</td><td class="num ${cls(t.pnl)}" data-trade-pnl="${t.id}">${money(t.pnl)}</td>
    <td>${esc(t.exit_reason || t.error || "")}</td><td>${t.duration_s ? Math.round(t.duration_s / 60) + " min" : "–"}</td>
    <td><span class="status ${BAD.has(t.status) ? "bad" : ""}">${esc(t.status)}</span></td></tr>`;
function renderCompleted() {
  const {page, total} = DONE_FILTER.apply(COMPLETED, (t) => t.exit_time || t.updated_at || t.created_at, () => false);
  setHTML($("#completed tbody"), page.map(completedRow).join("") ||
    `<tr><td colspan="11">${total || !COMPLETED.length ? "None yet" : "No completed trades in this date range"}</td></tr>`);
  Live.reapply();
}

async function refresh() {
  let d;
  try { d = await api("/api/dashboard"); } catch (e) { $("#banner").textContent = "Server unreachable: " + e.message; return; }
  const b = $("#banner");
  b.className = "banner" + (d.live ? " live" : "");
  b.textContent = d.live ? "● LIVE TRADING: orders go to Zerodha with real money" : "PAPER TRADING: simulated exchange, no orders reach Zerodha";
  const h = $("#halt");
  h.classList.toggle("hidden", !d.halted);
  if (d.halted) h.innerHTML = `New trades blocked: ${esc(d.halted)} <button id="resume">Resume</button>`;
  const rb = $("#resume"); if (rb) rb.onclick = async () => { if (confirm("Allow new trades again?")) { await api("/api/resume", {}); refresh(); } };
  $("#paper-price").classList.toggle("hidden", !(d.mode === "PAPER" && d.quotes.source === "manual"));

  setHTML($("#active tbody"), d.active.map((t) => {
    const working = ["ENTRY_ORDER_PLACED", "ENTRY_PENDING"].includes(t.status);
    const canExit = !["ERROR", "UNKNOWN_REQUIRES_RECONCILIATION"].includes(t.status) && t.filled_qty > 0 && !t.pending_exit_reason;
    const sl = t.current_sl !== t.initial_sl ? `${num(t.current_sl)} <small>(was ${num(t.initial_sl)})</small>` : num(t.current_sl);
    return `<tr class="clickable" data-id="${t.id}"><td>${t.id}</td><td>${esc(t.tradingsymbol)}</td><td class="${t.side}">${t.side}</td>
      <td class="num">${num(t.entry_avg_price ?? t.entry_price)}</td>
      <td class="num">${ltpCell(t)}</td>
      <td class="num">${sl}${t.sl_software_only ? " ⚠" : ""}</td>
      <td class="num">${num(t.target)}</td><td class="num">${t.quantity}</td><td class="num">${t.filled_qty}${t.open_qty !== t.filled_qty ? ` (open ${t.open_qty})` : ""}</td>
      <td class="num ${cls(t.pnl)}" data-trade-pnl="${t.id}">${money(t.pnl)}</td><td class="num ${cls(t.pnl)}" data-trade-pnlpct="${t.id}">${t.pnl_pct == null ? "–" : t.pnl_pct + "%"}</td>
      <td><span class="status ${BAD.has(t.status) ? "bad" : ""}">${esc(t.status)}${t.pending_exit_reason ? " → " + esc(t.pending_exit_reason) : ""}</span></td>
      <td>${tm(t.entry_time)}</td><td>${tm(t.updated_at)}</td>
      <td>${working ? `<button data-act="CANCEL" data-id="${t.id}">Cancel entry</button>` : ""}
          ${canEdit(t) ? `<button data-act="EDIT" data-id="${t.id}">Edit</button>` : ""}
          ${canExit ? `<button data-act="EXIT" data-id="${t.id}" class="danger">Exit</button>` : ""}</td></tr>`;
  }).join("") || `<tr><td colspan="15">No active trades</td></tr>`);

  COMPLETED = d.completed;
  renderCompleted();

  const s = d.system, v = (k) => (s[k] || {}).value;
  const broker = v("broker") || {}, rec = v("reconciliation") || {}, proc = v("process") || {};
  const rows = [
    ["Mode", d.mode], ["Zerodha", broker.ok ? `<span class="ok">connected</span> (${esc(d.broker)})` : `<span class="err">${esc(broker.last_error || "not synced")}</span>`],
    ["Last broker sync", tm(broker.last_sync)], ["Active trades", d.active.length],
    ["Reconciliation", rec.at ? `${tm(rec.at)} · mismatches ${rec.pending_mismatches} · need attention ${rec.needs_attention}` : "–"],
    ["Prices", `<span id="prices-status">${pricesText(d.quotes)}</span>`],
    ["Daily P&L", money(v("daily_pnl"))], ["Trades today", `${d.trades_today} / ${d.risk_limits.max_trades_per_day || "∞"}`],
    ["Last error", `<span class="err">${esc(v("last_error") || d.quotes.last_error || "–")}</span>`],
    ["Process", `${esc(proc.state || "?")} since ${tm(proc.started_at)} · heartbeat ${tm(v("heartbeat"))}`],
    ["Limits", `open ≤ ${d.risk_limits.max_open_trades || "off"}, loss/day ${d.risk_limits.max_daily_loss ? money(d.risk_limits.max_daily_loss) : "off"}, loss/trade ${d.risk_limits.max_loss_per_trade ? money(d.risk_limits.max_loss_per_trade) : "off"}, window ${d.risk_limits.trading_window.map((x) => x || "off").join("–")}, square-off ${d.risk_limits.square_off_time || "off"}${d.risk_limits.mcx ? ` · MCX ${d.risk_limits.mcx[0]}–${d.risk_limits.mcx[1]}, square-off ${d.risk_limits.mcx[2] || "off"}` : ""}`],
  ];
  setHTML($("#system"), rows.map(([k, x]) => `<div><span>${k}</span><span class="v">${x}</span></div>`).join(""));
  Live.reapply();
}

function pricesText(q) {
  if (!q) return "–";
  if (q.source === "kite") {
    const state = q.session_expired ? `<span class="err">Kite session expired: run python3 -m zerodha login</span>`
      : q.streaming ? `<span class="ok">streaming</span> · ${q.subscribed} instruments`
      : `<span class="err">${q.connected ? "stale" : "not connected"}</span> · REST fallback`;
    return `kite ${state}${q.last_ok ? " · last tick " + tm(q.last_ok) : ""}${q.fallback ? " · fallback " + esc(q.fallback.source) : ""}`;
  }
  return `${esc(q.source)} ${q.last_ok ? "· last " + tm(q.last_ok) : ""}${q.api_budget_remaining != null ? " · budget " + q.api_budget_remaining : ""}`;
}
Live.onStatus((q) => { const el = $("#prices-status"); if (el) el.innerHTML = pricesText(q); });

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (btn) { ev.stopPropagation(); return btn.dataset.act === "EDIT" ? edit(Number(btn.dataset.id)) : action(Number(btn.dataset.id), btn.dataset.act); }
  const row = ev.target.closest("tr[data-id]");
  if (row) showDetail(Number(row.dataset.id));
});

async function showDetail(id) {
  const r = await api(`/api/trades/${id}`);
  const t = r.trade;
  $("#detail").classList.remove("hidden");
  $("#detail-title").textContent = `Trade #${id} · ${t.side} ${t.tradingsymbol} · ${t.status}`;
  const orders = r.orders.map((o) => `<tr><td>${esc(o.kind)}</td><td>${esc(o.tag)}</td><td>${esc(o.broker_order_id)}</td><td>${esc(o.side)} ${esc(o.order_type)}</td>
    <td>${o.quantity}</td><td>${num(o.price)}${o.trigger_price ? " / trg " + num(o.trigger_price) : ""}</td><td>${esc(o.status)}</td><td>${o.filled_qty} @ ${num(o.avg_price)}</td><td>${esc(o.status_message)}</td></tr>`).join("");
  const evs = r.events.map((e) => `<tr><td>${tm(e.ts)}</td><td>${esc(e.level)}</td><td>${esc(e.event)}</td><td>${esc(e.from_status || "")}${e.to_status ? " → " + esc(e.to_status) : ""}</td><td><pre>${esc(e.detail ? JSON.stringify(e.detail) : "")}</pre></td></tr>`).join("");
  $("#detail-body").innerHTML = `<h3>Orders</h3><table><tr><th>Kind</th><th>Tag</th><th>Order id</th><th>Type</th><th>Qty</th><th>Price</th><th>Status</th><th>Filled</th><th>Message</th></tr>${orders}</table>
    <h3>Audit trail</h3><table><tr><th>Time</th><th>Level</th><th>Event</th><th>Status</th><th>Detail</th></tr>${evs}</table>`;
  $("#detail").scrollIntoView({behavior: "smooth"});
}

$("#pp-set").onclick = async () => {
  try { await api("/api/paper/price", {tradingsymbol: $("#pp-symbol").value, price: $("#pp-price").value}); refresh(); onContract(); }
  catch (e) { alertBox("Error", [e.message]); }
};

loadMeta().catch((e) => { $("#form-hint").textContent = "Could not load instruments: " + e.message; });
// No polling: the server pushes a "dashboard" event whenever trade/order state changes (and on reconnect).
// A slow refresh stays as a safety net for anything that is not a trade (e.g. system rows).
Live.onDashboard(refresh);
Live.start();
refresh();
setInterval(refresh, 30000);
