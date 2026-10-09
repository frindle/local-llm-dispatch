// fake-browser.ts -- shared, TESTED fakes for node:test fixtures whose target
// drives a browser page (Playwright page.evaluate / addInitScript, in-page
// fetch + XMLHttpRequest interceptors, document.querySelector...).
//
// Source of truth: ~/bin/dispatch-templates/fake-browser.ts, proven by
// fake-browser.test.ts next to it (`node --test fake-browser.test.ts`).
// ollama-dispatch-auto pastes this block into the authoring prompt for
// browser-ish targets: COPY IT VERBATIM into the fixture, do not re-invent it.
//
// RULES THAT MADE THE PROVEN HARNESSES WORK (read before editing a fixture)
//   * node:test mock.module(): ONE call per specifier for the whole file (a
//     second call throws ERR_INVALID_STATE "already mocked"). Mock at the TOP
//     and vary behaviour through MUTABLE state objects each test resets.
//   * CJS targets (module.exports = {...}): mock with { namedExports: {...} };
//     a bare { default: ... } silently mocks nothing.
//   * Import the target LAZILY and cache it (let _mod; async function load(){...})
//     -- tsx emits CJS, which rejects top-level await.
//   * Keep the fake globals installed until the LAST assertion of a test
//     (handlers the code registers fire later); call restore() in t.after().
//   * Build FRESH fakes per test (installFakeBrowser() returns a new FakeXHR
//     class each call) so one test's prototype patches never leak.
//   * Fakes return REAL shapes: fetch -> Response-like {ok,status,headers.get,
//     json(),text()}; querySelectorAll -> NodeList-like (length/item/forEach/
//     iterable, NO .map/.some); XHR fires load asynchronously.

export type Route = (url: string, init: { method: string; headers: Record<string, string>; body?: any })
  => { status?: number; body?: any; headers?: Record<string, string> } | undefined;

// Normalise every header container real code passes (Headers, Map, [k,v][], object)
// to a lower-cased plain object, so assertions never depend on the container type.
export function headersToObject(h: any): Record<string, string> {
  const out: Record<string, string> = {};
  if (!h) return out;
  const put = (k: any, v: any) => { out[String(k).toLowerCase()] = String(v); };
  if (typeof h.forEach === 'function' && !Array.isArray(h) && !(h instanceof Map)) {
    h.forEach((v: any, k: any) => put(k, v));            // Headers
  } else if (h instanceof Map || Array.isArray(h)) {
    for (const [k, v] of h as any) put(k, v);
  } else if (typeof h === 'object') {
    for (const k of Object.keys(h)) put(k, h[k]);
  }
  return out;
}

export function fakeResponse(status = 200, body: any = {}, headers: Record<string, string> = {}) {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  const hdr = headersToObject(headers);
  const res: any = {
    ok: status >= 200 && status < 300, status, url: '',
    headers: { get: (k: string) => hdr[String(k).toLowerCase()] ?? null },
    json: async () => JSON.parse(text), text: async () => text,
  };
  res.clone = () => fakeResponse(status, body, headers);
  return res;
}

export function nodeList<T>(items: T[]) {
  const nl: any = { length: items.length, item: (i: number) => items[i] ?? null,
    forEach: (fn: any) => items.forEach((x, i) => fn(x, i, nl)),
    [Symbol.iterator]: () => items[Symbol.iterator]() };
  items.forEach((x, i) => { nl[i] = x; });
  return nl;                                   // deliberately no .map/.some/.filter
}

// Install window/document/fetch/XMLHttpRequest on globalThis (window === globalThis,
// so `window.fetch` and bare `fetch` are the same function, as in a page).
export function installFakeBrowser(opts: { route?: Route; selectors?: Record<string, any[]> } = {}) {
  const route: Route = opts.route || (() => ({ status: 200, body: {} }));
  const selectors = opts.selectors || {};
  const g: any = globalThis;
  const saved: Record<string, any> = {};
  const KEYS = ['window', 'document', 'fetch', 'XMLHttpRequest'];
  for (const k of KEYS) saved[k] = Object.getOwnPropertyDescriptor(g, k);
  // window === globalThis, so page state the code stashes (window.__captured,
  // window.__installed...) lands on globalThis: remember what existed so restore()
  // can drop it -- otherwise one test's install guard silently disables the next.
  const preexisting = new Set(Reflect.ownKeys(g));
  const fetchCalls: any[] = [];
  const xhrCalls: any[] = [];
  const answer = (url: string, method: string, headers: any, body?: any) => {
    const r = route(url, { method, headers, body }) || {};
    return { status: r.status ?? 200, body: r.body ?? {}, headers: r.headers || {} };
  };
  const fakeFetch = async (input: any, init: any = {}) => {
    const url = typeof input === 'string' ? input : String(input?.url ?? input);
    const method = String(init.method || input?.method || 'GET').toUpperCase();
    const headers = headersToObject(init.headers ?? input?.headers);
    fetchCalls.push({ url, method, headers, body: init.body });
    const a = answer(url, method, headers, init.body);
    const res = fakeResponse(a.status, a.body, a.headers); res.url = url;
    return res;
  };
  class FakeXHR {
    readyState = 0; status = 0; responseText = ''; response: any = null;
    responseURL = ''; onload: any = null; onreadystatechange: any = null;
    _m = 'GET'; _u = ''; _h: Record<string, string> = {}; _l: Record<string, any[]> = {};
    open(method: string, url: string) { this._m = String(method).toUpperCase(); this._u = String(url); this.readyState = 1; }
    setRequestHeader(k: string, v: string) { this._h[String(k).toLowerCase()] = String(v); }
    getResponseHeader(_k: string) { return null; }
    getAllResponseHeaders() { return ''; }
    addEventListener(ev: string, fn: any) { (this._l[ev] = this._l[ev] || []).push(fn); }
    removeEventListener(ev: string, fn: any) { this._l[ev] = (this._l[ev] || []).filter((f) => f !== fn); }
    send(body?: any) {
      xhrCalls.push({ url: this._u, method: this._m, headers: { ...this._h }, body });
      const a = answer(this._u, this._m, { ...this._h }, body);
      queueMicrotask(() => {                     // real XHR completes asynchronously
        this.readyState = 4; this.status = a.status; this.responseURL = this._u;
        this.responseText = typeof a.body === 'string' ? a.body : JSON.stringify(a.body);
        this.response = this.responseText;
        for (const fn of [this.onreadystatechange, this.onload, ...(this._l.readystatechange || []), ...(this._l.load || [])]) {
          if (typeof fn === 'function') fn.call(this, { target: this });
        }
      });
    }
    abort() {}
  }
  const document = {
    querySelector: (sel: string) => (selectors[sel] || [])[0] ?? null,
    querySelectorAll: (sel: string) => nodeList(selectors[sel] || []),
    cookie: '',
  };
  const define = (k: string, v: any) => Object.defineProperty(g, k, { value: v, writable: true, configurable: true });
  define('fetch', fakeFetch);
  define('XMLHttpRequest', FakeXHR);
  define('document', document);
  define('window', g);
  const restore = () => {
    for (const k of KEYS) {
      if (saved[k]) Object.defineProperty(g, k, saved[k]); else delete g[k];
    }
    for (const k of Reflect.ownKeys(g)) {
      if (!preexisting.has(k) && !KEYS.includes(k as string)) {
        try { delete g[k as any]; } catch { /* non-configurable: leave it */ }
      }
    }
  };
  return { fetchCalls, xhrCalls, FakeXHR, document, window: g, restore };
}

// A Playwright-shaped page. evaluate(fn, arg) runs fn IN NODE against the fake
// globals (install them first); goto() records the call, updates url() and
// re-runs every init script, like a real navigation.
export function makeFakePage(opts: { url?: string; gotoThrowsAfter?: number } = {}) {
  let current = opts.url || 'about:blank';
  const gotoCalls: any[] = [];
  const initScripts: any[] = [];
  const runInit = async () => { for (const s of initScripts) await (typeof s === 'function' ? s() : undefined); };
  const page: any = {
    gotoCalls, initScripts,
    url: () => current,
    isClosed: () => false,
    goto: async (url: string, o?: any) => {
      gotoCalls.push([url, o].filter((x) => x !== undefined));
      if (opts.gotoThrowsAfter !== undefined && gotoCalls.length > opts.gotoThrowsAfter) throw new Error('navigation failed');
      current = url; await runInit(); return { ok: () => true, status: () => 200 };
    },
    evaluate: async (fn: any, arg?: any) => (typeof fn === 'function' ? fn(arg) : undefined),
    addInitScript: async (s: any) => { initScripts.push(s); },
    waitForLoadState: async () => {},
    waitForTimeout: async () => {},
    screenshot: async () => Buffer.alloc(0),
    content: async () => '<html></html>',
  };
  return page;
}

export function makeFakeContext(pages: any[] = []) {
  return {
    pages: () => pages,
    newPage: async () => { const p = makeFakePage(); pages.push(p); return p; },
    addInitScript: async (s: any) => { for (const p of pages) await p.addInitScript(s); },
    storageState: async () => ({ cookies: [], origins: [] }),
    cookies: async () => [],
  };
}
