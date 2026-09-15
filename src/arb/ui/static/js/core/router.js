/* core/router.js — History API routing over one document.

   Every screen is a route with a real URL, so pages are bookmarkable, browser
   back/forward works and a deep link cold-loads. It stays client side (rather
   than one HTML document per page) because the WebSocket must survive
   navigation — see core/ws.js. */

import { schedule, registerRenderer } from "./state.js";
import { onMessage } from "./ws.js";

const pages = [];               // registration order == nav order
const byId = new Map();

let active = null;
let activeParams = {};

function compile(path) {
  // "/market/:id" -> /^\/market\/([^/]+)$/ with keys ["id"]
  const keys = [];
  const re = path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\\?:(\w+)/g, (_m, k) => {
    keys.push(k);
    return "([^/]+)";
  });
  return { re: new RegExp("^" + re + "$"), keys };
}

/** Register a page module. main.js does this once per page, in nav order. */
export function register(page) {
  if (byId.has(page.id)) return;
  const compiled = compile(page.path);
  const entry = { page, re: compiled.re, keys: compiled.keys };
  pages.push(entry);
  byId.set(page.id, entry);
  const root = document.getElementById(page.root);
  if (root && page !== (active && active.page)) root.hidden = true;
  // The rAF batch calls a page's render only while that page is mounted.
  registerRenderer(page.id, () => {
    if (active === entry) page.render();
  });
  if (typeof page.onMessage === "function") onMessage("*", (m) => page.onMessage(m));
}

/** The page objects that asked to appear in the nav strip, in order. */
export function navPages() {
  return pages.filter((e) => e.page.nav).map((e) => e.page);
}

/** The mounted page object, or null before start(). */
export function currentPage() {
  return active ? active.page : null;
}

function normalize(path) {
  let p = String(path || "/");
  const cut = p.search(/[?#]/);
  if (cut >= 0) p = p.slice(0, cut);
  if (!p.startsWith("/")) p = "/" + p;
  if (p.length > 1 && p.endsWith("/")) p = p.slice(0, -1);
  return p || "/";
}

function match(path) {
  for (const entry of pages) {
    const m = entry.re.exec(path);
    if (!m) continue;
    const params = {};
    entry.keys.forEach((k, i) => {
      try { params[k] = decodeURIComponent(m[i + 1]); } catch (err) { params[k] = m[i + 1]; }
    });
    return { entry, params };
  }
  return null;
}

function markNav(entry) {
  for (const a of document.querySelectorAll("#nav a[data-page]")) {
    const on = entry != null && a.dataset.page === entry.page.id;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
}

/* Every page mounts in COMMAND scope with focus on the ARB> line — but only
   when the keyboard has nowhere better to be. It must NOT be unconditional:
   pages/market.js re-navigates with {replace:true} on every arrow press and
   show() does not early-return when the :id param changed, so an unconditional
   focus() would fire on every single DES arrow and fight whatever the user had
   focused. Focus only when nothing holds it, or when what held it is the root
   being unmounted. */
function restoreFocus(hadFocusInPrev) {
  const a = document.activeElement;
  if (!hadFocusInPrev && a && a !== document.body) return;
  const c = document.getElementById("cmd");
  if (c) c.focus({ preventScroll: true });
}

function show(entry, params) {
  if (active === entry && sameParams(activeParams, params)) {
    activeParams = params;
    markNav(entry);
    return;
  }
  const prev = active;
  active = entry;
  activeParams = params;
  markNav(entry);
  // Read this BEFORE hiding: hiding the element that contains activeElement
  // makes the browser drop focus to <body>, which would erase the answer.
  let hadFocusInPrev = false;
  if (prev) {
    const prevRoot = document.getElementById(prev.page.root);
    hadFocusInPrev = !!(prevRoot && prevRoot.contains(document.activeElement));
    prev.page.unmount();
    if (prevRoot) prevRoot.hidden = true;
  }
  const root = document.getElementById(entry.page.root);
  if (root) root.hidden = false;
  document.title = "ARB · " + entry.page.title;
  entry.page.mount(params);
  restoreFocus(hadFocusInPrev);
  // core/keys.js listens: the keys strip names the live page and its scope.
  document.dispatchEvent(new CustomEvent("arb:navigate", { detail: { id: entry.page.id } }));
  schedule(entry.page.id);
}

function sameParams(a, b) {
  const ak = Object.keys(a || {});
  const bk = Object.keys(b || {});
  if (ak.length !== bk.length) return false;
  return ak.every((k) => a[k] === b[k]);
}

/** Go to a path. opts = {replace: false}. An unknown path lands on the
    monitor and rewrites the URL to "/". */
export function navigate(path, opts) {
  const replace = !!(opts && opts.replace);
  const p = normalize(path);
  const hit = match(p);
  if (!hit) {
    if (p !== "/") navigate("/", { replace: true });
    return;
  }
  const url = p + location.search;
  if (replace || location.pathname !== p) {
    try {
      if (replace) history.replaceState({ page: hit.entry.page.id }, "", url);
      else history.pushState({ page: hit.entry.page.id }, "", url);
    } catch (err) { /* file:// or a sandboxed frame: route anyway */ }
  }
  show(hit.entry, hit.params);
}

function onPopState() {
  const p = normalize(location.pathname);
  const hit = match(p);
  if (!hit) {
    navigate("/", { replace: true });
    return;
  }
  show(hit.entry, hit.params);
}

/** Read the current URL, mount its page, bind popstate and the nav strip. */
export function start() {
  window.addEventListener("popstate", onPopState);
  document.addEventListener("click", (e) => {
    // Real anchors, so middle-click and copy-link keep working; only a plain
    // left click is intercepted.
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target && e.target.closest ? e.target.closest("a[data-page]") : null;
    if (!a) return;
    const href = a.getAttribute("href");
    if (!href || !href.startsWith("/")) return;
    e.preventDefault();
    if (e.detail > 0 && typeof a.blur === "function") a.blur(); // pointer click: let typing go to ARB>
    navigate(href);
  });
  const p = normalize(location.pathname);
  const hit = match(p);
  if (!hit) {
    navigate("/", { replace: true });
    return;
  }
  navigate(p, { replace: true });
}
