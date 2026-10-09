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
    <td>${esc(s.rationale || "")}</td></tr>`).join("") || `<tr><td colspan="9" class="hint small">No signals yet.</td></tr>`;

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
