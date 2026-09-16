/* tests/js/shim.mjs — the smallest set of globals that lets the real frontend
   modules under src/arb/ui/static/js/core/ be imported and driven by
   `node --test`. No package.json, no npm, no dependency: this is the same
   `node` binary that already runs `node --check` in every verification pass.

   WHAT THIS IS
   A stub, deliberately, not a DOM. It implements exactly the handful of DOM
   operations the core modules actually call, and nothing else:

     getElementById / createElement / createTextNode / activeElement / body
     element.focus() / .blur() / .closest() / .contains() / .append()
     .replaceChildren() / .classList / .dataset / .hidden / .value / .textContent
     addEventListener + dispatchEvent (own-target only: NO bubbling, NO
     capture, NO event delegation)
     window.matchMedia, location, history, requestAnimationFrame, localStorage

   WHAT IT DELIBERATELY DOES NOT DO — and what that means for a test
     * No layout, no CSS, no `hidden` inheritance from an ancestor.
     * No real focus model: focus() just assigns document.activeElement. A
       browser drops focus to <body> when the focused node is removed or
       hidden; this stub does NOT. That is precisely the mechanism behind the
       shipped /control bug (replaceChildren destroyed a live <input> and
       focus fell to <body>), so THAT BUG CANNOT BE TESTED HERE. It needs a
       real DOM and a real render loop — it belongs in the browser layer.
     * No event propagation, so a test must call `onKey(e)` directly rather
       than dispatching at a leaf and expecting the document listener to see
       it. `install()` is never exercised here for the same reason.
     * `querySelectorAll` answers only the selectors core/router.js uses.

   Import this module FIRST in a test file: ESM evaluates imports in source
   order, so the globals are installed before the modules under test run. */

import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));

/** Absolute file:// URL of a module under src/arb/ui/static/js/. */
export function jsUrl(rel) {
  return new URL(
    "file://" + path.resolve(here, "../../src/arb/ui/static/js/", rel),
  ).href;
}

// ---------- selectors: only the shapes the core actually uses ----------

/* Supported: "tag", "#id", "[data-attr]", '[data-attr="value"]' and a single
   tag+attr pair ("a[data-page]"). Anything else throws rather than silently
   matching nothing — a test that needs a real selector engine is a test that
   belongs in the browser layer. */
function matches(el, sel) {
  if (!el || el.nodeType !== 1) return false;
  const s = sel.trim();
  const m = /^([a-zA-Z]*)(?:#([\w-]+))?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/.exec(s);
  if (!m || (!m[1] && !m[2] && !m[3])) {
    throw new Error("shim.matches: unsupported selector " + JSON.stringify(sel));
  }
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  if (m[2] && el.id !== m[2]) return false;
  if (m[3]) {
    const v = el.getAttribute(m[3]);
    if (v == null) return false;
    if (m[4] !== undefined && v !== m[4]) return false;
  }
  return true;
}

function dataKey(attr) {
  return attr.startsWith("data-")
    ? attr.slice(5).replace(/-([a-z])/g, (_x, c) => c.toUpperCase())
    : null;
}

class ClassList {
  constructor() {
    this._set = new Set();
  }
  add(...cs) {
    for (const c of cs) this._set.add(c);
  }
  remove(...cs) {
    for (const c of cs) this._set.delete(c);
  }
  contains(c) {
    return this._set.has(c);
  }
  toggle(c, force) {
    const on = force === undefined ? !this._set.has(c) : !!force;
    if (on) this._set.add(c);
    else this._set.delete(c);
    return on;
  }
  toString() {
    return Array.from(this._set).join(" ");
  }
}

class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.textContent = String(text);
    this.parentNode = null;
  }
}

class El {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.id = "";
    this.hidden = false;
    this.isContentEditable = false;
    this.title = "";
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this.classList = new ClassList();
    this._attrs = new Map();
    this._listeners = new Map();
    this._text = "";
    // focus() bookkeeping the tests assert on
    this.focusCount = 0;
    this.blurCount = 0;
  }

  get className() {
    return this.classList.toString();
  }
  set className(v) {
    this.classList = new ClassList();
    for (const c of String(v).split(/\s+/)) if (c) this.classList.add(c);
  }

  get textContent() {
    if (this.childNodes.length) return this.childNodes.map((n) => n.textContent).join("");
    return this._text;
  }
  set textContent(v) {
    this.childNodes = [];
    this._text = v == null ? "" : String(v);
  }

  setAttribute(k, v) {
    this._attrs.set(k, String(v));
    const dk = dataKey(k);
    if (dk) this.dataset[dk] = String(v);
    if (k === "id") this.id = String(v);
  }
  getAttribute(k) {
    if (this._attrs.has(k)) return this._attrs.get(k);
    const dk = dataKey(k);
    if (dk && dk in this.dataset) return String(this.dataset[dk]);
    return null;
  }
  removeAttribute(k) {
    this._attrs.delete(k);
    const dk = dataKey(k);
    if (dk) delete this.dataset[dk];
  }
  hasAttribute(k) {
    return this.getAttribute(k) != null;
  }

  append(...nodes) {
    for (const n of nodes) {
      if (n == null) continue;
      const node = typeof n === "string" ? new TextNode(n) : n;
      node.parentNode = this;
      this.childNodes.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const n of this.childNodes) n.parentNode = null;
    this.childNodes = [];
    this._text = "";
    this.append(...nodes);
  }
  get children() {
    return this.childNodes.filter((n) => n.nodeType === 1);
  }

  closest(sel) {
    let n = this;
    while (n && n.nodeType === 1) {
      if (matches(n, sel)) return n;
      n = n.parentNode;
    }
    return null;
  }
  contains(node) {
    let n = node;
    while (n) {
      if (n === this) return true;
      n = n.parentNode;
    }
    return false;
  }

  /* NOT the browser's focus model: no focusability check, no focusin event,
     and nothing takes focus away when this node is hidden or replaced. */
  focus() {
    this.focusCount += 1;
    doc.activeElement = this;
  }
  blur() {
    this.blurCount += 1;
    if (doc.activeElement === this) doc.activeElement = doc.body;
  }

  addEventListener(type, fn) {
    const l = this._listeners.get(type);
    if (l) l.push(fn);
    else this._listeners.set(type, [fn]);
  }
  removeEventListener(type, fn) {
    const l = this._listeners.get(type) || [];
    const i = l.indexOf(fn);
    if (i >= 0) l.splice(i, 1);
  }
  /* Own-target dispatch only. `bubbles: true` is accepted and IGNORED. */
  dispatchEvent(ev) {
    if (ev && ev.target == null) {
      try {
        Object.defineProperty(ev, "target", { value: this, configurable: true });
      } catch {
        /* a plain object event: ignore */
      }
    }
    for (const fn of (this._listeners.get(ev.type) || []).slice()) fn(ev);
    return true;
  }
}

// ---------- document / window ----------

const byId = new Map();

const doc = {
  nodeType: 9,
  title: "",
  body: null,
  activeElement: null,
  _listeners: new Map(),
  createElement(tag) {
    return new El(tag);
  },
  createTextNode(t) {
    return new TextNode(t);
  },
  getElementById(id) {
    return byId.get(id) || null;
  },
  /* Only the selectors core/router.js asks for. Elements are registered with
     mkEl(); there is no tree walk because there is no real tree. */
  querySelectorAll(sel) {
    const parts = sel.split(/\s+/).filter(Boolean);
    const leaf = parts[parts.length - 1];
    const out = [];
    for (const el of byId.values()) {
      let ok = false;
      try {
        ok = matches(el, leaf);
      } catch {
        ok = false;
      }
      if (ok) out.push(el);
    }
    return out;
  },
  addEventListener(type, fn) {
    const l = doc._listeners.get(type);
    if (l) l.push(fn);
    else doc._listeners.set(type, [fn]);
  },
  removeEventListener(type, fn) {
    const l = doc._listeners.get(type) || [];
    const i = l.indexOf(fn);
    if (i >= 0) l.splice(i, 1);
  },
  dispatchEvent(ev) {
    for (const fn of (doc._listeners.get(ev.type) || []).slice()) fn(ev);
    return true;
  },
};

const loc = { pathname: "/", search: "", host: "127.0.0.1:8080", protocol: "http:" };

const hist = {
  entries: [],
  pushState(state, _t, url) {
    hist.entries.push({ state, url, kind: "push" });
    applyUrl(url);
  },
  replaceState(state, _t, url) {
    hist.entries.push({ state, url, kind: "replace" });
    applyUrl(url);
  },
  back() {
    hist.entries.push({ kind: "back" });
  },
};

function applyUrl(url) {
  const u = String(url);
  const cut = u.search(/[?#]/);
  loc.pathname = cut >= 0 ? u.slice(0, cut) : u;
  loc.search = cut >= 0 && u[cut] === "?" ? u.slice(cut) : "";
}

const win = {
  addEventListener(type, fn) {
    doc.addEventListener("window:" + type, fn);
  },
  removeEventListener(type, fn) {
    doc.removeEventListener("window:" + type, fn);
  },
  dispatchEvent(ev) {
    return doc.dispatchEvent({ ...ev, type: "window:" + ev.type });
  },
  matchMedia() {
    return { matches: false, addEventListener() {}, removeEventListener() {} };
  },
  get location() {
    return loc;
  },
  get history() {
    return hist;
  },
  get document() {
    return doc;
  },
};

// ---------- the rAF queue: frames run only when a test says so ----------

let rafQueue = [];
let rafSeq = 0;

function requestAnimationFrame(fn) {
  rafSeq += 1;
  rafQueue.push(fn);
  return rafSeq;
}

/** Run exactly the callbacks queued right now (one frame). Callbacks queued
    by those callbacks are left for the next frame, like a real browser. */
export function runFrame() {
  const q = rafQueue;
  rafQueue = [];
  for (const fn of q) fn();
  return q.length;
}

/** Run frames until the queue drains, bounded. Returns the frame count. */
export function flushFrames(max = 8) {
  let n = 0;
  while (rafQueue.length && n < max) {
    runFrame();
    n += 1;
  }
  return n;
}

export function pendingFrames() {
  return rafQueue.length;
}

// ---------- install the globals ----------

function install() {
  doc.body = new El("body");
  doc.activeElement = doc.body;
  globalThis.document = doc;
  globalThis.window = win;
  globalThis.location = loc;
  globalThis.history = hist;
  globalThis.requestAnimationFrame = requestAnimationFrame;
  globalThis.cancelAnimationFrame = () => {};
  globalThis.localStorage = {
    _m: new Map(),
    getItem(k) {
      return this._m.has(k) ? this._m.get(k) : null;
    },
    setItem(k, v) {
      this._m.set(k, String(v));
    },
    removeItem(k) {
      this._m.delete(k);
    },
    clear() {
      this._m.clear();
    },
  };
  setNavigator({ platform: "Win32", userAgent: "node" });
  // Timers must never hold `node --test` open: core/cmd.js parks a 2.5 s
  // message timer whose handle it never hands back.
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...rest) => {
    const t = realSetTimeout(fn, ms, ...rest);
    if (t && typeof t.unref === "function") t.unref();
    return t;
  };
}

/** `navigator` is a getter-only global in node: plain assignment throws. */
export function setNavigator(nav) {
  Object.defineProperty(globalThis, "navigator", {
    value: nav,
    configurable: true,
    writable: true,
  });
}

install();

// ---------- helpers the tests build fixtures with ----------

/** Create an element, register it by id and optionally parent it. */
export function mkEl(tag, props = {}) {
  const e = new El(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "parent") {
      v.append(e);
    } else if (k === "attrs") {
      for (const [ak, av] of Object.entries(v)) e.setAttribute(ak, av);
    } else {
      e[k] = v;
    }
  }
  if (e.id) byId.set(e.id, e);
  if (!e.parentNode) doc.body.append(e);
  return e;
}

/** Forget every element created with mkEl and put focus back on <body>. */
export function resetDom() {
  byId.clear();
  doc.body = new El("body");
  doc.activeElement = doc.body;
  doc._listeners.clear();
  rafQueue = [];
  hist.entries.length = 0;
  loc.pathname = "/";
  loc.search = "";
  doc.title = "";
}

/** A keydown-shaped object: exactly the fields core/keys.js reads. */
export function keydown(key, opts = {}) {
  const e = {
    type: "keydown",
    key,
    code: opts.code !== undefined ? opts.code : codeFor(key),
    target: opts.target !== undefined ? opts.target : doc.body,
    ctrlKey: !!opts.ctrlKey,
    metaKey: !!opts.metaKey,
    altKey: !!opts.altKey,
    shiftKey: !!opts.shiftKey,
    repeat: !!opts.repeat,
    isComposing: !!opts.isComposing,
    keyCode: opts.keyCode !== undefined ? opts.keyCode : 0,
    defaultPrevented: !!opts.defaultPrevented,
    prevented: 0,
    preventDefault() {
      this.prevented += 1;
      this.defaultPrevented = true;
    },
    stopPropagation() {},
  };
  return e;
}

function codeFor(key) {
  if (/^[0-9]$/.test(key)) return "Digit" + key;
  if (/^[a-zA-Z]$/.test(key)) return "Key" + key.toUpperCase();
  if (key === "[") return "BracketLeft";
  if (key === "]") return "BracketRight";
  if (key === " ") return "Space";
  return key;
}

export { doc as documentStub, win as windowStub, loc as locationStub, hist as historyStub, El };
