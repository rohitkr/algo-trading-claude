// Shared page behaviour (Strategy Builder + Signals):
// 1. The header scrolls away with the page at first; once you are past it, it slides down from the top and stays
//    pinned (with a shadow); back at the very top it sits in its normal place again - no jump either way.
// 2. Scroll bars show only while scrolling (strategy.css hides them at rest), like macOS's default.
"use strict";
(() => {
  const header = document.querySelector(".topbar");
  if (header) {
    let stuck = false;
    const update = () => {
      const h = header.offsetHeight;
      if (!stuck && window.scrollY > h + 40) {
        document.documentElement.style.setProperty("--hdr-h", `${h}px`);
        document.body.classList.add("header-stuck");        // keeps the header's space: the content doesn't jump
        header.classList.add("stuck");                      // fixed at the top, slides down (CSS animation)
        stuck = true;
      } else if (stuck && window.scrollY <= 0) {
        header.classList.remove("stuck");
        document.body.classList.remove("header-stuck");
        stuck = false;
      }
    };
    window.addEventListener("scroll", update, {passive: true});
    update();
  }
  const timers = new WeakMap();
  document.addEventListener("scroll", (e) => {
    const el = e.target === document ? document.documentElement : e.target;
    if (!el.classList) return;
    el.classList.add("is-scrolling");
    clearTimeout(timers.get(el));
    timers.set(el, setTimeout(() => el.classList.remove("is-scrolling"), 900));
  }, {capture: true, passive: true});
})();
