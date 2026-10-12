// /telegram: the tips channel as the trader reads it (read-only). Polls /api/telegram/feed every 5 s.
"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const num = (v) => (v == null ? "–" : Number(v).toLocaleString("en-IN", {maximumFractionDigits: 2}));
const ist = (iso, withDate) => {
  const d = new Date(iso);
  const o = {timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false};
  return (withDate ? d.toLocaleDateString("en-IN", {timeZone: "Asia/Kolkata", day: "numeric", month: "short"}) + " " : "") +
    d.toLocaleTimeString("en-IN", o);
};

// theme: same switch and remembered choice as the Strategy Builder
const THEME_KEY = "strategy:theme";
let theme = "dark";
try { theme = localStorage.getItem(THEME_KEY) || "dark"; } catch (e) { /* ignore */ }
const applyTheme = () => { document.documentElement.setAttribute("data-theme", theme); $("#theme-toggle").textContent = theme === "dark" ? "🌙" : "☀️"; };
$("#theme-toggle").onclick = () => { theme = theme === "dark" ? "light" : "dark"; applyTheme(); try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* ignore */ } };
applyTheme();

const STATUS_TEXT = {OPEN: "Open", T1: "Target 1", T2: "Target 2", T3: "Target 3", SL_HIT: "SL hit"};
const KIND_TEXT = {SIGNAL: "Signal", DETAILS: "TP / SL", TARGET: "Target", SL_HIT: "SL hit", TICK: "Price",
  MEDIA: "Image", ADVISORY: "Advice", NOISE: "Chat", UNCLEAR: "Unclear"};
let selected = null, last = "";

function render(d) {
  const st = d.status || {};
  const pill = $("#feed-state");
  pill.textContent = {listening: "● LIVE", connecting: "connecting…", error: "error", off: "off"}[st.state] || st.state;
  pill.className = `mode-pill ${st.state}`;
  $("#feed-channel").textContent = st.channel ? `${st.channel} · ${d.stored} messages stored` : "";
  $("#feed-detail").textContent = st.detail || "";
  $("#feed-detail").classList.toggle("hidden", !st.detail);

  $("#sig-count").textContent = d.signals.length ? `(${d.signals.length})` : "";
  $("#sig-body").innerHTML = d.signals.map((s) => `<tr data-sig="${s.id}" class="${s.id === selected ? "sel" : ""}">
    <td>${ist(s.date, true)}</td>
    <td><span class="dir ${s.direction}">${s.direction === "BULLISH" ? "▲ Bullish" : "▼ Bearish"}</span></td>
    <td>${esc(s.action)} ${esc(s.index)} ${s.strike} ${esc(s.option_type)}</td>
    <td class="num">${num(s.entry_low)}–${num(s.entry_high)}</td>
    <td class="num">${num(s.stop_loss)}</td>
    <td>${(s.targets || []).map((t, i) => `<span class="tgt ${(s.targets_done || []).includes(i + 1) ? "done" : ""}">${num(t)}</span>`).join("")}</td>
    <td><span class="st ${s.status}">${STATUS_TEXT[s.status] || s.status}</span>${s.complete ? "" : ' <small class="hint">(no SL yet)</small>'}</td>
    <td class="num">${num(s.last_price)}</td>
    <td>${ourTrade(s, d.trading)}</td></tr>`).join("") || `<tr><td colspan="9" class="hint small">No signals yet.</td></tr>`;
  renderTrading(d.trading);

  $("#msg-body").innerHTML = d.messages.map((m) => `<tr class="${selected && m.signal_id === selected ? "sel" : ""}">
    <td>${ist(m.date, true)}</td>
    <td><span class="kind ${m.kind}">${KIND_TEXT[m.kind] || m.kind}</span></td>
    <td class="txt">${m.text ? esc(m.text) : '<span class="hint">[image]</span>'}</td>
    <td>${m.signal_id ? `#${m.signal_id}` : ""}</td></tr>`).join("");
}

async function refresh() {
  try {
    const r = await fetch(`/api/telegram/feed${$("#show-ticks").checked ? "?ticks=1" : ""}`);
    const d = await r.json();
    const key = JSON.stringify(d);
    if (key !== last) { last = key; render(d); }
  } catch (e) {
    $("#feed-state").textContent = "offline"; $("#feed-state").className = "mode-pill error";
  }
}
$("#sig-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-sig]");
  if (!tr) return;
  selected = Number(tr.dataset.sig) === selected ? null : Number(tr.dataset.sig);   // highlight its messages
  last = ""; refresh();
});
$("#show-ticks").addEventListener("change", () => { last = ""; refresh(); });
refresh();
setInterval(refresh, 5000);


// -- trading on signals (phase 3) -----------------------------------------------------------------------------
const ACT_TEXT = {PLACED: "Placed", PROPOSED: "Waiting for you", ALERT: "Alert only", SKIPPED: "Skipped", FAILED: "Failed", EXPIRED: "Expired"};
const MODE_HELP = {
  off: "Off: signals are shown, nothing is traded.",
  alert: "Alert: shows the trade each NEW signal would place, places nothing.",
  approve: "Approve: each NEW signal waits for your Place / Skip here (expires).",
  auto: "Auto: each NEW signal is placed at once, through the Strategy Builder's engine and checks.",
};
function ourTrade(s, trading) {
  const a = trading && trading.actions[String(s.id)];
  if (!a) return '<span class="hint small">–</span>';
  const plan = a.plan ? `<span class="act-plan">${esc(a.plan.summary)}</span>` : "";
  const why = a.reason ? `<span class="act-plan">${esc(a.reason)}</span>` : "";
  const strat = a.strategy_id ? ` <a href="/" class="act-plan" title="Open the Strategy Builder">strategy #${a.strategy_id}</a>` : "";
  const btns = a.status === "PROPOSED" ? `<span class="act-btns"><button type="button" class="place" data-approve="${s.id}">Place</button>
    <button type="button" data-skip="${s.id}">Skip</button></span>` : "";
  return `<span class="act ${a.status}">${ACT_TEXT[a.status] || a.status}</span>${strat}${plan}${why}${btns}`;
}

let RULES = null, editing = false;
function renderTrading(trading) {
  const panel = $("#trading-panel");
  if (!trading) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  RULES = trading.rules;
  document.querySelectorAll("#mode-seg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === RULES.mode));
  $("#mode-help").textContent = MODE_HELP[RULES.mode] || "";
  $("#rules-example").textContent = trading.example ? `Latest signal #${trading.example.signal} would place: ${trading.example.summary}` : "";
  if (editing) return;                                       // don't overwrite what you are typing
  const f = $("#rules-form");
  for (const k of ["action", "strike_offset", "spread_width", "leg_sl_points", "max_loss", "max_profit", "order_type",
    "start", "end", "max_trades_per_day", "max_signal_age_s", "approve_timeout_s"]) f[k].value = RULES[k] ?? "";
  f.lots_NIFTY.value = RULES.lots.NIFTY ?? 1;
  f.lots_SENSEX.value = RULES.lots.SENSEX ?? 1;
  f.idx_NIFTY.checked = RULES.indices.includes("NIFTY");
  f.idx_SENSEX.checked = RULES.indices.includes("SENSEX");
  f.exit_on_channel_sl.checked = !!RULES.exit_on_channel_sl;
}
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Trader": "1"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
$("#rules-form").addEventListener("input", () => { editing = true; $("#rules-msg").textContent = "unsaved changes"; });
$("#rules-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, n = (v) => (v === "" ? null : Number(v));
  const body = {
    action: f.action.value, strike_offset: n(f.strike_offset.value) ?? 0, spread_width: n(f.spread_width.value) ?? 1,
    lots: {NIFTY: n(f.lots_NIFTY.value) ?? 1, SENSEX: n(f.lots_SENSEX.value) ?? 1}, leg_sl_points: n(f.leg_sl_points.value),
    max_loss: n(f.max_loss.value), max_profit: n(f.max_profit.value), order_type: f.order_type.value,
    start: f.start.value, end: f.end.value, max_trades_per_day: n(f.max_trades_per_day.value) ?? 0,
    max_signal_age_s: n(f.max_signal_age_s.value) ?? 120, approve_timeout_s: n(f.approve_timeout_s.value) ?? 120,
    indices: ["NIFTY", "SENSEX"].filter((i) => f[`idx_${i}`].checked), exit_on_channel_sl: f.exit_on_channel_sl.checked,
  };
  try { await post("/api/telegram/rules", body); editing = false; $("#rules-msg").textContent = "saved"; last = ""; refresh(); }
  catch (err) { $("#rules-msg").textContent = `not saved: ${err.message}`; }
});
$("#mode-seg").addEventListener("click", async (e) => {
  const m = e.target.dataset?.mode;
  if (!m || !RULES || m === RULES.mode) return;
  if (m === "auto" && !window.confirm("Auto: every NEW signal will be placed as a real trade on your account (in LIVE mode, real money). Turn Auto on?")) return;
  try { await post("/api/telegram/rules", {mode: m}); last = ""; refresh(); }
  catch (err) { $("#rules-msg").textContent = `not changed: ${err.message}`; }
});
$("#sig-body").addEventListener("click", async (e) => {
  const ap = e.target.dataset?.approve, sk = e.target.dataset?.skip;
  if (!ap && !sk) return;
  e.stopPropagation();
  e.target.disabled = true;
  try { await post(ap ? "/api/telegram/approve" : "/api/telegram/skip", {signal_id: Number(ap || sk)}); }
  catch (err) { window.alert(err.message); }
  last = ""; refresh();
}, true);

// Scroll bars show only while scrolling (see strategy.css): mark whatever is being scrolled for ~1 s.
{
  const timers = new WeakMap();
  document.addEventListener("scroll", (e) => {
    const el = e.target === document ? document.documentElement : e.target;
    if (!el.classList) return;
    el.classList.add("is-scrolling");
    clearTimeout(timers.get(el));
    timers.set(el, setTimeout(() => el.classList.remove("is-scrolling"), 900));
  }, {capture: true, passive: true});
}
