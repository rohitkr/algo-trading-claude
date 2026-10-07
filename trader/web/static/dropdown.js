// Dropdowns that open just BELOW their box. macOS draws a native <select> list on top of the box (the selected item
// lined up over it), which hides what you are choosing from. This replaces only the list: the <select> stays the
// source of truth (value, change/input events, keyboard focus), so every page script keeps working unchanged, and
// it covers selects that are re-rendered later (one delegated listener). <select data-search> adds a search box
// (type to filter, Enter picks the first match) - used for the instrument picker.
"use strict";
(() => {
  let cur = null;                                   // {sel, panel, items, hi, search}

  function close(refocus) {
    if (!cur) return;
    const {sel, panel} = cur;
    panel.remove();
    window.removeEventListener("resize", onResize);
    document.removeEventListener("scroll", onScroll, true);
    cur = null;
    if (refocus) sel.focus();
  }
  const onResize = () => close(false);
  const onScroll = (e) => { if (cur && !cur.panel.contains(e.target)) close(false); };

  function choose(value) {
    const sel = cur.sel;
    close(true);
    if (sel.value === value) return;
    sel.value = value;
    sel.dispatchEvent(new Event("input", {bubbles: true}));
    sel.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function entries(sel) {                           // [{group}] / [{value, label, disabled}] in order
    const out = [];
    const add = (o) => {
      if (o.value === "" && o === sel.options[0]) return;      // a "Templates" style placeholder: not a choice
      out.push({value: o.value, label: o.textContent.trim(), disabled: o.disabled});
    };
    for (const c of sel.children) {
      if (c.tagName === "OPTGROUP") { out.push({group: c.label}); [...c.children].forEach(add); }
      else if (c.tagName === "OPTION") add(c);
    }
    return out;
  }

  function render(filter) {
    const {sel, list} = cur;
    const q = (filter || "").trim().toLowerCase();
    let rows = entries(sel);
    if (q) rows = rows.filter((r) => r.group === undefined && r.label.toLowerCase().includes(q));
    list.innerHTML = "";
    cur.items = [];
    for (const r of rows) {
      const el = document.createElement("div");
      if (r.group !== undefined) { el.className = "dd-group"; el.textContent = r.group; list.append(el); continue; }
      el.className = "dd-item" + (r.value === sel.value ? " selected" : "") + (r.disabled ? " disabled" : "");
      el.setAttribute("role", "option");
      el.setAttribute("aria-selected", String(r.value === sel.value));
      el.textContent = r.label;
      el.dataset.value = r.value;
      if (!r.disabled) cur.items.push(el);
      list.append(el);
    }
    if (!cur.items.length) {
      const el = document.createElement("div");
      el.className = "dd-empty";
      el.textContent = "No match";
      list.append(el);
    }
    const at = q ? 0 : Math.max(0, cur.items.findIndex((el) => el.dataset.value === sel.value));
    highlight(at, true);
  }

  function highlight(i, center) {
    if (!cur.items.length) { cur.hi = -1; return; }
    cur.hi = Math.max(0, Math.min(cur.items.length - 1, i));
    cur.items.forEach((el, k) => el.classList.toggle("hi", k === cur.hi));
    const el = cur.items[cur.hi];
    if (center) el.scrollIntoView({block: "center"}); else el.scrollIntoView({block: "nearest"});
  }

  function open(sel) {
    close(false);
    const searchable = sel.hasAttribute("data-search");
    const panel = document.createElement("div");
    panel.className = "dd-panel";
    panel.setAttribute("role", "listbox");
    const list = document.createElement("div");
    list.className = "dd-list";
    let search = null;
    if (searchable) {
      search = document.createElement("input");
      search.className = "dd-search";
      search.type = "text";
      search.placeholder = "Search…";
      search.setAttribute("aria-label", "Search");
      panel.append(search);
    }
    panel.append(list);
    (sel.closest("dialog") || document.body).append(panel);   // inside an open dialog: stay above it
    cur = {sel, panel, list, items: [], hi: -1, search};
    render("");

    // just below the box (above only when there is clearly more room up there)
    const r = sel.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - 8, above = r.top - 8;
    const up = below < 180 && above > below;
    const maxH = Math.max(120, Math.min(340, up ? above : below));
    panel.style.minWidth = `${Math.max(r.width, searchable ? 220 : 0)}px`;
    panel.style.maxHeight = `${maxH}px`;
    panel.style.left = `${Math.min(r.left, window.innerWidth - panel.offsetWidth - 8)}px`;
    panel.style.top = up ? `${Math.max(8, r.top - 4 - Math.min(maxH, panel.offsetHeight))}px` : `${r.bottom + 4}px`;
    if (cur.hi >= 0) highlight(cur.hi, true);

    panel.addEventListener("mousedown", (e) => {
      if (e.target === search) return;
      e.preventDefault();                                     // keep focus where it is
      const it = e.target.closest(".dd-item");
      if (it && !it.classList.contains("disabled")) choose(it.dataset.value);
    });
    panel.addEventListener("mousemove", (e) => {
      const it = e.target.closest(".dd-item");
      const i = it ? cur.items.indexOf(it) : -1;
      if (i >= 0 && i !== cur.hi) highlight(i, false);
    });
    if (search) {
      search.addEventListener("input", () => render(search.value));
      search.addEventListener("keydown", onKey);
      search.focus();
    }
    window.addEventListener("resize", onResize);
    document.addEventListener("scroll", onScroll, true);
  }

  function onKey(e) {
    if (!cur) return;
    if (e.key === "ArrowDown") { highlight(cur.hi + 1); e.preventDefault(); }
    else if (e.key === "ArrowUp") { highlight(cur.hi - 1); e.preventDefault(); }
    else if (e.key === "Enter") { if (cur.hi >= 0) choose(cur.items[cur.hi].dataset.value); e.preventDefault(); }
    else if (e.key === "Escape") { close(true); e.preventDefault(); e.stopPropagation(); }
    else if (e.key === "Tab") close(false);
    else if (!cur.search && e.key.length === 1) {               // type-ahead on plain lists
      const k = e.key.toLowerCase();
      const i = cur.items.findIndex((el, n) => n > cur.hi && el.textContent.toLowerCase().startsWith(k));
      const j = i >= 0 ? i : cur.items.findIndex((el) => el.textContent.toLowerCase().startsWith(k));
      if (j >= 0) highlight(j);
    }
  }

  const usable = (sel) => sel && !sel.disabled && !sel.multiple && (sel.size || 0) <= 1;
  document.addEventListener("mousedown", (e) => {
    if (cur && cur.panel.contains(e.target)) return;
    const sel = e.target.closest && e.target.closest("select");
    if (!usable(sel)) { close(false); return; }
    e.preventDefault();                                       // no native list
    if (cur && cur.sel === sel) { close(true); return; }
    sel.focus();
    open(sel);
  }, true);
  document.addEventListener("keydown", (e) => {
    if (cur) {
      if (!cur.search || document.activeElement !== cur.search) onKey(e);
      return;
    }
    const sel = document.activeElement;
    if (!usable(sel) || sel.tagName !== "SELECT") return;
    if ([" ", "Enter", "ArrowDown", "ArrowUp"].includes(e.key)) { e.preventDefault(); open(sel); }
  }, true);
})();
