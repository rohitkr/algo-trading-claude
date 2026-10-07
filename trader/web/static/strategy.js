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
let renderSettingsChip = () => {};                       // set up with the settings drawer (end of file)

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
  setTimeout(() => renderSettingsChip(), 0);              // the top-bar chip shows the restored order type
  if (c.start_time) $("#cfg-start-time").value = c.start_time;
  if (c.square_off_time) $("#cfg-square-off").value = c.square_off_time;
  if (Array.isArray(c.days)) $$(".day").forEach((b) => b.classList.toggle("on", c.days.includes(b.dataset.day)));
  // overall profit / loss exits are NOT carried over: a remembered ₹3,000 loss exit closed strategy #68 unnoticed
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

// "+0.98%" next to the spot: vs the previous close (from the server, once a day per instrument)
let PREV_CLOSE = null;
function renderSpotChange() {
  const el = $("#spot-chg");
  if (SPOT == null || !PREV_CLOSE) { el.textContent = ""; el.className = "spot-chg"; return; }
  const pct = (SPOT - PREV_CLOSE) / PREV_CLOSE * 100, pts = SPOT - PREV_CLOSE;
  el.textContent = `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`;
  el.title = `${pts >= 0 ? "+" : ""}${pts.toFixed(2)} vs previous close ${PREV_CLOSE}`;
  el.className = `spot-chg ${pct >= 0 ? "pos" : "neg"}`;
}
async function refreshSpot(force, keepDisplay) {
  if (!keepDisplay) { SPOT = null; PREV_CLOSE = null; $("#base-spot").textContent = "–"; renderSpotChange(); }   // switching instrument: old price is meaningless
  try {
    const s = await api(`/api/spot?ltp=1${force ? "&force=1" : ""}&underlying=${encodeURIComponent($("#base-underlying").value)}`);
    SPOT = s.spot; $("#base-spot").textContent = s.spot == null ? "no price" : money(s.spot);
    PREV_CLOSE = s.prev_close ?? null;
    renderSpotChange();
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

// The SL/TP mode (PRICE | POINTS | PERCENT) last chosen is remembered separately for SL and for target, and
// separately for the Edit leg dialog ("sl"/"tp") and the builder's new legs ("leg-sl"/"leg-tp", default Points).
const SLTP_MODES = ["PRICE", "POINTS", "PERCENT"];
function rememberedMode(kind, fallback = "PRICE") {
  try { const m = localStorage.getItem(`trader:sltp-mode:${kind}`); return SLTP_MODES.includes(m) ? m : fallback; }
  catch (e) { return fallback; }
}
function rememberMode(kind, mode) {
  if (!SLTP_MODES.includes(mode)) return;
  try { localStorage.setItem(`trader:sltp-mode:${kind}`, mode); } catch (e) { /* private window: ignore */ }
}

function newLeg(overrides) {
  const atm = atmStrike() || 0;
  // SL/TP are optional by default: the strategy's own combined profit/loss exit can cover a leg instead
  // (see the global config panel), so a blank per-leg value is a valid, common choice, not an oversight.
  // base_lots is the leg's OWN lot count at 1x - the multiplier dropdown scales every leg from this base
  // (Sensibull-style), so it stays correct no matter how many times the multiplier is changed.
  const baseLots = (overrides && overrides.base_lots) || 1;
  const leg = {id: NEXT_ID++, side: "SELL", underlying: $("#base-underlying").value, expiry: $("#base-expiry").value,
              strike: atm, option_type: "CE", base_lots: baseLots, lots: baseLots * currentMultiplier(), entry_price: null,
              sl_value: null, sl_type: rememberedMode("leg-sl", "POINTS"), tp_value: null, tp_type: rememberedMode("leg-tp", "POINTS"),
              leg_role: "", checked: true};
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
    <td class="price-cell"><input type="number" step="any" min="0" data-f="entry_price" value="${leg.entry_price ?? ""}" placeholder="LTP"></td>
    <td><div class="slp-cell">
      <input type="number" step="any" min="0" data-f="sl_value" value="${leg.sl_value ?? ""}" placeholder="optional"${wrongSide(leg, "sl")}>
      <select data-f="sl_type">
        <option value="POINTS" ${leg.sl_type === "POINTS" ? "selected" : ""}>Points</option>
        <option value="PERCENT" ${leg.sl_type === "PERCENT" ? "selected" : ""}>SL%</option>
        <option value="PRICE" ${leg.sl_type === "PRICE" ? "selected" : ""}>Price ₹</option>
      </select></div></td>
    <td><div class="slp-cell">
      <input type="number" step="any" min="0" data-f="tp_value" value="${leg.tp_value ?? ""}" placeholder="optional"${wrongSide(leg, "tp")}>
      <select data-f="tp_type">
        <option value="POINTS" ${leg.tp_type === "POINTS" ? "selected" : ""}>Points</option>
        <option value="PERCENT" ${leg.tp_type === "PERCENT" ? "selected" : ""}>TP%</option>
        <option value="PRICE" ${leg.tp_type === "PRICE" ? "selected" : ""}>Price ₹</option>
      </select></div></td>
    <td><button type="button" class="leg-del" data-act="del" title="Remove leg">✕</button></td>
  </tr>`;
}

// A stop-loss / target PRICE on the wrong side of the entry: red box + the reason on hover, before placing.
function wrongSide(leg, kind) {
  const v = Number(leg[`${kind}_value`]), e = Number(leg.entry_price);
  if (leg[`${kind}_type`] !== "PRICE" || !(v > 0) || !(e > 0)) return "";
  const below = leg.side === "BUY" ? kind === "sl" : kind === "tp";      // must this one be below the entry?
  if (below ? v < e : v > e) return "";
  const what = kind === "sl" ? "Stop-loss" : "Target";
  return ` class="bad" title="${leg.side}: ${what} must be ${below ? "below" : "above"} the entry ${e}"`;
}

// B <-> S for a leg (builder row or the order pop-up). A stop-loss / target typed as a PRICE is side-specific
// (BUY: SL below entry; SELL: above): flip it to the other side of the entry, same distance, so B -> S never
// leaves a stop on the wrong side.
function flipSide(leg) {
  leg.side = leg.side === "BUY" ? "SELL" : "BUY";
  const e = Number(leg.entry_price);
  for (const [v, t] of [["sl_value", "sl_type"], ["tp_value", "tp_type"]]) {
    if (leg[t] === "PRICE" && leg[v] != null && e > 0) {
      const flipped = Math.round((2 * e - Number(leg[v])) * 100) / 100;
      leg[v] = flipped > 0 ? flipped : null;
    }
  }
}

function renderLegs() {
  if (typeof markTightExits === "function") setTimeout(markTightExits, 0);
  // Re-rendering replaces every input, so keep the focused one focused: each ↑/↓ on a number box fires
  // "change" and re-renders, which used to drop focus after the first key press.
  const a = document.activeElement, row = a?.closest?.("#leg-tbody tr[data-id]");
  const keep = row && a.dataset.f ? {id: row.dataset.id, f: a.dataset.f} : null;
  $("#leg-tbody").innerHTML = LEGS.map(legRow).join("") ||
    `<tr><td colspan="11" class="hint small">No legs yet - add one or pick a template above.</td></tr>`;
  if (keep) $(`#leg-tbody tr[data-id="${keep.id}"] [data-f="${keep.f}"]`)?.focus();
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

// -- option pricing for the "today" curve (Black-Scholes on the spot, r = 0; display only) -------------------
function normCdf(x) {               // Abramowitz-Stegun 26.2.17, |error| < 7.5e-8
  const t = 1 / (1 + 0.2316419 * Math.abs(x)), d = 0.3989423 * Math.exp(-x * x / 2);
  const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))));
  return x > 0 ? 1 - p : p;
}
function bsPrice(S, K, T, vol, type) {
  if (T <= 0 || vol <= 0) return type === "CE" ? Math.max(S - K, 0) : Math.max(K - S, 0);
  const sd = vol * Math.sqrt(T), d1 = (Math.log(S / K) + sd * sd / 2) / sd, d2 = d1 - sd;
  return type === "CE" ? S * normCdf(d1) - K * normCdf(d2) : K * normCdf(-d2) - S * normCdf(-d1);
}
function impliedVol(price, S, K, T, type) {   // bisection; null when the price is below intrinsic / no time left
  if (!(price > 0) || !(S > 0) || T <= 0) return null;
  let lo = 0.005, hi = 5;
  if (bsPrice(S, K, T, lo, type) > price || bsPrice(S, K, T, hi, type) < price) return null;
  for (let i = 0; i < 60; i++) {
    const mid = (lo + hi) / 2;
    if (bsPrice(S, K, T, mid, type) > price) hi = mid; else lo = mid;
  }
  return (lo + hi) / 2;
}
// years to the leg's expiry (15:30 IST on the expiry date)
function yearsToExpiry(expiry) {
  const ms = new Date(`${expiry}T15:30:00+05:30`).getTime() - Date.now();
  return Math.max(0, ms) / (365 * 24 * 3600 * 1000);
}
const niceStep = (span, n) => {
  const raw = span / n, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * mag;
};
const compact = (v) => {           // 45,711 -> 45.7K, 312000 -> 3.12L (Indian), for axes and tiles
  const a = Math.abs(v), sg = v < 0 ? "-" : "";
  if (a >= 1e7) return `${sg}${+(a / 1e7).toFixed(2)}Cr`;
  if (a >= 1e5) return `${sg}${+(a / 1e5).toFixed(2)}L`;
  if (a >= 1e3) return `${sg}${+(a / 1e3).toFixed(1)}K`;
  return `${sg}${Math.round(a)}`;
};
let MARGIN = {key: "", margin: null, charges: null};

function renderPayoff() {
  const legs = LEGS.filter((l) => l.checked && l.strike && l.entry_price != null);
  const box = $("#payoff-chart"), stats = $("#payoff-stats");
  if (legs.length < LEGS.filter((l) => l.checked).length) {
    box.innerHTML = `<p class="hint small chart-wait">Waiting on a price for every leg…</p>`;
    stats.innerHTML = "";
    return;
  }
  if (!legs.length) { box.innerHTML = `<p class="hint small chart-wait">Add a leg to see its payoff.</p>`; stats.innerHTML = ""; return; }

  // implied vol per leg from its own price -> the "today" curve and the +-1/2 SD range
  const S0 = SPOT;
  legs.forEach((l) => { l._T = yearsToExpiry(l.expiry); l._iv = S0 ? impliedVol(l.entry_price, S0, l.strike, l._T, l.option_type) : null; });
  const withIv = legs.filter((l) => l._iv);
  const atmIv = withIv.length ? withIv.reduce((s, l) => s + l._iv, 0) / withIv.length : null;
  const Tmin = Math.min(...legs.map((l) => l._T));
  const sdPts = S0 && atmIv && Tmin > 0 ? S0 * atmIv * Math.sqrt(Tmin) : null;
  const today = sdPts && withIv.length === legs.length;   // every leg priced by the model: draw the blue line
  const todayPnl = (x) => legs.reduce((s, l) =>
    s + (l.side === "BUY" ? 1 : -1) * (bsPrice(x, l.strike, l._T, l._iv, l.option_type) - l.entry_price) * l.lots * (l.lot_size || 1), 0);

  const strikes = legs.map((l) => l.strike);
  const spread = Math.max(...strikes) - Math.min(...strikes);
  const pad = Math.max(BASE_STEP * 6, spread * 0.6, 1);
  let lo = Math.min(...strikes) - pad, hi = Math.max(...strikes) + pad;
  if (sdPts) { lo = Math.min(lo, S0 - 2.3 * sdPts); hi = Math.max(hi, S0 + 2.3 * sdPts); }
  const xs = new Set([lo, hi]);
  for (let i = 0; i <= 160; i++) xs.add(lo + ((hi - lo) * i) / 160);
  strikes.forEach((k) => { xs.add(k - 0.01); xs.add(k); xs.add(k + 0.01); });   // land exactly on the kinks
  const points = Array.from(xs).sort((a, b) => a - b).map((x) => [x, combinedPayoff(x, legs)]);
  const tPoints = today ? points.filter((_, i) => i % 2 === 0).map(([x]) => [x, todayPnl(x)]) : [];

  const ys = [...points.map((p) => p[1]), ...tPoints.map((p) => p[1])];
  let maxY = Math.max(0, ...ys), minY = Math.min(0, ...ys);
  if (maxY === minY) { maxY += 1; minY -= 1; }
  const yPad = (maxY - minY) * 0.08; maxY += yPad; minY -= yPad;
  const breakevens = [];
  for (let i = 1; i < points.length; i++) {
    const [x0, y0] = points[i - 1], [x1, y1] = points[i];
    if ((y0 < 0 && y1 >= 0) || (y0 > 0 && y1 <= 0)) breakevens.push(x0 + (x1 - x0) * (0 - y0) / (y1 - y0));
  }

  // drawn at the box's real pixel size (the rail is resizable; ResizeObserver below redraws), so text stays crisp
  const W = Math.max(280, Math.round(box.clientWidth - 8)), H = Math.max(160, Math.round(box.clientHeight - 8));
  const mL = 52, mR = 12, mT = 30, mB = 24;
  const pw = W - mL - mR, ph = H - mT - mB;
  const xScale = (x) => mL + ((x - lo) / (hi - lo)) * pw;
  const yScale = (y) => mT + ((maxY - y) / (maxY - minY)) * ph;
  const zeroY = yScale(0);
  const zeroFrac = ((maxY - 0) / (maxY - minY)) * 100;

  const path = `M ${xScale(points[0][0])},${zeroY} ` + points.map(([x, y]) => `L ${xScale(x)},${yScale(y)}`).join(" ") +
    ` L ${xScale(points[points.length - 1][0])},${zeroY} Z`;
  const linePath = `M ` + points.map(([x, y]) => `${xScale(x)},${yScale(y)}`).join(" L ");
  const todayPath = tPoints.length ? `M ` + tPoints.map(([x, y]) => `${xScale(x)},${yScale(y)}`).join(" L ") : "";

  // round-number grid: x every nice step (strikes), y every nice rupee step
  const xStep = niceStep(hi - lo, Math.max(3, Math.floor(pw / 90)));
  const xTicks = []; for (let v = Math.ceil(lo / xStep) * xStep; v <= hi; v += xStep) xTicks.push(v);
  const yStep = niceStep(maxY - minY, Math.max(3, Math.floor(ph / 45)));
  const yTicks = []; for (let v = Math.ceil(minY / yStep) * yStep; v <= maxY; v += yStep) yTicks.push(Math.round(v));
  const grid = xTicks.map((v) => `<line x1="${xScale(v)}" y1="${mT}" x2="${xScale(v)}" y2="${mT + ph}" class="grid-line"/>
      <text x="${xScale(v)}" y="${H - 6}" class="axis-label" text-anchor="${xScale(v) > W - 28 ? "end" : "middle"}">${v.toLocaleString("en-IN")}</text>`).join("") +
    yTicks.map((v) => `<line x1="${mL}" y1="${yScale(v)}" x2="${mL + pw}" y2="${yScale(v)}" class="grid-line"/>
      <text x="${mL - 6}" y="${yScale(v) + 4}" class="axis-label" text-anchor="end">${compact(v)}</text>`).join("");
  const sdMarks = sdPts ? [-2, -1, 1, 2].map((k) => [k, S0 + k * sdPts]).filter(([, x]) => x > lo && x < hi).map(([k, x]) =>
    `<line x1="${xScale(x)}" y1="${mT}" x2="${xScale(x)}" y2="${mT + ph}" class="sd-line"/>
     <text x="${xScale(x)}" y="${mT - 4}" class="axis-label sd-label" text-anchor="middle">${k > 0 ? "+" : ""}${k}SD</text>`).join("") : "";
  const beMarks = breakevens.map((be) => `<circle cx="${xScale(be)}" cy="${zeroY}" r="3.5" class="be-dot"/>`).join("");
  const spotLine = S0 != null && S0 >= lo && S0 <= hi
    ? `<line x1="${xScale(S0)}" y1="${mT}" x2="${xScale(S0)}" y2="${mT + ph}" class="spot-line"/>` : "";

  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="payoff-svg" role="img" aria-label="Payoff chart">
    <defs>
      <linearGradient id="pnlGrad" gradientUnits="userSpaceOnUse" x1="0" y1="${mT}" x2="0" y2="${mT + ph}">
        <stop offset="0%" stop-color="var(--buy)" stop-opacity=".22"/>
        <stop offset="${zeroFrac}%" stop-color="var(--buy)" stop-opacity=".06"/>
        <stop offset="${zeroFrac}%" stop-color="var(--sell)" stop-opacity=".06"/>
        <stop offset="100%" stop-color="var(--sell)" stop-opacity=".22"/>
      </linearGradient>
      <linearGradient id="expGrad" gradientUnits="userSpaceOnUse" x1="0" y1="${mT}" x2="0" y2="${mT + ph}">
        <stop offset="${zeroFrac}%" stop-color="var(--buy)"/><stop offset="${zeroFrac}%" stop-color="var(--sell)"/>
      </linearGradient>
    </defs>
    ${grid}${sdMarks}
    <line x1="${mL}" y1="${zeroY}" x2="${mL + pw}" y2="${zeroY}" class="zero-line"/>
    <path d="${path}" fill="url(#pnlGrad)" stroke="none"/>
    <path d="${linePath}" fill="none" stroke="url(#expGrad)" class="expiry-line"/>
    ${todayPath ? `<path d="${todayPath}" fill="none" class="today-line"/>` : ""}
    ${spotLine}${beMarks}
    <line id="pf-cross" x1="0" y1="${mT}" x2="0" y2="${mT + ph}" class="cross-line" visibility="hidden"/>
    <circle id="pf-dot" r="4.5" class="cross-dot" visibility="hidden"/>
    <circle id="pf-dot2" r="4" class="cross-dot today" visibility="hidden"/>
  </svg>
  <div class="chart-legend"><span class="lg exp"></span>On expiry${todayPath ? `<span class="lg today"></span>Today` : ""}</div>
  ${S0 != null ? `<div class="chart-spot">Spot ${S0.toLocaleString("en-IN")}</div>` : ""}
  <div id="pf-tip" class="pf-tip" hidden></div>`;

  // hover: "when the price is at X (+y% from spot)" -> P&L today and at expiry, like Sensibull's tooltip
  const svg = box.querySelector("svg"), tip = $("#pf-tip"), cross = $("#pf-cross"), dot = $("#pf-dot"), dot2 = $("#pf-dot2");
  const hide = () => { tip.hidden = true; [cross, dot, dot2].forEach((e) => e.setAttribute("visibility", "hidden")); };
  svg.addEventListener("pointerleave", hide);
  svg.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) * (W / r.width);
    if (px < mL || px > mL + pw) return hide();
    const x = lo + ((px - mL) / pw) * (hi - lo), y = combinedPayoff(x, legs), cx = xScale(x);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", cx); dot.setAttribute("cy", yScale(y)); dot.setAttribute("visibility", "visible");
    dot.setAttribute("class", `cross-dot ${y >= 0 ? "pos" : "neg"}`);
    const yt = today ? todayPnl(x) : null;
    if (yt != null) { dot2.setAttribute("cx", cx); dot2.setAttribute("cy", yScale(yt)); dot2.setAttribute("visibility", "visible"); }
    const pct = S0 ? ` <span class="${x >= S0 ? "pos" : "neg"}">${x >= S0 ? "+" : ""}${((x - S0) / S0 * 100).toFixed(1)}% (${x >= S0 ? "+" : ""}${Math.round(x - S0)})</span>` : "";
    tip.innerHTML = `<div class="pf-tip-h">When price is at</div><div class="pf-tip-x">${Math.round(x).toLocaleString("en-IN")}${pct}</div>
      ${yt != null ? `<div class="pf-tip-row">Today <b class="${yt >= 0 ? "pos" : "neg"}">${money(Math.round(yt))}</b></div>` : ""}
      <div class="pf-tip-row">On expiry <b class="${y >= 0 ? "pos" : "neg"}">${money(Math.round(y))}</b></div>`;
    tip.hidden = false;
    const bx = box.getBoundingClientRect(), mx = ev.clientX - bx.left, my = ev.clientY - bx.top;
    tip.style.left = `${mx + 14 + tip.offsetWidth > bx.width ? mx - 14 - tip.offsetWidth : mx + 14}px`;
    tip.style.top = `${Math.max(4, Math.min(bx.height - tip.offsetHeight - 4, my - tip.offsetHeight / 2))}px`;
  });

  // -- headline numbers ---------------------------------------------------------------------------------
  const slopes = tailSlopes(legs);
  const expY = points.map((p) => p[1]);
  const maxP = Math.max(...expY), maxL = Math.min(...expY);
  const unlimitedP = slopes.right > 0, unlimitedL = slopes.right < 0 || slopes.left < 0;
  const ofMargin = (v) => (MARGIN.margin ? ` <small>(${v >= 0 ? "+" : ""}${Math.round(v / MARGIN.margin * 100)}%)</small>` : "");
  const beText = breakevens.length ? breakevens.map((b) =>
    `${Math.round(b)}${S0 ? ` <small class="${b >= S0 ? "pos" : "neg"}">(${b >= S0 ? "+" : ""}${((b - S0) / S0 * 100).toFixed(1)}%)</small>` : ""}`).join(", ") : "none in range";
  const rr = !unlimitedP && !unlimitedL && maxL < 0 ? (maxP / -maxL).toFixed(2) : "–";
  stats.innerHTML = `<div>Max profit <output class="pos">${unlimitedP ? "Unlimited" : `+${money(Math.round(maxP))}${ofMargin(maxP)}`}</output></div>
    <div>Max loss <output class="neg">${unlimitedL ? "Unlimited" : `${money(Math.round(maxL))}${ofMargin(maxL)}`}</output></div>
    <div class="wide">Breakeven <output>${beText}</output></div>
    <div title="Zerodha margin for these legs together (hedge benefit included)">Margin needed <output>${MARGIN.margin ? `₹${compact(MARGIN.margin)}` : "–"}</output></div>
    <div title="Max profit ÷ max loss at expiry">Reward / risk <output>${rr}</output></div>`;

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
    "";                                                   // no per-leg SL/TP: nothing to show, take no space
  fetchMargin(legs);
}

// Zerodha margin for the checked legs (hedge benefit included): refetched only when the structure changes
// (strikes / sides / lots / expiry / order type), not on every price tick.
let marginTimer = 0;
function fetchMargin(legs) {
  const cfg = collectConfig();
  const key = JSON.stringify([cfg.order_type, legs.map((l) => [l.underlying, l.expiry, l.strike, l.option_type, l.side, l.lots])]);
  if (key === MARGIN.key) return;
  MARGIN = {key, margin: null, charges: null};
  clearTimeout(marginTimer);
  marginTimer = setTimeout(async () => {
    try {
      const r = await api("/api/strategies/margin", {config: cfg, legs: legs.map((l) => ({underlying: l.underlying,
        expiry: l.expiry, strike: l.strike, option_type: l.option_type, side: l.side, lots: l.lots, entry_price: l.entry_price}))});
      if (MARGIN.key !== key) return;                     // the legs changed meanwhile
      MARGIN = {key, margin: r.margin, charges: r.charges};
      renderPayoff(); renderCalc();
    } catch (e) { /* no margin shown */ }
  }, 500);
}

function renderCalc() {
  const priced = LEGS.filter((l) => l.checked && l.entry_price != null);
  if (!priced.length) {
    $("#calc-price-get").textContent = "–"; $("#calc-premium-get").textContent = "–"; $("#calc-charges").textContent = "–";
    return;
  }
  // like Sensibull: price get = net credit (+) / debit (-) per unit of the structure, premium get = in rupees
  const minLots = Math.min(...priced.map((l) => l.lots || 1));
  const sign = (l) => (l.side === "SELL" ? 1 : -1);
  const perUnit = priced.reduce((s, l) => s + sign(l) * l.entry_price * (l.lots / minLots), 0);
  const premium = priced.reduce((s, l) => s + sign(l) * l.entry_price * l.lots * (l.lot_size || 1), 0);
  const lotSum = priced.reduce((s, l) => s + l.lots, 0);
  $("#calc-price-get").textContent = perUnit.toFixed(2);
  $("#calc-price-get").className = perUnit >= 0 ? "pos" : "neg";
  $("#calc-premium-get").textContent = money(Math.round(premium));
  $("#calc-premium-get").className = premium >= 0 ? "pos" : "neg";
  $("#calc-charges").textContent = MARGIN.charges != null ? money(Math.round(MARGIN.charges)) : `~${money(lotSum * 40)}`;
  $("#calc-charges").title = MARGIN.charges != null ? "Zerodha's estimate for these orders" : "rough estimate (₹40 per lot)";
}

$("#leg-tbody").addEventListener("click", (ev) => {
  const row = ev.target.closest("tr[data-id]");
  if (!row) return;
  const id = Number(row.dataset.id), leg = LEGS.find((l) => l.id === id);
  const act = ev.target.dataset.act;
  if (!leg || !act) return;
  if (act === "del") return removeLeg(id);
  if (act === "side") { flipSide(leg); return renderLegs(); }
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
  // the builder remembers its own SL/TP type, separate from the Edit pop-up (sharing it made new legs
  // silently start in "Price ₹" after a price-mode edit, so "20" meant ₹20, not 20 points)
  if (f === "sl_type") rememberMode("leg-sl", ev.target.value);
  if (f === "tp_type") rememberMode("leg-tp", ev.target.value);
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
  } else if (kind === "long_straddle") {                 // volatility: profits on a big move either way
    add({side: "BUY", option_type: "CE", strike: atm});
    add({side: "BUY", option_type: "PE", strike: atm});
  } else if (kind === "long_strangle") {
    add({side: "BUY", option_type: "CE", strike: atm + 2 * step});
    add({side: "BUY", option_type: "PE", strike: atm - 2 * step});
  } else if (kind === "long_iron_condor") {              // reverse iron condor: debit, profits outside the body
    add({side: "SELL", option_type: "PE", strike: atm - 6 * step});
    add({side: "BUY", option_type: "PE", strike: atm - 2 * step});
    add({side: "BUY", option_type: "CE", strike: atm + 2 * step});
    add({side: "SELL", option_type: "CE", strike: atm + 6 * step});
  } else if (kind === "long_iron_fly") {                 // reverse iron fly
    add({side: "SELL", option_type: "PE", strike: atm - 4 * step});
    add({side: "BUY", option_type: "PE", strike: atm});
    add({side: "BUY", option_type: "CE", strike: atm});
    add({side: "SELL", option_type: "CE", strike: atm + 4 * step});
  } else if (kind === "bull_call_spread") {              // bullish, debit
    add({side: "BUY", option_type: "CE", strike: atm});
    add({side: "SELL", option_type: "CE", strike: atm + 4 * step});
  } else if (kind === "bull_put_spread") {               // bullish, credit
    add({side: "BUY", option_type: "PE", strike: atm - 4 * step});
    add({side: "SELL", option_type: "PE", strike: atm});
  } else if (kind === "bear_put_spread") {               // bearish, debit
    add({side: "BUY", option_type: "PE", strike: atm});
    add({side: "SELL", option_type: "PE", strike: atm - 4 * step});
  } else if (kind === "bear_call_spread") {              // bearish, credit
    add({side: "BUY", option_type: "CE", strike: atm + 4 * step});
    add({side: "SELL", option_type: "CE", strike: atm});
  }
  renderLegs();
  LEGS.forEach(fetchLegPrice);
}
$("#tmpl-select").addEventListener("change", (e) => {
  const v = e.target.value;
  e.target.value = "";                                   // a picker, not a setting: back to "Templates…"
  if (v) applyTemplate(v);
});

// ---------------------------------------------------------------- config collection
function collectConfig() {
  const mode = $("input[name=trailing_mode]:checked").value;
  const cfg = {
    order_type: $("input[name=order_type]:checked").value,
    start_time: $("#cfg-start-time").value || null,
    square_off_time: $("#cfg-square-off").value || null,
    days: $$(".day.on").map((b) => b.dataset.day),
    exit_profit_amount: $("#cfg-exit-profit").value || null,
    exit_loss_amount: $("#cfg-exit-loss").value ? Math.abs(Number($("#cfg-exit-loss").value)) : null,   // "-2000" = 2000
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
  okButton("Confirm");
  return new Promise((resolve) => { d.onclose = () => resolve(d.returnValue === "ok"); d.returnValue = ""; d.showModal(); });
}
// The dialog's OK button: "Buy" / "Sell" (coloured by side) for an order, "Confirm" otherwise.
function okButton(text, side) {
  const b = $("#dlg-ok");
  b.classList.remove("hidden", "ok-buy", "ok-sell");
  b.textContent = text;
  if (side) b.classList.add(side === "BUY" ? "ok-buy" : "ok-sell");
}
// ↻ next to a price box: fills it with the contract's live price (a fresh fetch, not the cached one).
const ltpBtn = (target) => `<button type="button" class="ltp-fill" data-ltp-for="${target}" title="Fill the live price (LTP)">↻</button>`;
function wireLtpFill(t) {
  $$("#dlg-body .ltp-fill").forEach((b) => {
    b.onclick = async (ev) => {
      ev.preventDefault();
      b.disabled = true;
      try {
        const c = await api(`/api/contract?ltp=1&force=1&underlying=${encodeURIComponent(t.underlying)}&expiry=${t.expiry}&strike=${t.strike}&option_type=${t.option_type}`);
        if (c.ltp == null) throw new Error(c.price_error || "no live price");
        const box = $(b.dataset.ltpFor);
        box.value = toTick(Number(c.ltp), Number(t.tick_size) || 0.05);
        box.dispatchEvent(new Event("input", {bubbles: true}));
        box.focus();
      } catch (e) { b.title = `No price: ${e.message}`; }
      finally { b.disabled = false; }
    };
  });
}
function alertBox(title, lines) { dialog(title, `<ul>${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`); $("#dlg-ok").classList.add("hidden"); }

// ---------------------------------------------------------------- trade all
// Order confirmation: the EXECUTION order (default: all BUY legs, then SELL legs - hedges first) and Market/Limit
// per leg. Re-arranging here never changes the leg order in the builder table above.
function confirmOrders(active) {
  let rows = [...active.filter((l) => l.side === "BUY"), ...active.filter((l) => l.side !== "BUY")]
    .map((l) => ({leg: l, type: "LIMIT", price: l.entry_price}));
  const d = $("#dlg");
  // compact: a small LIVE tag in the title instead of a line of text (the page banner says LIVE too)
  $("#dlg-title").innerHTML = `Place ${rows.length} order${rows.length > 1 ? "s" : ""}${MODE === "LIVE" ? '<span class="live-tag">LIVE</span>' : ""}`;
  $("#dlg-live").classList.add("hidden");
  const sides = new Set(rows.map((r) => r.leg.side));
  if (sides.size === 1) { const sd = [...sides][0]; okButton(sd === "BUY" ? "Buy" : "Sell", sd); }
  else okButton("Place orders");
  const draw = () => {
    const all = rows.every((r) => r.type === "LIMIT") ? "LIMIT" : rows.every((r) => r.type === "MARKET") ? "MARKET" : "";
    $("#dlg-body").innerHTML = `
      <div class="order-all">All legs: <button type="button" data-all="LIMIT" class="${all === "LIMIT" ? "on" : ""}">Limit</button>
        <button type="button" data-all="MARKET" class="${all === "MARKET" ? "on" : ""}">Market</button></div>
      <table class="order-confirm"><tr><th></th><th>#</th><th>Side</th><th>Contract</th><th>Lots</th><th>Type</th>
        <th>Price <button type="button" class="spin-btn" data-reset-ltp title="Reset LTP for every leg">↻</button></th><th>Order</th></tr>
      ${rows.map((r, i) => `<tr data-row="${i}">
        <td class="drag-handle" data-drag="${i}" title="Drag to re-arrange" aria-label="Drag to re-arrange">⠿</td>
        <td>${i + 1}</td>
        <td><button type="button" class="bs-btn ${r.leg.side === "BUY" ? "buy" : "sell"}" data-flip="${i}"
          title="Switch BUY / SELL">${r.leg.side === "BUY" ? "B" : "S"}</button>
          <span class="bs-btn ${r.leg.option_type === "CE" ? "ce" : "pe"} tag">${r.leg.option_type}</span></td>
        <td>${esc(r.leg.underlying)} ${r.leg.strike}</td><td>${r.leg.lots}</td>
        <td><select data-type="${i}"><option value="LIMIT"${r.type === "LIMIT" ? " selected" : ""}>Limit</option>
          <option value="MARKET"${r.type === "MARKET" ? " selected" : ""}>Market</option></select></td>
        <td>${r.type === "LIMIT" ? `<input type="number" step="any" min="0" data-price="${i}" value="${r.price ?? ""}" required>`
          : `<input type="text" value="" placeholder="at market" disabled aria-label="Market price">`}</td>
        <td><button type="button" data-up="${i}" ${i === 0 ? "disabled" : ""} aria-label="Move up">↑</button>
          <button type="button" data-down="${i}" ${i === rows.length - 1 ? "disabled" : ""} aria-label="Move down">↓</button></td>
      </tr>`).join("")}</table>
`;
  };
  return new Promise((resolve) => {
    const body = $("#dlg-body");
    body.onclick = (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.all) rows.forEach((r) => { r.type = b.dataset.all; });
      if (b.dataset.flip !== undefined) {               // B <-> S, the same as the builder row (kept in sync)
        flipSide(rows[Number(b.dataset.flip)].leg);
        const sides = new Set(rows.map((r) => r.leg.side));
        if (sides.size === 1) { const sd = [...sides][0]; okButton(sd === "BUY" ? "Buy" : "Sell", sd); }
        else okButton("Place orders");
        renderLegs();
      }
      if (b.dataset.resetLtp !== undefined) {           // every Limit price <- the contract's live price
        b.classList.add("spinning");
        Promise.all(rows.map(async (r) => {
          try {
            const c = await api(`/api/contract?ltp=1&force=1&underlying=${encodeURIComponent(r.leg.underlying)}&expiry=${r.leg.expiry}&strike=${r.leg.strike}&option_type=${r.leg.option_type}`);
            if (c.ltp != null) r.price = toTick(Number(c.ltp), Number(c.tick_size) || 0.05);
          } catch (err) { /* keep the price shown */ }
        })).then(draw);
        return;
      }
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

// A combined max loss / max profit worth only a point or two of movement trips on the bid/ask spread the moment the
// order fills (2026-10-07: max loss ₹200 on 325 qty = 0.6 pts squared a fresh SELL off within 4 s).
const TIGHT_PTS = 2;
function tightExits(cfg, qty) {
  if (!qty) return [];
  return [["Max loss", cfg.exit_loss_amount], ["Max profit", cfg.exit_profit_amount]]
    .filter(([, v]) => Number(v) > 0 && Number(v) / qty < TIGHT_PTS)
    .map(([n, v]) => `${n} ₹${Number(v).toLocaleString("en-IN")} is only ${(Number(v) / qty).toFixed(1)} points on ${qty} quantity: ` +
      "normal bid/ask movement can trigger it right after the order fills.");
}
const builderQty = () => LEGS.filter((l) => l.checked).reduce((s, l) => s + l.lots * (l.lot_size || 1), 0);
function markTightExits() {                                 // red box + reason on hover; no text, so nothing shifts
  const qty = builderQty();
  for (const [id, key] of [["#cfg-exit-loss", "exit_loss_amount"], ["#cfg-exit-profit", "exit_profit_amount"]]) {
    const w = tightExits({[key]: Math.abs(Number($(id).value)) || null}, qty);
    $(id).classList.toggle("bad", w.length > 0);
    $(id).title = w[0] || "";
  }
}
["#cfg-exit-loss", "#cfg-exit-profit"].forEach((id) => $(id).addEventListener("input", markTightExits));
async function confirmTight(warnings) {
  if (!warnings.length) return true;
  const pending = dialog("Check the combined exit", `<ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
    <p class="hint small">Use a bigger amount, or leave it blank (off) and rely on the legs' own stop-loss.</p>`);
  okButton("Place anyway");
  return pending;
}

$("#trade-all").onclick = async () => {
  const active = LEGS.filter((l) => l.checked);
  if (!active.length) return alertBox("Nothing to trade", ["select at least one leg"]);
  if (!(await confirmTight(tightExits(collectConfig(), builderQty())))) return;
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

// "NIFTY 22300 PE · 13 Oct" instead of NIFTY26O1322300PE (the raw symbol stays in the tooltip)
function contractName(t) {
  const d = t.expiry ? new Date(`${t.expiry}T00:00:00`) : null;
  const day = d ? d.toLocaleDateString("en-IN", {day: "numeric", month: "short"}) : "";
  const k = Number(t.strike);
  return `${esc(t.underlying || "")} ${Number.isInteger(k) ? k : k.toFixed(1)} ${esc(t.option_type || "")}${day ? ` <small>· ${day}</small>` : ""}`;
}
// Status in words, with WHY a leg closed: SL hit, Target booked, Manual exit, ...
const EXIT_REASONS = {
  STOP_LOSS_HIT: "SL hit", TRAILING_SL_HIT: "Trailing SL hit", TARGET_HIT: "Target booked", USER_EXIT: "Exited by you",
  MANUAL_EXIT: "Closed in Kite", AUTO_EXIT: "Auto-exit time", SQUARE_OFF: "Square-off time", DAILY_LIMIT: "Daily loss limit",
  STRATEGY_LOSS_LIMIT: "Max loss hit", STRATEGY_PROFIT_LOCKED: "Locked profit (combined SL)", STRATEGY_PROFIT_TARGET: "Max profit booked", STRATEGY_TRAIL_STOP: "Trailing profit stop",
};
function legStatus(t) {
  if (t.pending_exit_reason) return {text: `Exiting · ${EXIT_REASONS[t.pending_exit_reason] || t.pending_exit_reason}`, cls: "warn"};
  switch (t.status) {
    case "POSITION_ACTIVE": case "ENTRY_EXECUTED": return {text: "Running", cls: "run"};
    case "ENTRY_ORDER_PLACED": case "ENTRY_PENDING": return {text: "Waiting for fill", cls: "wait"};
    case "EXIT_ORDER_PLACED": case "EXIT_PENDING": return {text: "Exiting", cls: "warn"};
    case "EXITED": case "MANUALLY_EXITED": return {text: EXIT_REASONS[t.exit_reason] || "Exited", cls: "done"};
    case "CANCELLED": return {text: "Cancelled", cls: "muted"};
    case "REJECTED": return {text: "Rejected", cls: "bad"};
    case "EXPIRED": return {text: "Not placed", cls: "muted"};
    case "UNKNOWN_REQUIRES_RECONCILIATION": return {text: "Check in Kite", cls: "bad"};
    case "ERROR": return {text: "Error", cls: "bad"};
    default: return {text: t.status, cls: ""};
  }
}

function strategyCard(s) {
  const cfg = s.config;
  const legRows = s.legs.map((t) => {
    const canExit = LEG_LIVE_STATUSES.has(t.status) && t.filled_qty > 0 && !t.pending_exit_reason;
    const canEdit = LEG_LIVE_STATUSES.has(t.status) && !t.pending_exit_reason;
    // Not yet executed at all (still resting, nothing filled): offer Cancel instead of Exit - there's no
    // position to exit, just an order to pull before it fills.
    const canCancel = ["ENTRY_ORDER_PLACED", "ENTRY_PENDING"].includes(t.status) && t.filled_qty === 0;
    // Add = more lots of this contract on a running leg; Re-buy / Re-sell = enter a closed leg again.
    // Both place a NEW leg in this strategy (its own SL/target).
    const canReenter = !LEG_LIVE_STATUSES.has(t.status) && t.filled_qty > 0;
    const qtyText = t.open_qty !== t.quantity ? `${t.quantity} <small>(open ${t.open_qty})</small>` : t.quantity;
    const st = legStatus(t);
    return `<tr><td><span class="bs-btn ${t.side === "BUY" ? "buy" : "sell"} chip">${t.side === "BUY" ? "B" : "S"}</span></td>
    <td class="sym" title="${esc(t.tradingsymbol)}">${contractName(t)}</td><td class="num">${qtyText}</td>
    <td class="num">${num(t.entry_avg_price ?? t.entry_price)}</td>
    <td class="num">${t.exit_avg_price != null ? num(t.exit_avg_price) : "–"}</td>
    <td class="num" data-ltp-cell="${t.id}" data-trade-ltp="${t.id}">${num(t.kite_ltp ?? t.last_ltp)}</td>
    <td class="num">${num(t.current_sl)}</td><td class="num">${num(t.target)}</td>
    <td class="num ${(t.pnl || 0) >= 0 ? "pos" : "neg"}" data-trade-pnl="${t.id}">${money(t.pnl)}</td>
    <td title="${esc(t.status)}${t.exit_reason ? " · " + esc(t.exit_reason) : ""}${t.pending_exit_reason ? " → " + esc(t.pending_exit_reason) : ""}"><span class="leg-st ${st.cls}">${st.text}</span></td>
    <td class="leg-actions">${canEdit ? `<button type="button" class="edit-btn" data-leg-edit="${t.id}">Edit</button>` : ""}
      ${canCancel ? `<button type="button" class="mkt-btn" data-leg-market="${t.id}" title="Fill now: move the limit to the live price">Market</button>` : ""}
      ${canCancel ? `<button type="button" class="leg-del" data-leg-cancel="${t.id}" title="Cancel this unfilled leg">✕</button>` : ""}
      ${canExit ? `<button type="button" class="add-btn" data-leg-add="${t.id}" title="Add lots to this leg">Add</button>` : ""}
      ${canExit ? `<button type="button" class="danger" data-leg-exit="${t.id}">Exit</button>` : ""}
      ${canReenter ? `<button type="button" class="add-btn" data-leg-reenter="${t.id}" title="Buy or sell this contract again">Re-enter</button>` : ""}</td></tr>`;
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
    ${s.status === "ACTIVE" ? `<div class="strategy-exits">
      <span class="ex-chip">${cfg.exit_sl_pnl != null
        ? `Combined SL <b class="${cfg.exit_sl_pnl >= 0 ? "pos" : "neg"}">${cfg.exit_sl_pnl >= 0 ? "+" : ""}${money(cfg.exit_sl_pnl)}${cfg.exit_sl_pnl >= 0 ? " locked" : ""}</b>`
        : `Max loss <b class="neg">${cfg.exit_loss_amount ? money(cfg.exit_loss_amount) : "off"}</b>`}</span>
      <span class="ex-chip">Max profit <b class="pos">${cfg.exit_profit_amount ? money(cfg.exit_profit_amount) : "off"}</b></span>
      <button type="button" class="ex-edit" data-exits="${s.id}" title="Change the combined max loss / max profit">✎ Edit</button></div>` : ""}
    ${failedHtml}
    <div class="leg-table-scroll">
    <table class="legs-live"><tr><th class="c-side">Side</th><th class="c-sym">Contract</th><th class="num c-qty">Qty</th><th class="num c-px">Entry</th>
      <th class="num c-px">Exit</th>
      <th class="num c-px">LTP <button type="button" class="ltp-refresh" data-strategy-ltp="${s.id}" title="Refresh every leg's LTP now">↻</button></th>
      <th class="num c-px">SL</th><th class="num c-px">TP</th><th class="num c-pnl">P&amp;L</th><th class="c-status">Status</th><th class="c-act"></th></tr>${legRows}</table>
    </div>
  </div>`;
}

// Points/percent/price <-> absolute price, for one side (kind: "sl" or "tp") - the same convention the
// leg-creation table and legSlTpPrices() use: BUY target above entry & stop below, SELL the reverse.
/** Re-express an SL/TP value typed in one mode (PRICE | POINTS | PERCENT) in another, relative to `entry`.
 * Blank stays blank. Prices are tick-rounded; points and percent are shown to 2 decimals. */
function convertSlTp(entry, side, kind, value, from, to, tick) {
  if (value === "" || value == null || from === to || entry == null) return value;
  const price = slTpValueToPrice(entry, side, kind, value, from);
  if (price == null || !Number.isFinite(price)) return value;
  if (to === "PRICE") return toTick(price, tick);
  const pts = priceToPoints(entry, side, kind, price);
  return to === "PERCENT" ? Math.round((pts / entry) * 10000) / 100 : pts;
}

/** Nearest multiple of the tick (2-decimal clean): the server only accepts tick-multiple prices. */
function toTick(price, tick) {
  if (price == null || !Number.isFinite(price) || !(tick > 0)) return price;
  return Math.round(Math.round(price / tick) * tick * 100) / 100;
}
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
    const f = (name, label, val, attrs = 'type="number" step="any" min="0"') =>
      `<label>${label} <input name="${name}" ${attrs} value="${val ?? ""}"></label>`;
    // The dialog opens with the CURRENT stop/target, shown in the mode last used (Price ₹ if none yet), and
    // switching the mode converts the value shown (Price <-> Points from the avg fill <-> %) instead of keeping the
    // raw number, so "Points" shows how far the current stop is from your entry.
    const ref = t.entry_avg_price ?? t.entry_price;
    const tick = Number(t.tick_size) || 0.05;
    const ltp = t.kite_ltp ?? t.last_ltp;
    const slMode = rememberedMode("sl"), tpMode = rememberedMode("tp");
    const shown = (kind, price, mode) => (price == null ? "" : convertSlTp(ref, t.side, kind, toTick(Number(price), tick), "PRICE", mode, tick));
    const typeOpts = (kind, sel) => `
        <option value="PRICE" ${sel === "PRICE" ? "selected" : ""}>Price ₹</option>
        <option value="POINTS" ${sel === "POINTS" ? "selected" : ""}>Points</option>
        <option value="PERCENT" ${sel === "PERCENT" ? "selected" : ""}>${kind === "sl" ? "SL%" : "TP%"}</option>`;
    const html = `<p class="hint small edit-ref">Entry ${t.entry_avg_price != null ? "avg" : "limit"} <b>${num(ref)}</b> ·
        LTP <b data-trade-ltp="${t.id}">${num(ltp)}</b> · current SL <b>${num(t.current_sl)}</b> ·
        target <b>${t.target != null ? num(t.target) : "none"}</b></p>
      <div id="leg-edit-form" class="edit-leg-grid">
      ${entryOpen ? f("entry_price", `<span>Entry limit ₹ ${ltpBtn("#leg-edit-form input[name=entry_price]")}</span>`, t.entry_price) + f("lots", `Lots (filled ${t.filled_qty})`, t.lots, 'type="number" step="1" min="1"') : ""}
      <label>Stop-loss <span class="slp-cell">
        <input id="edit-sl-value" type="number" step="any" min="0" value="${shown("sl", t.current_sl, slMode)}">
        <select id="edit-sl-type" data-prev="${slMode}">${typeOpts("sl", slMode)}</select>
      </span></label>
      <label>Target (blank = none) <span class="slp-cell">
        <input id="edit-tp-value" type="number" step="any" min="0" value="${shown("tp", t.target, tpMode)}">
        <select id="edit-tp-type" data-prev="${tpMode}">${typeOpts("tp", tpMode)}</select>
      </span></label>
    </div>`;
    const pending = dialog(`Edit leg: ${t.side} ${t.tradingsymbol}`, html);
    wireLtpFill(t);
    for (const kind of ["sl", "tp"]) {
      const sel = $(`#edit-${kind}-type`), box = $(`#edit-${kind}-value`);
      sel.onchange = () => {
        box.value = convertSlTp(ref, t.side, kind, box.value, sel.dataset.prev, sel.value, tick);
        sel.dataset.prev = sel.value;
        rememberMode(kind, sel.value);
      };
    }
    const go = await pending;
    if (!go) return;
    const changes = {};
    $$("#leg-edit-form input[name]").forEach((el) => { changes[el.name] = el.value; });
    const slVal = $("#edit-sl-value").value, tpVal = $("#edit-tp-value").value;
    // any number is accepted in the box (points, %, price); the resulting PRICE is rounded to the contract's
    // tick here, so e.g. "10 points" from a 68.53 average fill becomes 58.55, not a rejected 58.53
    const slPrice = toTick(slTpValueToPrice(ref, t.side, "sl", slVal, $("#edit-sl-type").value), tick);
    const tpPrice = tpVal === "" ? null : toTick(slTpValueToPrice(ref, t.side, "tp", tpVal, $("#edit-tp-type").value), tick);
    if (changes.entry_price !== undefined && changes.entry_price !== "") changes.entry_price = toTick(Number(changes.entry_price), tick);
    if (slPrice != null) changes.stop_loss = slPrice;
    changes.target = tpPrice ?? "";
    const p = await api(`/api/trades/${tid}/edit/prepare`, {changes});
    if (!p.ok && (p.errors || []).join() === "nothing changed") return;   // saved without changes: just close
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

// Add lots to a running leg (into the SAME leg: one position at the average price, SL resized), or re-enter a
// closed one (a new leg of this strategy, either side via the toggle, with its own SL / target).
async function addToLeg(tid, reenter) {
  try {
    const t = (await api(`/api/trades/${tid}`)).trade;
    const tick = Number(t.tick_size) || 0.05;
    const ltp = t.kite_ltp ?? t.last_ltp;
    const px = ltp != null ? toTick(Number(ltp), tick) : toTick(Number(t.entry_avg_price ?? t.entry_price), tick);
    const ref = Number(t.entry_avg_price ?? t.entry_price);
    // adding: same SL / target as the running leg; re-entering: same distances as last time, from today's price
    const sl = reenter ? toTick(px - (ref - Number(t.initial_sl)), tick) : t.current_sl;
    const tp = t.target == null ? "" : reenter ? toTick(px + (Number(t.target) - ref), tick) : t.target;
    let side = t.side;
    const toggle = reenter ? `<div class="side-toggle" role="radiogroup" aria-label="Side">
        <button type="button" data-side="BUY" class="BUY">Buy</button><button type="button" data-side="SELL" class="SELL">Sell</button></div>` : "";
    const html = `${toggle}<div class="big-side ${esc(t.side)}" id="add-head">${esc(t.side)} ${esc(t.tradingsymbol)}</div>
      <p class="hint small">LTP <b data-trade-ltp="${t.id}">${num(ltp)}</b> · lot size ${t.lot_size}${reenter
        ? ". Placed as a new leg of this strategy with its own SL / target."
        : ` · holding ${Math.floor(t.open_qty / t.lot_size)} lot(s) @ ${num(ref)}. Added to this leg: quantity and average price
           update, and SL ${num(t.current_sl)} / target ${t.target != null ? num(t.target) : "none"} cover all of it (change with Edit).`}</p>
      <div class="edit-leg-grid">
        <label>Lots <input id="add-lots" type="number" min="1" step="1" value="${reenter ? t.lots : 1}"></label>
        <label>Order <select id="add-type"><option value="LIMIT">Limit</option><option value="MARKET">Market</option></select></label>
        <label><span>Price ₹ ${ltpBtn("#add-price")}</span> <input id="add-price" type="number" step="any" min="0" value="${px}"></label>
        ${reenter ? `<label>Stop-loss ₹ (blank = auto) <input id="add-sl" type="number" step="any" min="0" value="${sl ?? ""}"></label>
        <label>Target ₹ (blank = none) <input id="add-tp" type="number" step="any" min="0" value="${tp}"></label>` : ""}
      </div>`;
    const pending = dialog(reenter ? "Re-enter leg" : `Add to leg #${t.id}`, html, MODE === "LIVE");
    const show = () => {
      $("#add-head").className = `big-side ${side}`;
      $("#add-head").textContent = `${side} ${t.tradingsymbol}`;
      $$(".side-toggle button").forEach((b) => b.classList.toggle("on", b.dataset.side === side));
      okButton(side === "BUY" ? "Buy" : "Sell", side);
    };
    $$(".side-toggle button").forEach((b) => {
      b.onclick = () => {
        if (b.dataset.side === side) return;
        side = b.dataset.side;
        // a stop-loss / target is on the other side of the price for the other side: mirror them, same distance
        const p = Number($("#add-price").value);
        for (const id of ["#add-sl", "#add-tp"]) {
          const v = $(id).value;
          if (v !== "" && p > 0) { const m = toTick(2 * p - Number(v), tick); $(id).value = m > 0 ? m : ""; }
        }
        show();
      };
    });
    show();
    wireLtpFill(t);
    $("#add-type").onchange = () => { $("#add-price").disabled = $("#add-type").value === "MARKET"; };
    if (!(await pending)) return;
    const n = (id) => (!$(id) || $(id).value === "" ? null : toTick(Number($(id).value), tick));
    const what = `${side === "BUY" ? "Buy" : "Sell"} ${t.tradingsymbol}`;
    const r = await api(`/api/strategies/legs/${tid}/add`, {
      side, lots: Math.floor(Number($("#add-lots").value)) || 0, price_type: $("#add-type").value,
      entry_price: n("#add-price"), stop_loss: n("#add-sl"), target: n("#add-tp")});
    if (!r.ok) return alertBox(`${what}: not placed`, r.errors || []);
    refreshStrategies();
  } catch (e) { alertBox("Error", [e.message]); }
}

// Change a running strategy's combined max loss / max profit (blank = off).
// Change a running strategy's combined SL (a signed P&L level: -2000 = exit at a ₹2,000 loss, +1500 = exit if
// the profit falls back to ₹1,500, i.e. profit locked - raise it by hand to trail) and max profit. Blank = off.
async function editExits(sid) {
  try {
    const s = (await api(`/api/strategies/${sid}`)).strategy;
    const c = s.config;
    const now = s.rule_pnl ?? s.combined_pnl;
    const slNow = c.exit_sl_pnl ?? (c.exit_loss_amount ? -c.exit_loss_amount : "");
    const ok = await dialog(`Combined exit: #${s.id} ${s.name}`, `
      <p class="hint small">Combined P&amp;L now <b class="${now >= 0 ? "pos" : "neg"}">${money(now)}</b>. Every leg is squared off when it
        falls to the combined SL or rises to the max profit. Blank = off.</p>
      <div class="edit-leg-grid">
        <label>Combined SL (P&amp;L ₹) <input id="ex-sl" type="number" step="1" placeholder="off, e.g. -2000" value="${slNow}"></label>
        <label>Max profit ₹ <input id="ex-profit" type="number" min="0" step="1" placeholder="off" value="${c.exit_profit_amount ?? ""}"></label>
      </div>
      <p class="hint small">−2000 = exit at a ₹2,000 loss · +1500 = exit if the profit falls back to ₹1,500 (locks it in;
        raise it as the profit grows to trail).</p>`);
    if (!ok) return;
    const sl = $("#ex-sl").value === "" ? null : Number($("#ex-sl").value);
    const vals = {exit_sl_pnl: sl, exit_profit_amount: $("#ex-profit").value || null};
    const openQty = s.legs.reduce((a, t) => a + (LEG_LIVE_STATUSES.has(t.status) ? (t.open_qty || t.quantity || 0) : 0), 0);
    const warn = [];
    if (sl != null && sl >= now) warn.push(`Combined SL ${money(sl)} is at or above the P&L now (${money(now)}): every leg will be squared off at once.`);
    else if (sl != null && openQty && (now - sl) / openQty < TIGHT_PTS)
      warn.push(`Combined SL ${money(sl)} is only ${((now - sl) / openQty).toFixed(1)} points below the P&L now on ${openQty} quantity: normal bid/ask movement can trigger it.`);
    warn.push(...tightExits({exit_profit_amount: vals.exit_profit_amount}, openQty));
    if (!(await confirmTight(warn))) return;
    await api(`/api/strategies/${sid}/exits`, vals);
    refreshStrategies();
  } catch (e) { alertBox("Error", [e.message]); }
}

// A leg still waiting for its entry: fill it now at the market (the resting limit moves to LTP +/- buffer).
async function legToMarket(tid) {
  try {
    const t = (await api(`/api/trades/${tid}`)).trade;
    const ltp = t.kite_ltp ?? t.last_ltp;
    const pending = dialog(`${t.side === "BUY" ? "Buy" : "Sell"} at market?`,
      `<div class="big-side ${esc(t.side)}">${esc(t.side)} ${contractName(t)} · ${t.quantity} qty</div>
       <p class="hint small">Limit ${num(t.entry_price)} → about ${ltp != null ? num(ltp) : "the live price"} (a marketable limit, so it fills
       now but never at an absurd price). SL ${num(t.current_sl)} stays${t.sl_auto ? " (automatic: moves with the price)" : ""}.</p>`, MODE === "LIVE");
    okButton(t.side === "BUY" ? "Buy now" : "Sell now", t.side);
    if (!(await pending)) return;
    const r = await api(`/api/trades/${tid}/entry_market`, {});
    $("#form-hint").textContent = `${t.side} ${t.tradingsymbol}: limit moved to ${num(r.price)} (LTP ${num(r.ltp)})`;
    refreshStrategies();
  } catch (e) { alertBox("Not sent to market", [e.message]); }
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

// Running strategies are always listed first; finished ones are history, filtered by date and paged.
let STRATEGIES = [];
const STRAT_FILTER = HistoryFilter.create($("#strategies-filter"), "strategies", () => renderStrategies());
// Period P&L next to the heading: every strategy in the selected period (all pages, not just this one) plus the
// running ones shown on top; follows pushed P&L between dashboard refreshes.
let SHOWN = [];
function renderPeriodTotal() {
  const sum = SHOWN.reduce((a, s) => a + (Number(s.combined_pnl) || 0), 0);
  const tot = $("#strategies-total");
  tot.textContent = SHOWN.length ? `P&L ${money(sum)}` : "";
  tot.className = `period-total ${sum > 0 ? "pos" : sum < 0 ? "neg" : ""}`;
}
Live.onStrategy((id, v) => {
  const s = STRATEGIES.find((x) => x.id === id);
  if (s && v != null) { s.combined_pnl = v; renderPeriodTotal(); }
});
// "Show cancelled": strategies where nothing was ever bought or sold are hidden by default (remembered).
const placedSomething = (s) => s.status === "ACTIVE" || (s.legs || []).some((t) => (t.filled_qty || 0) > 0);
let SHOW_CANCELLED = false;
try { SHOW_CANCELLED = localStorage.getItem("trader:strategies:show-cancelled") === "1"; } catch (e) { /* ignore */ }
{
  const lab = document.createElement("label");
  lab.className = "inline-check";
  lab.innerHTML = `<input type="checkbox" id="show-cancelled" ${SHOW_CANCELLED ? "checked" : ""}> Show cancelled`;
  $("#strategies-filter").insertBefore(lab, $("#strategies-filter .history-pager"));
  $("#show-cancelled").onchange = (e) => {
    SHOW_CANCELLED = e.target.checked;
    try { localStorage.setItem("trader:strategies:show-cancelled", SHOW_CANCELLED ? "1" : "0"); } catch (err) { /* ignore */ }
    renderStrategies();
  };
}
function renderStrategies() {
  const list_ = SHOW_CANCELLED ? STRATEGIES : STRATEGIES.filter(placedSomething);
  const {pinned, page, total, shown} = STRAT_FILTER.apply(list_, (s) => s.created_at, (s) => s.status === "ACTIVE");
  SHOWN = shown;
  renderPeriodTotal();
  const html = [...pinned, ...page].map(strategyCard).join("") ||
    `<p class="hint small">${total || STRATEGIES.length ? "No finished strategies in this date range." : "No strategies yet."}</p>`;
  const list = $("#strategies-list");
  if (list._html !== html) { list.innerHTML = html; list._html = html; }   // unchanged state: leave the DOM alone
  Live.reapply();
}

async function refreshStrategies() {
  let d;
  try { d = await api("/api/dashboard"); } catch (e) { $("#banner").textContent = "offline"; $("#banner").title = "Server unreachable: " + e.message; return; }
  const b = $("#banner");                                  // small LIVE / PAPER pill next to the title
  b.className = "mode-pill" + (d.live ? " live" : "");
  b.textContent = d.live ? "● LIVE" : "PAPER";
  b.title = d.live ? "LIVE: orders go to Zerodha with real money" : "PAPER: simulated exchange, no orders reach Zerodha";
  MODE = d.mode;
  const h = $("#halt");
  h.classList.toggle("hidden", !d.halted);
  if (d.halted) h.textContent = `New trades blocked: ${d.halted}`;
  try {
    STRATEGIES = (await api("/api/strategies")).strategies;
    renderStrategies();
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
  const aid = ev.target.dataset.legAdd;
  if (aid) return addToLeg(Number(aid), false);
  const rid = ev.target.dataset.legReenter;
  if (rid) return addToLeg(Number(rid), true);
  const lid = ev.target.dataset.legExit;
  if (lid) return exitLeg(Number(lid));
  const mid = ev.target.dataset.legMarket;
  if (mid) return legToMarket(Number(mid));
  const cid = ev.target.dataset.legCancel;
  if (cid) return cancelLeg(Number(cid));
  const eid = ev.target.dataset.legEdit;
  if (eid) return editLeg(Number(eid));
  const xid = ev.target.dataset.exits;
  if (xid) return editExits(Number(xid));
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
Live.onPrice((k, px) => { if (k === $("#base-spot").dataset.price) { SPOT = px; renderSpotChange(); } });
Live.onDashboard(refreshStrategies);
Live.start();
refreshStrategies();
setInterval(refreshStrategies, 30000);


// -- layout: settings drawer, one-screen workspace, resizable summary rail, chart that fills its space ------------
{
  const drawer = $("#config-panel"), scrim = $("#drawer-scrim"), openBtn = $("#settings-open");
  const setDrawer = (open) => {
    drawer.classList.toggle("open", open);
    drawer.setAttribute("aria-hidden", String(!open));
    openBtn.setAttribute("aria-expanded", String(open));
    scrim.hidden = !open;
    if (open) $("#settings-close").focus(); else openBtn.focus();
  };
  openBtn.onclick = () => setDrawer(true);
  $("#settings-close").onclick = () => setDrawer(false);
  scrim.onclick = () => setDrawer(false);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer.classList.contains("open")) setDrawer(false); });
  // the top-bar button shows the settings that matter at a glance: order type + square-off time
  const chip = renderSettingsChip = () => {
    const ot = $("input[name=order_type]:checked")?.value || "";
    const sq = $("#cfg-square-off").value;
    $("#settings-chip").textContent = [ot, sq && `sq-off ${sq}`].filter(Boolean).join(" · ");
  };
  drawer.addEventListener("change", chip);
  drawer.addEventListener("input", chip);
  chip();
  setTimeout(chip, 0);                                   // after the remembered settings are restored

  // workspace height = the screen below the banner + top bar
  const ws = $("#workspace");
  const fit = () => document.documentElement.style.setProperty("--ws-top", `${ws.getBoundingClientRect().top + window.scrollY}px`);
  fit();
  window.addEventListener("resize", fit);
  new ResizeObserver(fit).observe($("#halt"));

  // summary rail width: drag the handle (or ←/→ when it has focus); remembered
  const KEY = "trader:strategy:rail-w", MIN = 320, MAX = () => Math.max(MIN, Math.min(820, window.innerWidth - 820));   // the legs table keeps ~800px
  const setW = (w) => {
    const v = Math.round(Math.max(MIN, Math.min(MAX(), w)));
    document.documentElement.style.setProperty("--rail-w", `${v}px`);
    try { localStorage.setItem(KEY, String(v)); } catch (e) { /* ignore */ }
  };
  try { const w = Number(localStorage.getItem(KEY)); if (w) setW(w); } catch (e) { /* ignore */ }
  const handle = $("#rail-resizer"), rail = $("#summary-rail");
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    const right = ws.getBoundingClientRect().right;
    const move = (ev) => setW(right - ev.clientX - 4);
    const up = () => { handle.classList.remove("dragging"); handle.removeEventListener("pointermove", move); };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up, {once: true});
  });
  handle.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 60 : 20;
    if (e.key === "ArrowLeft") { setW(rail.offsetWidth + step); e.preventDefault(); }
    if (e.key === "ArrowRight") { setW(rail.offsetWidth - step); e.preventDefault(); }
  });

  // redraw the payoff chart when its box changes size (rail drag, window resize)
  let raf = 0, last = "";
  new ResizeObserver(([e]) => {
    const k = `${Math.round(e.contentRect.width)}x${Math.round(e.contentRect.height)}`;
    if (k === last) return;
    last = k;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(renderPayoff);
  }).observe($("#payoff-chart"));
}
