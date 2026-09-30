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

// Skeleton rows in place of the legs table the instant the instrument changes, so the OLD instrument's
// data disappears immediately instead of sitting on screen (looking live) until the new one finishes
// loading - renderLegs() below overwrites this the moment the real data is ready. Built with the SAME
// 11 <td>s as a real leg row (not one colspan cell) so it inherits the exact same row height from
// .leg-table td's own padding - a shorter placeholder caused a layout jump when it swapped for real rows.
function legSkeletonRow() {
  const w = [14, 16, 28, 90, 70, 28, 36, 50, 90, 90, 20];
  return `<tr class="skeleton-leg">${w.map((px) => `<td><div class="skeleton-bar" style="width:${px}px"></div></td>`).join("")}</tr>`;
}
function showLegsLoading() {
  const rows = Math.max(LEGS.length, 1);
  $("#leg-tbody").innerHTML = legSkeletonRow().repeat(rows);
}

async function onBaseUnderlying() {
  const info = META.underlyings[$("#base-underlying").value] || {};
  $("#base-expiry").innerHTML = (info.expiries || []).map((e) => `<option>${esc(e)}</option>`).join("");
  if (RESTORING && STORED?.expiry && (info.expiries || []).includes(STORED.expiry)) $("#base-expiry").value = STORED.expiry;
  $("#base-spot-symbol").textContent = $("#base-underlying").value;
  $("#base-spot").dataset.price = `SPOT:${$("#base-underlying").value}`;   // pushed by live.js
  $("#base-spot").dataset.fmt = "money";
  Live.watch([$("#base-spot").dataset.price]);
  if (!RESTORING) showLegsLoading();
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

async function refreshSpot(force, keepDisplay) {
  if (!keepDisplay) { SPOT = null; $("#base-spot").textContent = "–"; }   // switching instrument: old price is meaningless
  try {
    const s = await api(`/api/spot?ltp=1${force ? "&force=1" : ""}&underlying=${encodeURIComponent($("#base-underlying").value)}`);
    SPOT = s.spot; $("#base-spot").textContent = s.spot == null ? "no price" : money(s.spot);
    $("#base-spot").title = s.error || "";
    if (s.error) $("#form-hint").textContent = s.error;
  } catch (e) { /* leave as no price */ }
}
function flash(el) { if (el) { el.classList.remove("value-flash"); void el.offsetWidth; el.classList.add("value-flash"); } }
// An explicit button click always gets a real Breeze call (force=1, bypasses the quote-interval cache) -
// the passive auto-fetch on selection keeps the normal cache so browsing strikes doesn't burn the budget.
// The button spins while the call is in flight and the price flashes on return - even when the fetched
// price happens to equal what was already shown, the click visibly did something (Sensibull-style cue).
$("#refresh-spot").onclick = async () => {
  const btn = $("#refresh-spot");
  btn.classList.add("spinning");
  try { await refreshSpot(true, true); renderLegs(); flash($("#base-spot")); }
  finally { btn.classList.remove("spinning"); }
};
$("#refresh-prices").onclick = async () => {
  const btn = $("#refresh-prices");
  btn.classList.add("spinning");
  try {
    await Promise.all(LEGS.map((l) => fetchLegPrice(l, true)));
    $$(".price-cell input").forEach(flash);
  } finally {
    btn.classList.remove("spinning");
  }
};

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
function currentMultiplier() { return Number($("#lot-multiplier").value) || 1; }

function newLeg(overrides) {
  const atm = atmStrike() || 0;
  // SL/TP are optional by default: the strategy's own combined profit/loss exit can cover a leg instead
  // (see the global config panel), so a blank per-leg value is a valid, common choice, not an oversight.
  // base_lots is the leg's OWN lot count at 1x - the multiplier dropdown scales every leg from this base
  // (Sensibull-style), so it stays correct no matter how many times the multiplier is changed.
  const baseLots = (overrides && overrides.base_lots) || 1;
  const leg = {id: NEXT_ID++, side: "SELL", underlying: $("#base-underlying").value, expiry: $("#base-expiry").value,
              strike: atm, option_type: "CE", base_lots: baseLots, lots: baseLots * currentMultiplier(), entry_price: null,
              sl_value: null, sl_type: "POINTS", tp_value: null, tp_type: "POINTS", leg_role: "", checked: true};
  return Object.assign(leg, overrides);
}

function addLeg(overrides) {
  const leg = newLeg(overrides);
  LEGS.push(leg);
  renderLegs();
  fetchLegPrice(leg);
}

function removeLeg(id) { LEGS = LEGS.filter((l) => l.id !== id); renderLegs(); }

async function fetchLegPrice(leg, force) {
  if (!leg.expiry || !leg.strike) return;
  try {
    const c = await api(`/api/contract?ltp=1${force ? "&force=1" : ""}&underlying=${encodeURIComponent(leg.underlying)}&expiry=${leg.expiry}&strike=${leg.strike}&option_type=${leg.option_type}`);
    // A price belongs to one contract: when this contract has no price (e.g. none from the data source),
    // never keep the previous contract's price (switching NIFTY -> GOLDM left FINNIFTY's prices in place).
    // A price typed by hand for this same contract is kept.
    const key = `${leg.underlying}|${leg.expiry}|${leg.strike}|${leg.option_type}`;
    if (c.ltp != null) { leg.entry_price = c.ltp; leg.priced_for = key; }
    else if (leg.priced_for !== key) { leg.entry_price = null; leg.priced_for = key; }
    if (c.price_error) $("#form-hint").textContent = c.price_error;
    if (c.lot_size != null) leg.lot_size = c.lot_size;      // needed for the payoff chart's rupee scale
  } catch (e) { /* leave price editable, empty */ }
  renderLegs();
}

function legRow(leg) {
  const expiries = (META.underlyings[leg.underlying]?.expiries || []).map((e) =>
    `<option value="${e}" ${e === leg.expiry ? "selected" : ""}>${e}</option>`).join("");
  return `<tr data-id="${leg.id}" class="${leg.checked ? "active-leg" : ""}">
    <td class="drag-handle" title="Drag to reorder">⠿</td>
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
      <input type="number" step="0.05" min="0" data-f="sl_value" value="${leg.sl_value ?? ""}" placeholder="optional">
      <select data-f="sl_type">
        <option value="POINTS" ${leg.sl_type === "POINTS" ? "selected" : ""}>Points</option>
        <option value="PERCENT" ${leg.sl_type === "PERCENT" ? "selected" : ""}>SL%</option>
        <option value="PRICE" ${leg.sl_type === "PRICE" ? "selected" : ""}>On price</option>
      </select></div></td>
    <td><div class="slp-cell">
      <input type="number" step="0.05" min="0" data-f="tp_value" value="${leg.tp_value ?? ""}" placeholder="optional">
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
    `<tr><td colspan="11" class="hint small">No legs yet - add one or pick a template above.</td></tr>`;
  renderSummaries();
  renderCalc();
  renderPayoff();
  saveStored();          // one choke point: every leg add/remove/edit/template/price-refresh calls this
}

// ---------------------------------------------------------------- payoff-at-expiry chart
// The curve/Max profit/Max loss below are the classic "if held to expiry" payoff - intrinsic value only,
// the same as any options payoff diagram, deliberately ignoring SL/TP (see renderPayoff()'s separate
// "if SL/TP hit" figure for that).
function legPayoffAtExpiry(spot, leg) {
  const qty = leg.lots * (leg.lot_size || 1);
  const premium = leg.entry_price || 0;
  const intrinsic = leg.option_type === "CE" ? Math.max(spot - leg.strike, 0) : Math.max(leg.strike - spot, 0);
  const perUnit = leg.side === "BUY" ? (intrinsic - premium) : (premium - intrinsic);
  return perUnit * qty;
}

function combinedPayoff(spot, legs) { return legs.reduce((s, l) => s + legPayoffAtExpiry(spot, l), 0); }

// Slope of the combined payoff far to the right (spot -> +inf) / far to the left (spot -> 0): only CE legs
// matter on the right, only PE legs on the left (the other side's intrinsic value is flat out there). A
// non-zero slope means that side of the structure is genuinely open-ended, not just "off the chart".
function tailSlopes(legs) {
  const unit = (l) => l.lots * (l.lot_size || 1) * (l.side === "BUY" ? 1 : -1);
  return {
    right: legs.filter((l) => l.option_type === "CE").reduce((s, l) => s + unit(l), 0),
    left: legs.filter((l) => l.option_type === "PE").reduce((s, l) => s + unit(l), 0),
  };
}

// The SL/TP actually configured on each leg, resolved from points/percent/price to an absolute option
// price exactly the way the backend does it (strategy.py's _resolve_price) - used only for the separate
// "if SL/TP hit" rupee figure, never for the expiry curve above.
function legSlTpPrices(leg) {
  const entry = leg.entry_price;
  if (entry == null) return {slPrice: null, tpPrice: null};
  const buy = leg.side === "BUY";
  const resolve = (kind, value, typ) => {
    if (value == null) return null;
    if (typ === "PRICE") return value;
    const up = kind === "tp" ? buy : !buy;          // BUY: TP above entry, SL below. SELL: the reverse.
    const delta = typ === "PERCENT" ? (entry * value) / 100 : value;
    return up ? entry + delta : entry - delta;
  };
  return {slPrice: resolve("sl", leg.sl_value, leg.sl_type), tpPrice: resolve("tp", leg.tp_value, leg.tp_type)};
}

// Rupee P&L for one leg if ITS OWN configured SL (or TP) actually triggers - null when that leg has no
// SL (or no TP) set, so the caller can tell "zero" apart from "not configured".
function legSlTpPnl(leg) {
  const qty = leg.lots * (leg.lot_size || 1);
  const buy = leg.side === "BUY";
  const {slPrice, tpPrice} = legSlTpPrices(leg);
  const slPnl = slPrice != null ? (buy ? (slPrice - leg.entry_price) : (leg.entry_price - slPrice)) * qty : null;
  const tpPnl = tpPrice != null ? (buy ? (tpPrice - leg.entry_price) : (leg.entry_price - tpPrice)) * qty : null;
  return {slPnl, tpPnl};
}

function renderPayoff() {
  const legs = LEGS.filter((l) => l.checked && l.strike && l.entry_price != null);
  const box = $("#payoff-chart"), stats = $("#payoff-stats");
  if (legs.length < LEGS.filter((l) => l.checked).length) {
    box.innerHTML = `<p class="hint small">Waiting on a price for every leg…</p>`;
    stats.innerHTML = "";
    return;
  }
  if (!legs.length) { box.innerHTML = `<p class="hint small">Add a leg to see its payoff.</p>`; stats.innerHTML = ""; return; }

  const strikes = legs.map((l) => l.strike);
  const spread = Math.max(...strikes) - Math.min(...strikes);
  const pad = Math.max(BASE_STEP * 6, spread * 0.6, 1);
  const lo = Math.min(...strikes) - pad, hi = Math.max(...strikes) + pad;
  const xs = new Set([lo, hi]);
  for (let i = 0; i <= 100; i++) xs.add(lo + ((hi - lo) * i) / 100);
  strikes.forEach((k) => { xs.add(k - 0.01); xs.add(k); xs.add(k + 0.01); });   // land exactly on the kinks
  const points = Array.from(xs).sort((a, b) => a - b).map((x) => [x, combinedPayoff(x, legs)]);

  let maxY = Math.max(0, ...points.map((p) => p[1])), minY = Math.min(0, ...points.map((p) => p[1]));
  if (maxY === minY) { maxY += 1; minY -= 1; }
  const breakevens = [];
  for (let i = 1; i < points.length; i++) {
    const [x0, y0] = points[i - 1], [x1, y1] = points[i];
    if ((y0 < 0 && y1 >= 0) || (y0 > 0 && y1 <= 0)) breakevens.push(x0 + (x1 - x0) * (0 - y0) / (y1 - y0));
  }

  const W = 640, H = 240, mL = 54, mR = 14, mT = 14, mB = 26;
  const pw = W - mL - mR, ph = H - mT - mB;
  const xScale = (x) => mL + ((x - lo) / (hi - lo)) * pw;
  const yScale = (y) => mT + ((maxY - y) / (maxY - minY)) * ph;
  const zeroY = yScale(0);

  const path = `M ${xScale(points[0][0])},${zeroY} ` +
    points.map(([x, y]) => `L ${xScale(x)},${yScale(y)}`).join(" ") +
    ` L ${xScale(points[points.length - 1][0])},${zeroY} Z`;
  const linePath = `M ` + points.map(([x, y]) => `${xScale(x)},${yScale(y)}`).join(" L ");
  const zeroFrac = ((maxY - 0) / (maxY - minY)) * 100;

  const strikeLines = [...new Set(strikes)].map((k) =>
    `<line x1="${xScale(k)}" y1="${mT}" x2="${xScale(k)}" y2="${mT + ph}" class="strike-line"/>
     <text x="${xScale(k)}" y="${H - 8}" class="axis-label" text-anchor="middle">${k}</text>`).join("");
  const beMarks = breakevens.map((be) =>
    `<circle cx="${xScale(be)}" cy="${zeroY}" r="3.5" class="be-dot"/>
     <text x="${xScale(be)}" y="${zeroY - 8}" class="axis-label be-label" text-anchor="middle">${Math.round(be)}</text>`).join("");
  const curSpotLine = SPOT != null && SPOT >= lo && SPOT <= hi
    ? `<line x1="${xScale(SPOT)}" y1="${mT}" x2="${xScale(SPOT)}" y2="${mT + ph}" class="spot-line"/>
       <text x="${xScale(SPOT)}" y="${mT + 10}" class="axis-label spot-label" text-anchor="middle">Spot</text>` : "";

  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="payoff-svg">
    <defs><linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="var(--buy)" stop-opacity=".35"/>
      <stop offset="${zeroFrac}%" stop-color="var(--buy)" stop-opacity=".08"/>
      <stop offset="${zeroFrac}%" stop-color="var(--sell)" stop-opacity=".08"/>
      <stop offset="100%" stop-color="var(--sell)" stop-opacity=".35"/>
    </linearGradient></defs>
    ${strikeLines}
    <line x1="${mL}" y1="${zeroY}" x2="${mL + pw}" y2="${zeroY}" class="zero-line"/>
    <path d="${path}" fill="url(#pnlGrad)" stroke="none"/>
    <path d="${linePath}" fill="none" class="payoff-line"/>
    ${curSpotLine}
    ${beMarks}
    <text x="${mL - 6}" y="${yScale(maxY) + 4}" class="axis-label" text-anchor="end">${money(Math.round(maxY))}</text>
    <text x="${mL - 6}" y="${yScale(minY) + 4}" class="axis-label" text-anchor="end">${money(Math.round(minY))}</text>
  </svg>`;

  const slopes = tailSlopes(legs);
  const maxProfit = slopes.right > 0 ? "Unlimited" : money(Math.round(maxY));
  const maxLoss = (slopes.right < 0 || slopes.left < 0) ? "Unlimited" : money(Math.round(minY));
  const beText = breakevens.length ? breakevens.map((b) => Math.round(b)).join(" / ") : "none in range";
  const netPremium = legs.reduce((s, l) => s + (l.entry_price || 0) * l.lots * (l.lot_size || 1) * (l.side === "SELL" ? 1 : -1), 0);
  stats.innerHTML = `<div>Max profit (at expiry) <output class="pos">${maxProfit}</output></div>
    <div>Max loss (at expiry) <output class="neg">${maxLoss}</output></div>
    <div>Breakeven <output>${beText}</output></div>
    <div>Net premium <output class="${netPremium >= 0 ? "pos" : "neg"}">${money(Math.round(netPremium))} ${netPremium >= 0 ? "credit" : "debit"}</output></div>`;

  // Separate from the expiry curve above: what you'd actually realise if every leg's OWN configured SL /
  // TP triggers (this is how positions actually close in this app - almost never held to expiry).
  const slTp = legs.map((l) => ({leg: l, ...legSlTpPnl(l)}));
  const withSL = slTp.filter((r) => r.slPnl != null), withTP = slTp.filter((r) => r.tpPnl != null);
  const slSum = withSL.reduce((s, r) => s + r.slPnl, 0), tpSum = withTP.reduce((s, r) => s + r.tpPnl, 0);
  const slNote = withSL.length < legs.length ? ` <small>(${legs.length - withSL.length} leg(s) with no SL not counted)</small>` : "";
  const tpNote = withTP.length < legs.length ? ` <small>(${legs.length - withTP.length} leg(s) with no TP not counted)</small>` : "";
  $("#payoff-sltp").innerHTML = (withSL.length || withTP.length) ? `
    <div>If SL hit <output class="neg">${withSL.length ? money(Math.round(slSum)) : "–"}</output>${slNote}</div>
    <div>If TP hit <output class="pos">${withTP.length ? money(Math.round(tpSum)) : "–"}</output>${tpNote}</div>` :
    `<p class="hint small">No leg has an SL or TP set - nothing to show here (per-leg SL/TP is optional).</p>`;
}

function renderSummaries() {
  $("#leg-summaries").innerHTML = LEGS.map((l) => {
    const qty = l.lots;
    return `<div class="leg-summary"><b class="${l.side}">${l.side}</b> ${esc(l.option_type)} • ${esc(l.underlying)}
      • Strike ${l.strike}${l.strike === atmStrike() ? " (ATM)" : ""} • Lots ${qty}
      • SL ${l.sl_value ?? "auto (wide)"}${l.sl_value != null ? (l.sl_type === "PERCENT" ? "%" : l.sl_type === "PRICE" ? " (price)" : "pts") : ""}
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
  else if (f === "lots") {
    const v = ev.target.value === "" ? null : Number(ev.target.value);
    leg.lots = v;
    // A manual per-leg edit becomes that leg's new 1x baseline, so a later multiplier change scales from
    // what the user just typed rather than silently overwriting it.
    if (v != null) leg.base_lots = Math.max(1, Math.round(v / currentMultiplier()));
  } else if (["strike", "entry_price", "sl_value", "tp_value"].includes(f)) leg[f] = ev.target.value === "" ? null : Number(ev.target.value);
  else leg[f] = ev.target.value;
  const refetch = f === "strike" || f === "expiry";
  renderLegs();
  if (refetch) fetchLegPrice(leg);
});

$("#add-leg").onclick = () => addLeg();
$("#lot-multiplier").addEventListener("change", () => {
  const m = currentMultiplier();
  LEGS.forEach((l) => { l.lots = (l.base_lots || 1) * m; });   // each leg's own Lots cell stays editable after
  renderLegs();
});

// ---------------------------------------------------------------- drag to reorder
// ONE implementation for every re-orderable table (the builder's legs AND the order-confirmation popup): drag the
// ⠿ handle; the dragged row fades (and follows the cursor as the browser's drag image), and an accent line shows
// where it will land - above or below the row under the pointer. onMove(fromKey, overKey, above) does the move.
// Mouse/pen use native HTML5 drag; the row is draggable ONLY while its handle is held (a permanently draggable
// row swallows ordinary clicks on its inputs/buttons: real clicks drift a little and read as drag starts).
// Touch uses pointer events with the same visual cues, since native drag does not start from a finger.
function enableRowDrag(container, keyAttr, onMove) {
  const rowOf = (el) => el?.closest?.(`tr[${keyAttr}]`);
  const keyOf = (row) => row.getAttribute(keyAttr);
  let dragKey = null;
  const clearMarks = () => container.querySelectorAll("tr.drop-above, tr.drop-below")
    .forEach((r) => r.classList.remove("drop-above", "drop-below"));
  const isAbove = (row, y) => y < row.getBoundingClientRect().top + row.offsetHeight / 2;
  const mark = (row, y) => {
    clearMarks();
    if (row && dragKey !== null && keyOf(row) !== dragKey) row.classList.add(isAbove(row, y) ? "drop-above" : "drop-below");
  };
  const finish = (row, y) => {
    const from = dragKey;
    dragKey = null;
    clearMarks();
    container.querySelectorAll("tr.dragging").forEach((r) => r.classList.remove("dragging"));
    if (row && from !== null && keyOf(row) !== from) onMove(from, keyOf(row), isAbove(row, y));
  };
  const disarm = () => container.querySelectorAll(`tr[${keyAttr}]`).forEach((r) => { r.draggable = false; });
  container.addEventListener("mousedown", (ev) => {
    if (!ev.target.closest(".drag-handle")) return;
    const row = rowOf(ev.target);
    if (row) row.draggable = true;
  });
  container.addEventListener("mouseup", disarm);
  container.addEventListener("dragstart", (ev) => {
    const row = rowOf(ev.target);
    if (!row) return;
    dragKey = keyOf(row);
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", dragKey);   // Firefox requires data to be set to drag at all
    row.classList.add("dragging");
  });
  container.addEventListener("dragend", (ev) => {
    rowOf(ev.target)?.classList.remove("dragging");
    disarm();
    clearMarks();
    dragKey = null;
  });
  container.addEventListener("dragover", (ev) => {
    if (dragKey === null) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
    mark(rowOf(ev.target), ev.clientY);
  });
  container.addEventListener("dragleave", (ev) => { if (!container.contains(ev.relatedTarget)) clearMarks(); });
  container.addEventListener("drop", (ev) => {
    if (dragKey === null) return;
    ev.preventDefault();
    finish(rowOf(ev.target), ev.clientY);
    disarm();
  });
  container.addEventListener("pointerdown", (ev) => {          // touch only; mouse/pen take the native path
    if (ev.pointerType !== "touch" || !ev.target.closest(".drag-handle")) return;
    const row = rowOf(ev.target);
    if (!row) return;
    ev.preventDefault();
    dragKey = keyOf(row);
    row.classList.add("dragging");
    const at = (e) => rowOf(document.elementFromPoint(e.clientX, e.clientY));
    const move = (e) => { if (e.pointerId === ev.pointerId) mark(at(e), e.clientY); };
    const up = (e) => {
      if (e.pointerId !== ev.pointerId) return;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      finish(e.type === "pointerup" ? at(e) : null, e.clientY);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  });
}

// Builder legs (table order = the strategy's leg order).
enableRowDrag($("#leg-tbody"), "data-id", (fromKey, overKey, above) => {
  const from = LEGS.findIndex((l) => l.id === Number(fromKey));
  const overIndex = LEGS.findIndex((l) => l.id === Number(overKey));
  if (from === -1 || overIndex === -1) return;
  let insertAt = above ? overIndex : overIndex + 1;
  const [moved] = LEGS.splice(from, 1);        // removing `from` shifts every later index left by one
  if (from < insertAt) insertAt -= 1;
  LEGS.splice(insertAt, 0, moved);
  renderLegs();
});

// Order-confirmation popup (popup order = execution order). Registered once on the dialog body, which persists
// while its table is redrawn; confirmOrders() sets the handler while the popup is open.
let ORDER_MOVE = null;
enableRowDrag($("#dlg-body"), "data-row", (fromKey, overKey, above) => ORDER_MOVE?.(Number(fromKey), Number(overKey), above));

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
    // Display order BUY-SELL-SELL-BUY (wings on the outside, shorts together in the middle) - this is
    // cosmetic only. Trade All always places BUY legs before SELL legs regardless of this list's order
    // (see strategy.py's create_and_trade), so margin safety doesn't depend on this ordering.
    add({side: "BUY", option_type: "PE", strike: atm - 6 * step, leg_role: "LONG_PE_WING"});
    add({side: "SELL", option_type: "PE", strike: atm - 2 * step, leg_role: "SHORT_PE"});
    add({side: "SELL", option_type: "CE", strike: atm + 2 * step, leg_role: "SHORT_CE"});
    add({side: "BUY", option_type: "CE", strike: atm + 6 * step, leg_role: "LONG_CE_WING"});
  } else if (kind === "iron_fly") {
    add({side: "BUY", option_type: "PE", strike: atm - 4 * step, leg_role: "LONG_PE_WING"});
    add({side: "SELL", option_type: "PE", strike: atm, leg_role: "SHORT_PE"});
    add({side: "SELL", option_type: "CE", strike: atm, leg_role: "SHORT_CE"});
    add({side: "BUY", option_type: "CE", strike: atm + 4 * step, leg_role: "LONG_CE_WING"});
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
// Order confirmation: the EXECUTION order (default: all BUY legs, then SELL legs - hedges first) and Market/Limit
// per leg. Re-arranging here never changes the leg order in the builder table above.
function confirmOrders(active) {
  let rows = [...active.filter((l) => l.side === "BUY"), ...active.filter((l) => l.side !== "BUY")]
    .map((l) => ({leg: l, type: "LIMIT", price: l.entry_price}));
  const d = $("#dlg");
  $("#dlg-title").textContent = `Place ${rows.length} order(s)`;
  $("#dlg-live").classList.toggle("hidden", MODE !== "LIVE");
  $("#dlg-ok").classList.remove("hidden");
  const draw = () => {
    const sellFirst = rows.findIndex((r) => r.leg.side === "SELL") < rows.map((r) => r.leg.side).lastIndexOf("BUY") &&
      rows.some((r) => r.leg.side === "SELL");
    $("#dlg-body").innerHTML = `
      <div class="order-all">All legs: <button type="button" data-all="LIMIT">Limit</button>
        <button type="button" data-all="MARKET">Market</button></div>
      <table class="order-confirm"><tr><th></th><th>#</th><th>Side</th><th>Contract</th><th>Lots</th><th>Type</th><th>Price</th><th>Order</th></tr>
      ${rows.map((r, i) => `<tr data-row="${i}">
        <td class="drag-handle" data-drag="${i}" title="Drag to re-arrange" aria-label="Drag to re-arrange">⠿</td>
        <td>${i + 1}</td><td class="${r.leg.side}">${r.leg.side} ${r.leg.option_type}</td>
        <td>${esc(r.leg.underlying)} ${r.leg.strike}</td><td>${r.leg.lots}</td>
        <td><select data-type="${i}"><option value="LIMIT"${r.type === "LIMIT" ? " selected" : ""}>Limit</option>
          <option value="MARKET"${r.type === "MARKET" ? " selected" : ""}>Market</option></select></td>
        <td>${r.type === "LIMIT" ? `<input type="number" step="0.05" min="0.05" data-price="${i}" value="${r.price ?? ""}" required>`
          : `<span class="hint small">at market</span>`}</td>
        <td><button type="button" data-up="${i}" ${i === 0 ? "disabled" : ""} aria-label="Move up">↑</button>
          <button type="button" data-down="${i}" ${i === rows.length - 1 ? "disabled" : ""} aria-label="Move down">↓</button></td>
      </tr>`).join("")}</table>
      <p class="hint small">Orders are sent top to bottom: drag ⠿ (or use ↑ ↓) to re-arrange. Market = marketable limit at the live price (fills at once, protected
        from bad fills). If any leg fails its checks, nothing is sent; if a BUY (hedge) fails, the SELL legs are not sent.</p>
      ${sellFirst ? `<p class="strategy-warning">⚠ A SELL leg is placed before a BUY leg: it goes in unhedged for a moment and
        needs more margin. Move BUY legs up unless you mean it.</p>` : ""}`;
  };
  return new Promise((resolve) => {
    const body = $("#dlg-body");
    body.onclick = (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.all) rows.forEach((r) => { r.type = b.dataset.all; });
      const i = Number(b.dataset.up ?? b.dataset.down);
      if (b.dataset.up !== undefined) [rows[i - 1], rows[i]] = [rows[i], rows[i - 1]];
      if (b.dataset.down !== undefined) [rows[i + 1], rows[i]] = [rows[i], rows[i + 1]];
      draw();
    };
    body.onchange = (e) => {
      const t = e.target;
      if (t.dataset.type !== undefined) { rows[Number(t.dataset.type)].type = t.value; draw(); }
      if (t.dataset.price !== undefined) rows[Number(t.dataset.price)].price = t.value === "" ? null : Number(t.value);
    };
    body.oninput = body.onchange;
    ORDER_MOVE = (from, over, above) => {               // same drag + visual cues as the builder's legs
      const moved = rows[from];
      const rest = rows.filter((_, i) => i !== from);
      let at = rest.indexOf(rows[over]);
      if (!above) at += 1;
      rest.splice(at, 0, moved);
      rows = rest;
      draw();
    };
    d.onclose = () => {
      body.onclick = body.onchange = body.oninput = null;
      ORDER_MOVE = null;
      if (d.returnValue !== "ok") return resolve(null);
      const bad = rows.find((r) => r.type === "LIMIT" && !(r.price > 0));
      if (bad) { alertBox("Price needed", [`${bad.leg.side} ${bad.leg.option_type} ${bad.leg.strike}: enter a limit price or choose Market`]); return resolve(null); }
      resolve(rows.map((r) => ({...r.leg, price_type: r.type, entry_price: r.type === "LIMIT" ? r.price : r.leg.entry_price})));
    };
    d.returnValue = "";
    draw();
    d.showModal();
  });
}

$("#trade-all").onclick = async () => {
  const active = LEGS.filter((l) => l.checked);
  if (!active.length) return alertBox("Nothing to trade", ["select at least one leg"]);
  const ordered = await confirmOrders(active);
  if (!ordered) return;
  $("#trade-all").disabled = true;
  try {
    const res = await api("/api/strategies/trade_all", {config: collectConfig(), legs: ordered, keep_order: true});
    if (!res.ok) return alertBox("Not placed", res.errors || ["unknown error"]);
    $("#form-hint").textContent = `Strategy #${res.strategy_id}: ${res.confirmed.length} leg(s) placed` +
      (res.failed.length ? `, ${res.failed.length} NOT placed - see the strategy card below` : "");
    // never let a missing leg go unnoticed: say which legs did not go in, and why
    if (res.failed.length) alertBox(`${res.failed.length} leg(s) NOT placed`, res.failed.map((f) => `#${f.trade_id}: ${f.error}`));
    refreshStrategies();
  } catch (e) { alertBox("Error", [e.message]); }
  finally { $("#trade-all").disabled = false; }
};
$("#save-draft").onclick = () => alertBox("Drafts", ["Saving drafts for later/recurring runs is planned for a later phase; Trade All places the strategy now."]);

// ---------------------------------------------------------------- active strategies
// Exiting less than the full open quantity of a leg goes through the partial-exit API (prepare+confirm);
// exiting all of it uses the normal full exit - same distinction exitLeg() below makes for a single leg.
async function exitQty(t, qty) {
  if (qty >= t.open_qty) {
    const p = await api(`/api/trades/${t.id}/prepare`, {action: "EXIT"});
    return api(`/api/trades/${t.id}/exit`, {token: p.token});
  }
  const p = await api(`/api/trades/${t.id}/partial/prepare`, {qty});
  return api(`/api/trades/${t.id}/partial/confirm`, {token: p.token});
}

async function exitStrategy(id) {
  try {
    const s = (await api(`/api/strategies/${id}`)).strategy;
    const openLegs = s.legs.filter((t) => LEG_LIVE_STATUSES.has(t.status) && t.filled_qty > 0 && !t.pending_exit_reason);
    if (!openLegs.length) return alertBox("Nothing to exit", ["no open legs on this strategy"]);
    const rows = openLegs.map((t) => {
      const lots = Math.floor(t.open_qty / t.lot_size);
      return `<tr><td>${esc(t.side)} ${esc(t.tradingsymbol)}</td><td>${lots} lot(s) open</td>
        <td><input type="number" min="1" max="${lots}" step="1" value="${lots}" data-tid="${t.id}" class="exit-qty-input"></td></tr>`;
    }).join("");
    const ok = await dialog("Exit this strategy?",
      `<p class="hint small">Lots to exit per leg - defaults to the full open amount, edit any row to exit less.</p>
       <table><tr><th>Leg</th><th>Open</th><th>Lots to exit</th></tr>${rows}</table>`, MODE === "LIVE");
    if (!ok) return;
    for (const el of $$(".exit-qty-input")) {
      const leg = openLegs.find((t) => t.id === Number(el.dataset.tid));
      const lots = Math.max(0, Math.min(Math.floor(leg.open_qty / leg.lot_size), Math.floor(Number(el.value)) || 0));
      if (lots > 0) await exitQty(leg, lots * leg.lot_size);
    }
    refreshStrategies();
  } catch (e) { alertBox("Error", [e.message]); }
}

const LEG_LIVE_STATUSES = new Set(["ENTRY_ORDER_PLACED", "ENTRY_PENDING", "ENTRY_EXECUTED", "POSITION_ACTIVE"]);

function strategyCard(s) {
  const cfg = s.config;
  const legRows = s.legs.map((t) => {
    const canExit = LEG_LIVE_STATUSES.has(t.status) && t.filled_qty > 0 && !t.pending_exit_reason;
    const canEdit = LEG_LIVE_STATUSES.has(t.status) && !t.pending_exit_reason;
    // Not yet executed at all (still resting, nothing filled): offer Cancel instead of Exit - there's no
    // position to exit, just an order to pull before it fills.
    const canCancel = ["ENTRY_ORDER_PLACED", "ENTRY_PENDING"].includes(t.status) && t.filled_qty === 0;
    const qtyText = t.open_qty !== t.quantity ? `${t.quantity} <small>(open ${t.open_qty})</small>` : t.quantity;
    return `<tr><td>${t.side}</td><td>${esc(t.tradingsymbol)}</td><td>${qtyText}</td>
    <td>${num(t.entry_avg_price ?? t.entry_price)}</td>
    <td data-ltp-cell="${t.id}" data-trade-ltp="${t.id}">${num(t.kite_ltp ?? t.last_ltp)}</td>
    <td>${num(t.current_sl)}</td><td>${num(t.target)}</td>
    <td class="${(t.pnl || 0) >= 0 ? "pos" : "neg"}" data-trade-pnl="${t.id}">${money(t.pnl)}</td>
    <td>${esc(t.status)}${t.pending_exit_reason ? " → " + esc(t.pending_exit_reason) : ""}</td>
    <td class="leg-actions">${canEdit ? `<button type="button" class="edit-btn" data-leg-edit="${t.id}">Edit</button>` : ""}
      ${canCancel ? `<button type="button" class="leg-del" data-leg-cancel="${t.id}" title="Cancel this unfilled leg">✕</button>` : ""}
      ${canExit ? `<button type="button" class="danger" data-leg-exit="${t.id}">Exit</button>` : ""}</td></tr>`;
  }).join("");
  const cls = s.combined_pnl >= 0 ? "pos" : "neg";
  const failedLegs = s.failed_legs || [];
  const failedHtml = failedLegs.length ? `<div class="strategy-warning" role="alert">⚠ ${failedLegs.length} leg(s) not placed:
    <ul>${failedLegs.map((f) => `<li>${esc(f.side)} ${esc(f.tradingsymbol)}: ${esc(f.error)}</li>`).join("")}</ul></div>` : "";
  return `<div class="strategy-card${failedLegs.length ? " has-failed-legs" : ""}">
    <div class="head">
      <span class="name">#${s.id} ${esc(s.name)}</span>
      <span class="status-pill ${esc(s.status)}">${esc(s.status)}</span>
      <span>order type ${esc(cfg.order_type)}</span>
      <span class="pnl ${cls}" data-strategy-pnl="${s.id}">${money(s.combined_pnl)}</span>
      ${s.status === "ACTIVE" && s.open_legs > 0 ? `<button type="button" class="danger" data-exit="${s.id}">Exit strategy</button>` : ""}
    </div>
    ${failedHtml}
    <div class="leg-table-scroll">
    <table><tr><th>Side</th><th>Symbol</th><th>Qty</th><th>Entry</th>
      <th>LTP <button type="button" class="ltp-refresh" data-strategy-ltp="${s.id}" title="Refresh every leg's LTP now">↻</button></th>
      <th>SL</th><th>TP</th><th>P&amp;L</th><th>Status</th><th></th></tr>${legRows}</table>
    </div>
  </div>`;
}

// Points/percent/price <-> absolute price, for one side (kind: "sl" or "tp") - the same convention the
// leg-creation table and legSlTpPrices() use: BUY target above entry & stop below, SELL the reverse.
function slTpValueToPrice(entry, side, kind, value, typ) {
  if (value == null || value === "" || entry == null) return null;
  if (typ === "PRICE") return Number(value);
  const buy = side === "BUY";
  const up = kind === "tp" ? buy : !buy;
  const delta = typ === "PERCENT" ? (entry * Number(value)) / 100 : Number(value);
  return up ? entry + delta : entry - delta;
}
function priceToPoints(entry, side, kind, price) {
  if (price == null || entry == null) return "";
  const buy = side === "BUY";
  const up = kind === "tp" ? buy : !buy;
  return Math.round((up ? price - entry : entry - price) * 100) / 100;
}

async function editLeg(tid) {
  try {
    const t = (await api(`/api/trades/${tid}`)).trade;
    // Before the entry order has filled, entry price (and lots) are still changeable, same as the
    // single-trade page's edit form - only once it starts filling does the limit price stop making sense.
    const entryOpen = ["ENTRY_ORDER_PLACED", "ENTRY_PENDING"].includes(t.status);
    const f = (name, label, val, attrs = 'type="number" step="0.05"') =>
      `<label>${label} <input name="${name}" ${attrs} value="${val ?? ""}"></label>`;
    // Points/%/Price, same as when the leg was created, default Points - shown pre-converted from the
    // current absolute SL/target so the dialog opens already reflecting today's values.
    const ref = t.entry_avg_price ?? t.entry_price;
    const typeOpts = (kind, sel) => `
        <option value="POINTS" ${sel === "POINTS" ? "selected" : ""}>Points</option>
        <option value="PERCENT" ${sel === "PERCENT" ? "selected" : ""}>${kind === "sl" ? "SL%" : "TP%"}</option>
        <option value="PRICE" ${sel === "PRICE" ? "selected" : ""}>On price</option>`;
    const html = `<div id="leg-edit-form" class="edit-leg-grid">
      ${entryOpen ? f("entry_price", "Entry limit ₹", t.entry_price) + f("lots", `Lots (filled ${t.filled_qty})`, t.lots, 'type="number" step="1" min="1"') : ""}
      <label>Stop-loss <span class="slp-cell">
        <input id="edit-sl-value" type="number" step="0.05" value="${priceToPoints(ref, t.side, "sl", t.current_sl)}">
        <select id="edit-sl-type">${typeOpts("sl", "POINTS")}</select>
      </span></label>
      <label>Target (blank = none) <span class="slp-cell">
        <input id="edit-tp-value" type="number" step="0.05" value="${t.target != null ? priceToPoints(ref, t.side, "tp", t.target) : ""}">
        <select id="edit-tp-type">${typeOpts("tp", "POINTS")}</select>
      </span></label>
    </div>`;
    const go = await dialog(`Edit leg: ${t.side} ${t.tradingsymbol}`, html);
    if (!go) return;
    const changes = {};
    $$("#leg-edit-form input[name]").forEach((el) => { changes[el.name] = el.value; });
    const slVal = $("#edit-sl-value").value, tpVal = $("#edit-tp-value").value;
    const slPrice = slTpValueToPrice(ref, t.side, "sl", slVal, $("#edit-sl-type").value);
    const tpPrice = tpVal === "" ? null : slTpValueToPrice(ref, t.side, "tp", tpVal, $("#edit-tp-type").value);
    if (slPrice != null) changes.stop_loss = slPrice;
    changes.target = tpPrice ?? "";
    const p = await api(`/api/trades/${tid}/edit/prepare`, {changes});
    if (!p.ok) return alertBox("Edit not allowed", p.errors || []);
    if (!Object.keys(p.diff).length) return;   // nothing actually changed
    const rows = Object.entries(p.diff).map(([k, [a, b]]) => `<tr><td>${esc(k)}</td><td>${esc(a ?? "–")}</td><td>→ <b>${esc(b ?? "–")}</b></td></tr>`).join("");
    const ok = await dialog("Apply this change?", `<table>${rows}</table>`, MODE === "LIVE");
    if (!ok) return;
    await api(`/api/trades/${tid}/edit/apply`, {token: p.token});
    refreshStrategies();
  } catch (e) { alertBox("Edit failed", [e.message]); }
}

async function exitLeg(tid) {
  try {
    const t = (await api(`/api/trades/${tid}`)).trade;
    const lots = Math.floor(t.open_qty / t.lot_size);
    const html = `<div class="big-side ${esc(t.side)}">${esc(t.side)} ${esc(t.tradingsymbol)}</div>
      <label>Lots to exit (of ${lots} open) <input id="exit-lots" type="number" min="1" max="${lots}" step="1" value="${lots}"></label>`;
    const ok = await dialog("Exit this leg?", html, MODE === "LIVE");
    if (!ok) return;
    const chosen = Math.max(1, Math.min(lots, Math.floor(Number($("#exit-lots").value)) || lots));
    await exitQty(t, chosen * t.lot_size);
    refreshStrategies();
  } catch (e) { alertBox("Error", [e.message]); }
}

async function cancelLeg(tid) {
  try {
    const p = await api(`/api/trades/${tid}/prepare`, {action: "CANCEL"});
    const t = p.trade;
    const ok = await dialog("Cancel this leg?",
      `<div class="big-side ${esc(t.side)}">${esc(t.side)} ${esc(t.tradingsymbol)}</div>
       <p>Cancel the unfilled entry order - nothing has been executed on this leg yet.</p>`, MODE === "LIVE");
    if (!ok) return;
    await api(`/api/trades/${tid}/cancel`, {token: p.token});
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
    const html = sd.strategies.map(strategyCard).join("") || `<p class="hint small">No strategies yet.</p>`;
    const list = $("#strategies-list");
    if (list._html !== html) { list.innerHTML = html; list._html = html; }   // unchanged state: leave the DOM alone
    Live.reapply();
  } catch (e) { /* keep the last render */ }
}
// Manual LTP refresh: no click-count limit of our own (max_age=0 on every call) - Breeze's own daily
// budget is the only real ceiling, same as the "Get LTP" / "↻ Spot" buttons elsewhere in the app. One
// button on the column heading refreshes every leg in that strategy at once (no per-row button).
async function refreshLegLtp(tid) {
  try { await api(`/api/trades/${tid}/refresh_ltp`, {}); }
  catch (e) { alertBox("Error", [e.message]); }
}

$("#strategies-list").addEventListener("click", async (ev) => {
  const id = ev.target.dataset.exit;
  if (id) return exitStrategy(Number(id));
  const lid = ev.target.dataset.legExit;
  if (lid) return exitLeg(Number(lid));
  const cid = ev.target.dataset.legCancel;
  if (cid) return cancelLeg(Number(cid));
  const eid = ev.target.dataset.legEdit;
  if (eid) return editLeg(Number(eid));
  const sid = ev.target.dataset.strategyLtp;
  if (sid) {
    const btn = ev.target;
    btn.classList.add("spinning");
    try {
      const s = (await api(`/api/strategies/${sid}`)).strategy;
      const legIds = s.legs.filter((t) => LEG_LIVE_STATUSES.has(t.status)).map((t) => t.id);
      await Promise.all(legIds.map((tid) => refreshLegLtp(tid)));
      await refreshStrategies();
      // Flash each refreshed leg's LTP cell so a click that fetched an unchanged price still reads as
      // "it worked" (data-ltp-cell survives the innerHTML replace since refreshStrategies just re-rendered).
      legIds.forEach((tid) => {
        const cell = $(`[data-ltp-cell="${tid}"]`);
        if (cell) { cell.classList.remove("ltp-flash"); void cell.offsetWidth; cell.classList.add("ltp-flash"); }
      });
    } finally {
      btn.classList.remove("spinning");
    }
    return;
  }
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
}).catch((e) => { $("#form-hint").textContent = "Could not load instruments: " + e.message; RESTORING = false; })
  .finally(() => { $("#page-loading").classList.add("hidden"); });
// No polling: the server pushes "dashboard" when any trade/order state changes; LTP / P&L cells are patched
// in place by live.js on every tick. The slow refresh is only a safety net.
Live.onPrice((k, px) => { if (k === $("#base-spot").dataset.price) SPOT = px; });
Live.onDashboard(refreshStrategies);
Live.start();
refreshStrategies();
setInterval(refreshStrategies, 30000);
