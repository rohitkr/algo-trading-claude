// Date filter + pagination for history lists (strategy history, completed trades). Client-side over what the
// server returns. Pinned items (running strategies, open trades) are always shown first, whatever the filter;
// only history is filtered and paged. The filter choice is remembered per list (localStorage).
"use strict";
window.HistoryFilter = (() => {
  const PRESETS = [["today", "Today"], ["yesterday", "Yesterday"], ["7d", "Last 7 days"], ["30d", "Last 30 days"],
                   ["all", "All"], ["custom", "Custom"]];
  const SIZES = [10, 25, 50, 100];
  const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const shift = (days) => { const d = new Date(); d.setDate(d.getDate() + days); return ymd(d); };

  function range(st) {                     // [from, to] as YYYY-MM-DD (inclusive), null = open end
    switch (st.preset) {
      case "today": return [shift(0), shift(0)];
      case "yesterday": return [shift(-1), shift(-1)];
      case "7d": return [shift(-6), shift(0)];
      case "30d": return [shift(-29), shift(0)];
      case "custom": return [st.from || null, st.to || null];
      default: return [null, null];
    }
  }

  function create(el, key, onChange, defaults = {}) {
    const storeKey = `trader:history:${key}`;
    let st = {preset: "today", from: "", to: "", size: 25, page: 1, ...defaults};
    try { st = {...st, ...JSON.parse(localStorage.getItem(storeKey) || "{}"), page: 1}; } catch (e) { /* ignore */ }
    const save = () => { try { localStorage.setItem(storeKey, JSON.stringify(st)); } catch (e) { /* ignore */ } };
    let last = {total: 0, pages: 1};

    el.classList.add("history-bar");
    el.innerHTML = `
      <label>Show <select data-h="preset">${PRESETS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></label>
      <span data-h="custom"><input type="date" data-h="from" aria-label="From date"> – <input type="date" data-h="to" aria-label="To date"></span>
      <label>Rows <select data-h="size">${SIZES.map((n) => `<option>${n}</option>`).join("")}</select></label>
      <span class="history-pager"><button type="button" data-h="prev" aria-label="Previous page">‹</button>
        <span data-h="info"></span><button type="button" data-h="next" aria-label="Next page">›</button></span>`;
    const q = (n) => el.querySelector(`[data-h="${n}"]`);
    const sync = () => {
      q("preset").value = st.preset; q("size").value = String(st.size);
      q("from").value = st.from; q("to").value = st.to;
      q("custom").classList.toggle("hidden", st.preset !== "custom");
    };
    el.addEventListener("change", (e) => {
      const n = e.target.dataset.h;
      if (n === "preset") st.preset = e.target.value;
      if (n === "size") st.size = Number(e.target.value);
      if (n === "from") st.from = e.target.value;
      if (n === "to") st.to = e.target.value;
      st.page = 1; save(); sync(); onChange();
    });
    el.addEventListener("click", (e) => {
      const n = e.target.dataset?.h;
      if (n === "prev" && st.page > 1) { st.page -= 1; onChange(); }
      if (n === "next" && st.page < last.pages) { st.page += 1; onChange(); }
    });
    sync();

    return {
      /** items: history rows; dateOf(item) -> ISO timestamp string; pinned(item) -> always shown.
       * Returns {pinned, page} to render (pinned first) and updates the pager text. */
      apply(items, dateOf, pinned) {
        const [from, to] = range(st);
        const keep = [], hist = [];
        for (const it of items) {
          if (pinned(it)) { keep.push(it); continue; }
          const day = String(dateOf(it) || "").slice(0, 10);
          if ((from && day < from) || (to && day > to)) continue;
          hist.push(it);
        }
        const pages = Math.max(1, Math.ceil(hist.length / st.size));
        st.page = Math.min(st.page, pages);
        const start = (st.page - 1) * st.size;
        last = {total: hist.length, pages};
        q("info").textContent = hist.length ? `${start + 1}–${Math.min(start + st.size, hist.length)} of ${hist.length}` : "0 of 0";
        q("prev").disabled = st.page <= 1;
        q("next").disabled = st.page >= pages;
        // shown = everything in the selected period (pinned + every page), e.g. for a period P&L total
        return {pinned: keep, page: hist.slice(start, start + st.size), total: hist.length, shown: [...keep, ...hist]};
      },
    };
  }
  return {create};
})();
