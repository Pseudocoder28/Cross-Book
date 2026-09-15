/* core/dom.js — DOM helpers shared by every page.
   Ported byte-for-byte from the pre-multipage static/app.js (git history). */

const FLASH_MIN_GAP_MS = 84;    // coalesce >12 flashes/s per cell

export const $ = (id) => document.getElementById(id);

const rmQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
let reducedMotion = rmQuery.matches;
if (typeof rmQuery.addEventListener === "function") {
  rmQuery.addEventListener("change", (e) => { reducedMotion = e.matches; });
}

/** True when the user asked for reduced motion. Read it per use — it is live. */
export function isReducedMotion() {
  return reducedMotion;
}

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// Mirrors an element's text into its tooltip, for values that CSS clips.
export function titled(e) {
  if (e.textContent) e.title = e.textContent;
  return e;
}

const rmTimers = new WeakMap();

// Background-only flash: 120ms in (0.2,0,0,1), 240ms linear decay, 14% alpha.
// Reduced motion: persistent glyph + 700 weight for 1s, no animation.
export function flash(cell, kind) {
  const now = performance.now();
  if (cell._lastFlash != null && now - cell._lastFlash < FLASH_MIN_GAP_MS) return;
  cell._lastFlash = now;
  const up = kind === "up" || kind === "bid";
  const down = kind === "down" || kind === "ask";
  if (reducedMotion) {
    if (!up && !down) return;
    cell.classList.remove("rm-up", "rm-down");
    cell.classList.add(up ? "rm-up" : "rm-down");
    clearTimeout(rmTimers.get(cell));
    rmTimers.set(cell, setTimeout(() => cell.classList.remove("rm-up", "rm-down"), 1000));
    return;
  }
  if (typeof cell.animate !== "function") return;
  const c = up ? "rgba(47,224,160,0.14)" : down ? "rgba(255,79,94,0.14)" : "rgba(230,230,230,0.10)";
  cell.animate(
    [
      { backgroundColor: "rgba(0,0,0,0)", easing: "cubic-bezier(0.2,0,0,1)" },
      { backgroundColor: c, offset: 1 / 3, easing: "linear" },
      { backgroundColor: "rgba(0,0,0,0)" },
    ],
    { duration: 360 },
  );
}

export function setVal(elm, text, warn) {
  if (elm.textContent !== text) {
    elm.textContent = text;
    if (elm._has) flash(elm, "neutral");
    elm._has = true;
  }
  if (warn !== undefined) elm.classList.toggle("warn", !!warn);
}

export function setText(id, text) {
  const e = $(id);
  if (e && e.textContent !== text) e.textContent = text;
}
