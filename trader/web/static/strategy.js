// Strategy Builder: builds a multi-leg order, previews/places every leg through the SAME safety-checked
// single-trade API the New Trade form uses (POST /api/strategies/trade_all just does that server-side, leg
// by leg), then shows combined P&L for the group. No trading logic lives here.
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const money = (v) => (v == null ? "–" : "₹" + Number(v).toLocaleString("en-IN", {maximumFractionDigits: 2}));
const num = (v) => (v == null || v === "" ? "–" : Number(v).toLocaleString("en-IN", {maximumFractionDigits: 2}));
let META = null, MODE = "PAPER";
let BASE_STRIKES = [], BASE_STEP = 50, SPOT = null;
let LEGS = [];
let NEXT_ID = 1;
let RESTORING = true;   // true only during the initial load, so later instrument changes don't re-apply stale saved picks

// ---------------------------------------------------------------- persisted builder state (survives refresh)
const STORE_KEY = "strategy:builder:v1";
function loadStored() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch (e) { return null; }
}
function saveStored() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      config: collectConfig(), underlying: $("#base-underlying").value, expiry: $("#base-expiry").value,
      legs: LEGS.map(({id, ...rest}) => rest),
    }));
  } catch (e) { /* private window etc: ignore */ }
}
const STORED = loadStored();

async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-Trader": "1"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({error: "bad response"}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// ---------------------------------------------------------------- theme
const THEME_KEY = "strategy:theme";
function initTheme() {
  let t = "dark";
  try { t = localStorage.getItem(THEME_KEY) || "dark"; } catch (e) { /* ignore */ }
  document.documentElement.setAttribute("data-theme", t);
  $("#theme-toggle").textContent = t === "dark" ? "🌙" : "☀️";
}
$("#theme-toggle").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", cur);
  $("#theme-toggle").textContent = cur === "dark" ? "🌙" : "☀️";
  try { localStorage.setItem(THEME_KEY, cur); } catch (e) { /* ignore */ }
};
initTheme();

// ---------------------------------------------------------------- order type hint
const OT_HINT = {
  MIS: "Zerodha intraday (MIS): auto square-off at your broker's own MIS cut-off.",
  CNC: "Normal carry-forward (placed as NRML). No app-level overnight handling beyond your own settings.",
  BTST: "Carried overnight by THIS app: skips the global square-off and resumes monitoring (SL/TP/trailing) on the next run, until it hits its own exit condition.",
};
function updateOrderTypeHint() { $("#order-type-hint").textContent = OT_HINT[$("input[name=order_type]:checked").value] || ""; }
$$("input[name=order_type]").forEach((el) => el.addEventListener("change", updateOrderTypeHint));
updateOrderTypeHint();

// ---------------------------------------------------------------- days of week (informational in Phase 1)
$$(".day").forEach((btn) => btn.addEventListener("click", () => { btn.classList.toggle("on"); saveStored(); }));
$("#config-panel").addEventListener("change", saveStored);
$("#config-panel").addEventListener("input", saveStored);

// ---------------------------------------------------------------- profit trailing conditional fields
function updateTrailingVisibility() {
  const mode = $("input[name=trailing_mode]:checked").value;
  $$(".conditional").forEach((el) => el.classList.toggle("show", el.dataset.for === mode));
}
$$("input[name=trailing_mode]").forEach((el) => el.addEventListener("change", updateTrailingVisibility));
updateTrailingVisibility();

// ---------------------------------------------------------------- move SL to cost
const slCostAt = $("#cfg-sl-cost-at");
function updateSlCostState() { slCostAt.disabled = !$("#cfg-sl-cost-enabled").checked; }
$("#cfg-sl-cost-enabled").addEventListener("change", updateSlCostState);
updateSlCostState();

// ---------------------------------------------------------------- base instrument / spot / strikes
async function loadMeta() {
  META = await api("/api/meta");
  MODE = META.mode;
  const u = $("#base-underlying");
  u.innerHTML = Object.keys(META.underlyings).map((k) => `<option>${esc(k)}</option>`).join("");
  if (STORED?.underlying && META.underlyings[STORED.underlying]) u.value = STORED.underlying;
  restoreConfig();
  await onBaseUnderlying();
}

function restoreConfig() {
  const c = STORED?.config;
  if (!c) return;
  if (c.order_type) { const el = $(`input[name=order_type][value="${c.order_type}"]`); if (el) el.checked = true; }
  if (c.start_time) $("#cfg-start-time").value = c.start_time;
  if (c.square_off_time) $("#cfg-square-off").value = c.square_off_time;
  if (Array.isArray(c.days)) $$(".day").forEach((b) => b.classList.toggle("on", c.days.includes(b.dataset.day)));
  if (c.exit_profit_amount != null) $("#cfg-exit-profit").value = c.exit_profit_amount;
  if (c.exit_loss_amount != null) $("#cfg-exit-loss").value = c.exit_loss_amount;
  if (c.no_trade_after) $("#cfg-no-trade-after").value = c.no_trade_after;
  if (c.trailing_mode) { const el = $(`input[name=trailing_mode][value="${c.trailing_mode}"]`); if (el) el.checked = true; }
  if (c.move_sl_to_cost_enabled) $("#cfg-sl-cost-enabled").checked = true;
  if (c.move_sl_to_cost_at != null) $("#cfg-sl-cost-at").value = c.move_sl_to_cost_at;
  const pairs = [["tr-lock-reaches", "lock_if_profit_reaches"], ["tr-lock-at", "lock_profit_at"],
                 ["tr-trail-every", "trail_every_increase"], ["tr-trail-by", "trail_profit_by"],
                 ["tr2-lock-reaches", "lock_if_profit_reaches"], ["tr2-lock-at", "lock_profit_at"],
                 ["tr2-trail-every", "trail_every_increase"], ["tr2-trail-by", "trail_profit_by"]];
  pairs.forEach(([id, key]) => { if (c[key] != null) $("#" + id).value = c[key]; });
  updateOrderTypeHint(); updateTrailingVisibility(); updateSlCostState();
}

async function onBaseUnderlying() {
  const info = META.underlyings[$("#base-underlying").value] || {};
  $("#base-expiry").innerHTML = (info.expiries || []).map((e) => `<option>${esc(e)}</option>`).join("");
  if (RESTORING && STORED?.expiry && (info.expiries || []).includes(STORED.expiry)) $("#base-expiry").value = STORED.expiry;
  $("#base-spot-symbol").textContent = $("#base-underlying").value;
  await refreshSpot();
  // Changing the instrument governs every leg row (their own expiry dropdown is built from THIS
  // underlying's expiries via `leg.underlying`, so it must be kept in sync or it keeps showing the old
  // instrument's dates) - strikes reset to the new ATM too, since a strike from a different instrument
  // is meaningless here.
  const u = $("#base-underlying").value;
  LEGS.forEach((l) => { l.underlying = u; });
  await onBaseExpiry(true);
  saveStored();
}

async function refreshSpot() {
  SPOT = null; $("#base-spot").textContent = "–";
  try {
    const s = await api(`/api/spot?ltp=1&underlying=${encodeURIComponent($("#base-underlying").value)}`);
    SPOT = s.spot; $("#base-spot").textContent = s.spot == null ? "no price" : money(s.spot);
  } catch (e) { /* leave as no price */ }
}
$("#refresh-spot").onclick = async () => { await refreshSpot(); renderLegs(); };
$("#refresh-prices").onclick = () => LEGS.forEach(fetchLegPrice);

async function onBaseExpiry(resetStrikes) {
  const u = $("#base-underlying").value, e = $("#base-expiry").value;
  if (!e) { BASE_STRIKES = []; return; }
  const r = await api(`/api/strikes?underlying=${encodeURIComponent(u)}&expiry=${encodeURIComponent(e)}`);
  BASE_STRIKES = r.strikes;
  // Step from strikes NEAR THE MIDDLE of the chain: the far ends of a real strike chain are often sparser
  // (irregular gaps), so measuring from strikes[0]/[1] would pick up the wrong, much wider spacing.
  const mid = Math.floor(BASE_STRIKES.length / 2);
  BASE_STEP = BASE_STRIKES.length > 1 ? Math.round(BASE_STRIKES[mid] - BASE_STRIKES[mid - 1]) : 50;
  const atm = atmStrike();
  LEGS.forEach((l) => {
    l.expiry = e;
    if (resetStrikes || !BASE_STRIKES.includes(l.strike)) l.strike = atm;
  });
  renderLegs();
  LEGS.forEach(fetchLegPrice);
}

function atmStrike() {
  if (!BASE_STRIKES.length) return null;
  if (SPOT == null) return BASE_STRIKES[Math.floor(BASE_STRIKES.length / 2)];
  return BASE_STRIKES.reduce((best, k) => Math.abs(k - SPOT) < Math.abs(best - SPOT) ? k : best, BASE_STRIKES[0]);
}

$("#base-underlying").addEventListener("change", onBaseUnderlying);
$("#base-expiry").addEventListener("change", onBaseExpiry);

// ---------------------------------------------------------------- legs
function newLeg(overrides) {
  const atm = atmStrike() || 0;
  const leg = {id: NEXT_ID++, side: "SELL", underlying: $("#base-underlying").value, expiry: $("#base-expiry").value,
              strike: atm, option_type: "CE", lots: 1, entry_price: null,
              sl_value: 20, sl_type: "POINTS", tp_value: null, tp_type: "POINTS", leg_role: "", checked: true};
  return Object.assign(leg, overrides);
}

function addLeg(overrides) {
  const leg = newLeg(overrides);
  LEGS.push(leg);
  renderLegs();
  fetchLegPrice(leg);
}

function removeLeg(id) { LEGS = LEGS.filter((l) => l.id !== id); renderLegs(); }

async function fetchLegPrice(leg) {
  if (!leg.expiry || !leg.strike) return;
  try {
    const c = await api(`/api/contract?ltp=1&underlying=${encodeURIComponent(leg.underlying)}&expiry=${leg.expiry}&strike=${leg.strike}&option_type=${leg.option_type}`);
    if (c.ltp != null) leg.entry_price = c.ltp;
  } catch (e) { /* leave price editable, empty */ }
  renderLegs();
}

function legRow(leg) {
  const expiries = (META.underlyings[leg.underlying]?.expiries || []).map((e) =>
    `<option value="${e}" ${e === leg.expiry ? "selected" : ""}>${e}</option>`).join("");
  return `<tr data-id="${leg.id}" class="${leg.checked ? "active-leg" : ""}">
    <td><input type="checkbox" class="leg-check" data-f="checked" ${leg.checked ? "checked" : ""}></td>
    <td><button type="button" class="bs-btn ${leg.side === "BUY" ? "buy" : "sell"}" data-act="side">${leg.side === "BUY" ? "B" : "S"}</button></td>
    <td><select data-f="expiry">${expiries}</select></td>
    <td><div class="num-cell strike-cell">
      <button type="button" class="stepper" data-act="strike-" title="-${BASE_STEP}">−</button>
      <input type="number" data-f="strike" value="${leg.strike}" step="${BASE_STEP}">
      <button type="button" class="stepper" data-act="strike+" title="+${BASE_STEP}">+</button>
    </div></td>
    <td><button type="button" class="bs-btn ${leg.option_type === "CE" ? "ce" : "pe"}" data-act="type">${leg.option_type}</button></td>
    <td class="lots-cell"><input type="number" min="1" step="1" data-f="lots" value="${leg.lots}"></td>
    <td class="price-cell"><input type="number" step="0.05" min="0.05" data-f="entry_price" value="${leg.entry_price ?? ""}" placeholder="LTP"></td>
    <td><div class="slp-cell">
      <input type="number" step="0.05" min="0" data-f="sl_value" value="${leg.sl_value ?? ""}">
      <select data-f="sl_type">
        <option value="POINTS" ${leg.sl_type === "POINTS" ? "selected" : ""}>Points</option>
        <option value="PERCENT" ${leg.sl_type === "PERCENT" ? "selected" : ""}>SL%</option>
        <option value="PRICE" ${leg.sl_type === "PRICE" ? "selected" : ""}>On price</option>
      </select></div></td>
    <td><div class="slp-cell">
      <input type="number" step="0.05" min="0" data-f="tp_value" value="${leg.tp_value ?? ""}">
      <select data-f="tp_type">
        <option value="POINTS" ${leg.tp_type === "POINTS" ? "selected" : ""}>Points</option>
        <option value="PERCENT" ${leg.tp_type === "PERCENT" ? "selected" : ""}>TP%</option>
        <option value="PRICE" ${leg.tp_type === "PRICE" ? "selected" : ""}>On price</option>
      </select></div></td>
    <td><button type="button" class="leg-del" data-act="del" title="Remove leg">✕</button></td>
  </tr>`;
}

function renderLegs() {
  $("#leg-tbody").innerHTML = LEGS.map(legRow).join("") ||
    `<tr><td colspan="10" class="hint small">No legs yet - add one or pick a template above.</td></tr>`;
  renderSummaries();
  renderCalc();
  saveStored();          // one choke point: every leg add/remove/edit/template/price-refresh calls this
}

function renderSummaries() {
  $("#leg-summaries").innerHTML = LEGS.map((l) => {
    const qty = l.lots;
    return `<div class="leg-summary"><b class="${l.side}">${l.side}</b> ${esc(l.option_type)} • ${esc(l.underlying)}
      • Strike ${l.strike}${l.strike === atmStrike() ? " (ATM)" : ""} • Lots ${qty}
      • SL ${l.sl_value ?? "–"}${l.sl_type === "PERCENT" ? "%" : l.sl_type === "PRICE" ? " (price)" : "pts"}
      • TP ${l.tp_value ?? "off"}${l.tp_value != null ? (l.tp_type === "PERCENT" ? "%" : l.tp_type === "PRICE" ? " (price)" : "pts") : ""}</div>`;
  }).join("");
}

function renderCalc() {
  const priced = LEGS.filter((l) => l.entry_price != null);
  if (!priced.length) {
    $("#calc-price-get").textContent = "–"; $("#calc-premium-get").textContent = "–"; $("#calc-charges").textContent = "–";
    return;
  }
  const net = LEGS.reduce((s, l) => s + (l.entry_price || 0) * l.lots * (l.side === "SELL" ? 1 : -1), 0);
  const lotSum = LEGS.reduce((s, l) => s + l.lots, 0);
  $("#calc-price-get").textContent = priced.map((l) => num(l.entry_price)).join(" / ");
  $("#calc-premium-get").textContent = money(net);
  $("#calc-charges").textContent = money(lotSum * 40);   // rough estimate only, not a broker figure
}

$("#leg-tbody").addEventListener("click", (ev) => {
  const row = ev.target.closest("tr[data-id]");
  if (!row) return;
  const id = Number(row.dataset.id), leg = LEGS.find((l) => l.id === id);
  const act = ev.target.dataset.act;
  if (!leg || !act) return;
  if (act === "del") return removeLeg(id);
  if (act === "side") { leg.side = leg.side === "BUY" ? "SELL" : "BUY"; return renderLegs(); }
  if (act === "type") {
    leg.option_type = leg.option_type === "CE" ? "PE" : "CE";
    renderLegs(); fetchLegPrice(leg);
    return;
  }
  if (act === "strike-" || act === "strike+") {
    leg.strike = Math.round((leg.strike + (act === "strike+" ? BASE_STEP : -BASE_STEP)) * 100) / 100;
    renderLegs(); fetchLegPrice(leg);
  }
});
$("#leg-tbody").addEventListener("change", (ev) => {
  const row = ev.target.closest("tr[data-id]");
  if (!row) return;
  const id = Number(row.dataset.id), leg = LEGS.find((l) => l.id === id);
  const f = ev.target.dataset.f;
  if (!leg || !f) return;
  if (f === "checked") leg.checked = ev.target.checked;
  else if (["strike", "lots", "entry_price", "sl_value", "tp_value"].includes(f)) leg[f] = ev.target.value === "" ? null : Number(ev.target.value);
  else leg[f] = ev.target.value;
  const refetch = f === "strike" || f === "expiry";
  renderLegs();
  if (refetch) fetchLegPrice(leg);
});

$("#add-leg").onclick = () => addLeg();

// ---------------------------------------------------------------- templates
function applyTemplate(kind) {
  const atm = atmStrike();
  if (atm == null) { $("#form-hint").textContent = "pick an instrument/expiry first"; return; }
  const step = BASE_STEP;
  const base = {underlying: $("#base-underlying").value, expiry: $("#base-expiry").value};
  LEGS = [];
  const add = (o) => LEGS.push(newLeg({...base, ...o}));
  if (kind === "straddle") {
    add({side: "SELL", option_type: "CE", strike: atm});
    add({side: "SELL", option_type: "PE", strike: atm});
  } else if (kind === "strangle") {
    add({side: "SELL", option_type: "CE", strike: atm + 2 * step});
    add({side: "SELL", option_type: "PE", strike: atm - 2 * step});
  } else if (kind === "iron_condor") {
    add({side: "SELL", option_type: "CE", strike: atm + 2 * step, leg_role: "SHORT_CE"});
    add({side: "BUY", option_type: "CE", strike: atm + 6 * step, leg_role: "LONG_CE_WING"});
    add({side: "SELL", option_type: "PE", strike: atm - 2 * step, leg_role: "SHORT_PE"});
    add({side: "BUY", option_type: "PE", strike: atm - 6 * step, leg_role: "LONG_PE_WING"});
  } else if (kind === "iron_fly") {
    add({side: "SELL", option_type: "CE", strike: atm, leg_role: "SHORT_CE"});
    add({side: "SELL", option_type: "PE", strike: atm, leg_role: "SHORT_PE"});
    add({side: "BUY", option_type: "CE", strike: atm + 4 * step, leg_role: "LONG_CE_WING"});
    add({side: "BUY", option_type: "PE", strike: atm - 4 * step, leg_role: "LONG_PE_WING"});
  }
  renderLegs();
  LEGS.forEach(fetchLegPrice);
}
$$(".tmpl-btn").forEach((b) => b.addEventListener("click", () => applyTemplate(b.dataset.tmpl)));

// ---------------------------------------------------------------- config collection
function collectConfig() {
  const mode = $("input[name=trailing_mode]:checked").value;
  const cfg = {
    order_type: $("input[name=order_type]:checked").value,
    start_time: $("#cfg-start-time").value || null,
    square_off_time: $("#cfg-square-off").value || null,
    days: $$(".day.on").map((b) => b.dataset.day),
    exit_profit_amount: $("#cfg-exit-profit").value || null,
    exit_loss_amount: $("#cfg-exit-loss").value || null,
    no_trade_after: $("#cfg-no-trade-after").value || null,
    trailing_mode: mode,
    move_sl_to_cost_enabled: $("#cfg-sl-cost-enabled").checked,
    move_sl_to_cost_at: $("#cfg-sl-cost-at").value || null,
  };
  if (mode === "LOCK_FIX") { cfg.lock_if_profit_reaches = $("#tr-lock-reaches").value || null; cfg.lock_profit_at = $("#tr-lock-at").value || null; }
  if (mode === "TRAIL") { cfg.trail_every_increase = $("#tr-trail-every").value || null; cfg.trail_profit_by = $("#tr-trail-by").value || null; }
  if (mode === "LOCK_AND_TRAIL") {
    cfg.lock_if_profit_reaches = $("#tr2-lock-reaches").value || null; cfg.lock_profit_at = $("#tr2-lock-at").value || null;
    cfg.trail_every_increase = $("#tr2-trail-every").value || null; cfg.trail_profit_by = $("#tr2-trail-by").value || null;
  }
  return cfg;
}

// ---------------------------------------------------------------- dialogs (mirrors app.js)
function dialog(title, html, live = false) {
  const d = $("#dlg");
  $("#dlg-title").textContent = title;
  $("#dlg-body").innerHTML = html;
  $("#dlg-live").classList.toggle("hidden", !live);
  $("#dlg-ok").classList.remove("hidden");
  return new Promise((resolve) => { d.onclose = () => resolve(d.returnValue === "ok"); d.returnValue = ""; d.showModal(); });
}
function alertBox(title, lines) { dialog(title, `<ul>${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`); $("#dlg-ok").classList.add("hidden"); }

// ---------------------------------------------------------------- trade all
$("#trade-all").onclick = async () => {
  const active = LEGS.filter((l) => l.checked);
  if (!active.length) return alertBox("Nothing to trade", ["select at least one leg"]);
  const rows = active.map((l) => `<tr><td>${l.side} ${l.option_type}</td><td>${esc(l.underlying)} ${l.strike}</td>
    <td>${l.lots} lot(s)</td><td>${l.entry_price == null ? "market/no price" : money(l.entry_price)}</td></tr>`).join("");
  const ok = await dialog(`Trade ${active.length} leg(s)?`,
    `<table><tr><th>Side</th><th>Contract</th><th>Qty</th><th>Price</th></tr>${rows}</table>`, MODE === "LIVE");
  if (!ok) return;
  $("#trade-all").disabled = true;
  try {
    const res = await api("/api/strategies/trade_all", {config: collectConfig(), legs: active});
    if (!res.ok) return alertBox("Not placed", res.errors || ["unknown error"]);
    $("#form-hint").textContent = `Strategy #${res.strategy_id}: ${res.confirmed.length} leg(s) placed` +
      (res.failed.length ? `, ${res.failed.length} failed - check the strategy card below` : "");
    refreshStrategies();
  } catch (e) { alertBox("Error", [e.message]); }
  finally { $("#trade-all").disabled = false; }
};
$("#save-draft").onclick = () => alertBox("Drafts", ["Saving drafts for later/recurring runs is planned for a later phase; Trade All places the strategy now."]);

// ---------------------------------------------------------------- active strategies
async function exitStrategy(id) {
  const ok = await dialog("Exit this strategy?", "<p>Every open leg will be exited at a marketable price.</p>", MODE === "LIVE");
  if (!ok) return;
  try { await api(`/api/strategies/${id}/exit`, {}); refreshStrategies(); }
  catch (e) { alertBox("Error", [e.message]); }
}

const LEG_LIVE_STATUSES = new Set(["ENTRY_ORDER_PLACED", "ENTRY_PENDING", "ENTRY_EXECUTED", "POSITION_ACTIVE"]);

function strategyCard(s) {
  const cfg = s.config;
  const legRows = s.legs.map((t) => {
    const canExit = LEG_LIVE_STATUSES.has(t.status) && t.filled_qty > 0 && !t.pending_exit_reason;
    return `<tr><td>${t.side}</td><td>${esc(t.tradingsymbol)}</td><td>${t.quantity}</td>
    <td>${num(t.entry_avg_price ?? t.entry_price)}</td><td>${num(t.kite_ltp ?? t.last_ltp)}</td>
    <td class="${(t.pnl || 0) >= 0 ? "pos" : "neg"}">${money(t.pnl)}</td>
    <td>${esc(t.status)}${t.pending_exit_reason ? " → " + esc(t.pending_exit_reason) : ""}</td>
    <td>${canExit ? `<button type="button" class="danger" data-leg-exit="${t.id}">Exit</button>` : ""}</td></tr>`;
  }).join("");
  const cls = s.combined_pnl >= 0 ? "pos" : "neg";
  return `<div class="strategy-card">
    <div class="head">
      <span class="name">#${s.id} ${esc(s.name)}</span>
      <span class="status-pill ${esc(s.status)}">${esc(s.status)}</span>
      <span>order type ${esc(cfg.order_type)}</span>
      <span class="pnl ${cls}">${money(s.combined_pnl)}</span>
      ${s.status === "ACTIVE" && s.open_legs > 0 ? `<button type="button" class="danger" data-exit="${s.id}">Exit strategy</button>` : ""}
    </div>
    <table><tr><th>Side</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>LTP</th><th>P&amp;L</th><th>Status</th><th></th></tr>${legRows}</table>
  </div>`;
}

async function exitLeg(tid) {
  try {
    const p = await api(`/api/trades/${tid}/prepare`, {action: "EXIT"});
    const t = p.trade;
    const ok = await dialog("Exit this leg?",
      `<div class="big-side ${esc(t.side)}">${esc(t.side)} ${esc(t.tradingsymbol)}</div><p>Exit ${t.open_qty} at a marketable price.</p>`,
      MODE === "LIVE");
    if (!ok) return;
    await api(`/api/trades/${tid}/exit`, {token: p.token});
    refreshStrategies();
  } catch (e) { alertBox("Error", [e.message]); }
}

async function refreshStrategies() {
  let d;
  try { d = await api("/api/dashboard"); } catch (e) { $("#banner").textContent = "Server unreachable: " + e.message; return; }
  const b = $("#banner");
  b.className = "banner" + (d.live ? " live" : "");
  b.textContent = d.live ? "● LIVE TRADING: orders go to Zerodha with real money" : "PAPER TRADING: simulated exchange, no orders reach Zerodha";
  MODE = d.mode;
  const h = $("#halt");
  h.classList.toggle("hidden", !d.halted);
  if (d.halted) h.textContent = `New trades blocked: ${d.halted}`;
  try {
    const sd = await api("/api/strategies");
    $("#strategies-list").innerHTML = sd.strategies.map(strategyCard).join("") || `<p class="hint small">No strategies yet.</p>`;
  } catch (e) { /* keep the last render */ }
}
$("#strategies-list").addEventListener("click", (ev) => {
  const id = ev.target.dataset.exit;
  if (id) return exitStrategy(Number(id));
  const lid = ev.target.dataset.legExit;
  if (lid) return exitLeg(Number(lid));
});

// ---------------------------------------------------------------- init
loadMeta().then(() => {
  if (STORED?.legs?.length) {
    LEGS = STORED.legs.map((l) => ({...l, id: NEXT_ID++}));
    renderLegs();
    LEGS.forEach(fetchLegPrice);          // prices go stale fast; always re-fetch rather than trust a saved one
  } else {
    addLeg();
  }
  RESTORING = false;
}).catch((e) => { $("#form-hint").textContent = "Could not load instruments: " + e.message; RESTORING = false; });
refreshStrategies();
setInterval(refreshStrategies, 2000);
