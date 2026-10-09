// dispatch-env.ts -- SEALED, vetted environment fixtures for node:test harnesses.
//
// Source of truth: ~/bin/dispatch-templates/dispatch-env.ts, proven by
// dispatch-env.test.ts next to it. The scaffold drops a READ-ONLY copy at the
// worktree ROOT as `dispatch-env.ts`; auto-harness-check restores it byte-for-byte
// if it is edited or deleted, so do not edit it. IMPORT it, never paste or re-write it:
//
//   import { installFakeBrowser, makeFakePage, installChromeStub, installFetchStub,
//            openMemoryDb } from './dispatch-env.ts';
//
// WHY: a model-written fake DOM/page is a second program to debug (a smoke job burned
// 24 iterations on a fake whose querySelectorAll ignored comma selectors and whose page
// had no `.window`). This one has a REAL element tree and selector engine.
//
// CONTENTS (all pure, no network, no timers left running)
//   DOM / page   installFakeBrowser({html, url, route, selectors}) -> {window, document,
//                  fetchCalls, xhrCalls, FakeXHR, restore}; window === globalThis, so bare
//                  `window.__x`, `document`, `fetch`, `XMLHttpRequest`, `location`,
//                  `localStorage` all work for code run via page.evaluate(fn).
//                makeFakePage({url, html, window, document}) -> Playwright-shaped page:
//                  evaluate/$eval/$$eval/$/$$/goto/url/content/title/click/fill/type/
//                  waitForSelector/waitForFunction/waitForURL/waitForLoadState/
//                  waitForTimeout/waitForResponse/waitForNavigation/on/off/once/emit/
//                  frames/mainFrame/context/locator/screenshot/close, plus page.window and
//                  page.document. makeFakeContext(pages). loadHtml(html) -> detached Document.
//   chrome.*     installChromeStub({storage, tabs, manifest}) -> {chrome, restore, ...}
//                  storage.local/sync/session (+onChanged), runtime (sendMessage/onMessage/
//                  getURL/getManifest/lastError/onInstalled/onStartup), tabs, scripting,
//                  alarms, permissions, windows, action; every event has .emit().
//   fetch        installFetchStub(route) -> {calls, restore}; fakeResponse(); jsonResponse().
//   sqlite       openMemoryDb({sql, migrationsDir}) -> in-memory DB (exec/prepare().all/get/run).
//
// RULES THAT MADE THE PROVEN HARNESSES WORK
//   * node:test mock.module(): ONE call per specifier for the whole file; vary behaviour
//     through MUTABLE state the mock reads. CJS targets: { namedExports: {...} }.
//   * Install fakes in the test, call restore() in t.after(); keep them installed until
//     the LAST assertion (handlers the code registers fire later).
//   * Fakes return REAL shapes: querySelectorAll -> NodeList (no .map/.some/.filter; use
//     Array.from), fetch -> Response-like, XHR completes asynchronously.
//   * Unsupported selector syntax THROWS (loud) instead of silently matching nothing.

import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';

// ---------------------------------------------------------------------------
// shared shapes
// ---------------------------------------------------------------------------
export type Route = (url: string, init: { method: string; headers: Record<string, string>; body?: any })
  => { status?: number; body?: any; headers?: Record<string, string> } | undefined;

export function headersToObject(h: any): Record<string, string> {
  const out: Record<string, string> = {};
  if (!h) return out;
  const put = (k: any, v: any) => { out[String(k).toLowerCase()] = String(v); };
  if (typeof h.forEach === 'function' && !Array.isArray(h) && !(h instanceof Map)) {
    h.forEach((v: any, k: any) => put(k, v));
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
    ok: status >= 200 && status < 300, status, url: '', statusText: '',
    headers: { get: (k: string) => hdr[String(k).toLowerCase()] ?? null, has: (k: string) => String(k).toLowerCase() in hdr },
    json: async () => JSON.parse(text), text: async () => text,
  };
  res.clone = () => fakeResponse(status, body, headers);
  return res;
}

export const jsonResponse = (body: any, status = 200) =>
  fakeResponse(status, body, { 'content-type': 'application/json' });

// A NodeList: length/item/forEach/entries/keys/values/iterable and NOTHING else.
export function nodeList<T>(items: T[]) {
  const nl: any = {
    length: items.length, item: (i: number) => items[i] ?? null,
    forEach: (fn: any) => items.forEach((x, i) => fn(x, i, nl)),
    entries: () => items.entries(), keys: () => items.keys(), values: () => items.values(),
    [Symbol.iterator]: () => items[Symbol.iterator](),
  };
  items.forEach((x, i) => { nl[i] = x; });
  return nl;
}

// ---------------------------------------------------------------------------
// selector engine (comma lists, compound selectors, combinators, attribute ops)
// ---------------------------------------------------------------------------
type Simple =
  | { t: 'tag'; v: string } | { t: 'id'; v: string } | { t: 'class'; v: string }
  | { t: 'attr'; name: string; op: string; val: string; ci: boolean }
  | { t: 'pseudo'; name: string; arg?: string; list?: Compound[][] };
type Compound = { parts: Simple[]; comb: string };   // comb: how it relates to the PREVIOUS compound

const selCache = new Map<string, Compound[][]>();

function splitTopLevel(s: string, ch: string): string[] {
  const out: string[] = []; let depth = 0; let q = ''; let cur = '';
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (q) { cur += c; if (c === '\\') { cur += s[++i] ?? ''; } else if (c === q) q = ''; continue; }
    if (c === '"' || c === "'") { q = c; cur += c; continue; }
    if (c === '(' || c === '[') depth++;
    if (c === ')' || c === ']') depth--;
    if (c === ch && depth === 0) { out.push(cur); cur = ''; continue; }
    cur += c;
  }
  out.push(cur);
  return out;
}

const IDENT = /^(?:\\.|[\w -￿-])+/;
const unesc = (s: string) => s.replace(/\\(.)/g, '$1');

function parseCompoundList(sel: string): Compound[][] {
  const hit = selCache.get(sel);
  if (hit) return hit;
  const groups = splitTopLevel(sel, ',').map((g) => g.trim());
  if (!groups.length || groups.some((g) => !g)) {
    throw new SyntaxError(`dispatch-env: invalid selector ${JSON.stringify(sel)} (empty selector in list)`);
  }
  const parsed = groups.map((g) => parseComplex(g, sel));
  selCache.set(sel, parsed);
  return parsed;
}

function parseComplex(s: string, whole: string): Compound[] {
  const bad = (why: string) => new SyntaxError(`dispatch-env: unsupported/invalid selector ${JSON.stringify(whole)}: ${why}`);
  const out: Compound[] = [];
  let i = 0; let comb = ' ';
  const n = s.length;
  while (i < n) {
    // combinator
    let sawSpace = false;
    while (i < n && /\s/.test(s[i])) { i++; sawSpace = true; }
    if (i >= n) break;
    if ('>+~'.includes(s[i])) { comb = s[i]; i++; while (i < n && /\s/.test(s[i])) i++; }
    else if (sawSpace && out.length) comb = ' ';
    const parts: Simple[] = [];
    const start = i;
    while (i < n && !/[\s>+~]/.test(s[i])) {
      const c = s[i];
      if (c === '*') { i++; continue; }
      if (c === '#' || c === '.') {
        const m = IDENT.exec(s.slice(i + 1)); if (!m) throw bad(`bad ${c} at ${i}`);
        parts.push({ t: c === '#' ? 'id' : 'class', v: unesc(m[0]) }); i += 1 + m[0].length; continue;
      }
      if (c === '[') {
        let j = i + 1; let q = '';
        while (j < n && (q || s[j] !== ']')) { if (q) { if (s[j] === q) q = ''; } else if (s[j] === '"' || s[j] === "'") q = s[j]; j++; }
        if (j >= n) throw bad('unclosed [');
        const body = s.slice(i + 1, j).trim();
        const m = /^([\w:. -￿-]+)\s*(?:([~|^$*]?=)\s*("([^"]*)"|'([^']*)'|[^\s\]]+)(?:\s+([iIsS]))?)?$/.exec(body);
        if (!m) throw bad(`bad attribute selector [${body}]`);
        const val = m[4] ?? m[5] ?? m[3] ?? '';
        parts.push({ t: 'attr', name: m[1].toLowerCase(), op: m[2] || '', val, ci: (m[6] || '').toLowerCase() === 'i' });
        i = j + 1; continue;
      }
      if (c === ':') {
        const m = /^::?([\w-]+)/.exec(s.slice(i)); if (!m) throw bad(`bad pseudo at ${i}`);
        let name = m[1].toLowerCase(); i += m[0].length; let arg: string | undefined;
        if (s[i] === '(') {
          let d = 0; let j = i;
          for (; j < n; j++) { if (s[j] === '(') d++; else if (s[j] === ')') { d--; if (d === 0) break; } }
          if (j >= n) throw bad('unclosed (');
          arg = s.slice(i + 1, j).trim(); i = j + 1;
        }
        if (['not', 'is', 'where'].includes(name)) {
          if (arg === undefined) throw bad(`:${name} needs an argument`);
          parts.push({ t: 'pseudo', name: name === 'where' ? 'is' : name, list: parseCompoundList(arg) });
        } else if (['first-child', 'last-child', 'only-child', 'empty', 'checked', 'disabled', 'enabled', 'root',
                    'first-of-type', 'last-of-type', 'required', 'read-only'].includes(name)) {
          parts.push({ t: 'pseudo', name });
        } else if (['nth-child', 'nth-last-child', 'nth-of-type'].includes(name)) {
          if (arg === undefined) throw bad(`:${name} needs an argument`);
          parts.push({ t: 'pseudo', name, arg });
        } else {
          throw bad(`pseudo-class :${name} is not supported by dispatch-env (use a plain selector and filter in code)`);
        }
        continue;
      }
      const m = IDENT.exec(s.slice(i)); if (!m) throw bad(`unexpected ${JSON.stringify(c)} at ${i}`);
      parts.push({ t: 'tag', v: unesc(m[0]).toLowerCase() }); i += m[0].length;
    }
    if (i === start) throw bad(`unexpected ${JSON.stringify(s[i])}`);
    out.push({ parts, comb: out.length ? comb : ' ' });
    comb = ' ';
  }
  if (!out.length) throw bad('empty');
  return out;
}

function nthMatch(arg: string, idx1: number): boolean {
  const a = arg.replace(/\s+/g, '').toLowerCase();
  if (a === 'odd') return idx1 % 2 === 1;
  if (a === 'even') return idx1 % 2 === 0;
  const m = /^([+-]?\d*)n([+-]\d+)?$/.exec(a);
  if (m) {
    const A = m[1] === '' || m[1] === '+' ? 1 : m[1] === '-' ? -1 : parseInt(m[1], 10);
    const B = m[2] ? parseInt(m[2], 10) : 0;
    if (A === 0) return idx1 === B;
    const k = (idx1 - B) / A;
    return Number.isInteger(k) && k >= 0;
  }
  if (/^[+-]?\d+$/.test(a)) return idx1 === parseInt(a, 10);
  throw new SyntaxError(`dispatch-env: bad nth argument ${JSON.stringify(arg)}`);
}

function matchSimple(el: FakeElement, s: Simple): boolean {
  switch (s.t) {
    case 'tag': return el.localName === s.v;
    case 'id': return el.id === s.v;
    case 'class': return el.classList.contains(s.v);
    case 'attr': {
      if (!el.hasAttribute(s.name)) return false;
      if (!s.op) return true;
      let a = el.getAttribute(s.name) ?? ''; let v = s.val;
      if (s.ci) { a = a.toLowerCase(); v = v.toLowerCase(); }
      switch (s.op) {
        case '=': return a === v;
        case '~=': return v !== '' && !/\s/.test(v) && a.split(/\s+/).includes(v);
        case '|=': return a === v || a.startsWith(v + '-');
        case '^=': return v !== '' && a.startsWith(v);
        case '$=': return v !== '' && a.endsWith(v);
        case '*=': return v !== '' && a.includes(v);
      }
      return false;
    }
    case 'pseudo': {
      const sibs = el.parentElement ? el.parentElement.children : [el];
      const idx = sibs.indexOf(el);
      switch (s.name) {
        case 'not': return !matchList(el, s.list!);
        case 'is': return matchList(el, s.list!);
        case 'first-child': return idx === 0;
        case 'last-child': return idx === sibs.length - 1;
        case 'only-child': return sibs.length === 1;
        case 'empty': return el.childNodes.length === 0;
        case 'root': return el.localName === 'html';
        case 'checked': return el.hasAttribute('checked') || (el as any).checked === true;
        case 'disabled': return el.hasAttribute('disabled');
        case 'enabled': return !el.hasAttribute('disabled');
        case 'required': return el.hasAttribute('required');
        case 'read-only': return !['input', 'textarea', 'select'].includes(el.localName) || el.hasAttribute('readonly');
        case 'first-of-type': return sibs.filter((x: FakeElement) => x.localName === el.localName)[0] === el;
        case 'last-of-type': { const t = sibs.filter((x: FakeElement) => x.localName === el.localName); return t[t.length - 1] === el; }
        case 'nth-child': return nthMatch(s.arg!, idx + 1);
        case 'nth-last-child': return nthMatch(s.arg!, sibs.length - idx);
        case 'nth-of-type': { const t = sibs.filter((x: FakeElement) => x.localName === el.localName); return nthMatch(s.arg!, t.indexOf(el) + 1); }
      }
      return false;
    }
  }
}

function matchComplex(el: FakeElement, chain: Compound[], k: number): boolean {
  const c = chain[k];
  if (!c.parts.every((p) => matchSimple(el, p))) return false;
  if (k === 0) return true;
  switch (c.comb) {
    case '>': return !!el.parentElement && matchComplex(el.parentElement, chain, k - 1);
    case '+': { const p = el.previousElementSibling; return !!p && matchComplex(p, chain, k - 1); }
    case '~': { let p = el.previousElementSibling; while (p) { if (matchComplex(p, chain, k - 1)) return true; p = p.previousElementSibling; } return false; }
    default: { let p = el.parentElement; while (p) { if (matchComplex(p, chain, k - 1)) return true; p = p.parentElement; } return false; }
  }
}

function matchList(el: FakeElement, list: Compound[][]): boolean {
  return list.some((chain) => matchComplex(el, chain, chain.length - 1));
}

// ---------------------------------------------------------------------------
// DOM nodes
// ---------------------------------------------------------------------------
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);
const RAWTEXT = new Set(['script', 'style', 'textarea', 'title']);

class Listeners {
  private m: Record<string, Array<{ fn: any; once: boolean }>> = {};
  add(type: string, fn: any, opts?: any) {
    if (typeof fn === 'object' && fn && typeof fn.handleEvent === 'function') { const o = fn; fn = (e: any) => o.handleEvent(e); }
    if (typeof fn !== 'function') return;
    (this.m[type] = this.m[type] || []).push({ fn, once: !!(opts && typeof opts === 'object' && opts.once) });
  }
  remove(type: string, fn: any) { this.m[type] = (this.m[type] || []).filter((x) => x.fn !== fn); }
  fire(type: string, ev: any, self: any) {
    for (const l of [...(this.m[type] || [])]) {
      if (l.once) this.remove(type, l.fn);
      l.fn.call(self, ev);
    }
  }
}

export class FakeNode {
  parentNode: FakeNode | null = null;
  childNodes: FakeNode[] = [];
  ownerDocument: any = null;
  get nodeType(): number { return 1; }
  get parentElement(): FakeElement | null { const p = this.parentNode as any; return p && p.nodeType === 1 ? p : null; }
  get firstChild() { return this.childNodes[0] ?? null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] ?? null; }
  get nextSibling() { const s = this.parentNode?.childNodes; return s ? s[s.indexOf(this) + 1] ?? null : null; }
  get previousSibling() { const s = this.parentNode?.childNodes; return s ? s[s.indexOf(this) - 1] ?? null : null; }
  get textContent(): string { return this.childNodes.map((c) => c.textContent).join(''); }
  appendChild(n: any) { return this.insertBefore(n, null); }
  insertBefore(n: any, ref: any) {
    if (n.nodeType === 11) { for (const c of [...n.childNodes]) this.insertBefore(c, ref); return n; }
    n.parentNode?.removeChild(n);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(n); else this.childNodes.splice(i, 0, n);
    n.parentNode = this; n.ownerDocument = this.ownerDocument || this;
    return n;
  }
  removeChild(n: any) {
    const i = this.childNodes.indexOf(n);
    if (i < 0) throw new Error('dispatch-env: removeChild: not a child');
    this.childNodes.splice(i, 1); n.parentNode = null; return n;
  }
  remove() { this.parentNode?.removeChild(this); }
  contains(n: any): boolean { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
}

class FakeText extends FakeNode {
  data: string;
  constructor(data: string) { super(); this.data = data; }
  get nodeType() { return 3; }
  get textContent() { return this.data; }
  set textContent(v: string) { this.data = String(v); }
  get nodeValue() { return this.data; }
}

class FakeClassList {
  el: FakeElement;
  constructor(el: FakeElement) { this.el = el; }
  private list() { return (this.el.getAttribute('class') || '').split(/\s+/).filter(Boolean); }
  private set(l: string[]) { this.el.setAttribute('class', l.join(' ')); }
  contains(c: string) { return this.list().includes(c); }
  add(...cs: string[]) { const l = this.list(); for (const c of cs) if (!l.includes(c)) l.push(c); this.set(l); }
  remove(...cs: string[]) { this.set(this.list().filter((c) => !cs.includes(c))); }
  toggle(c: string, force?: boolean) { const has = this.contains(c); const want = force === undefined ? !has : force; if (want) this.add(c); else this.remove(c); return want; }
  get length() { return this.list().length; }
  item(i: number) { return this.list()[i] ?? null; }
  toString() { return this.list().join(' '); }
  [Symbol.iterator]() { return this.list()[Symbol.iterator](); }
}

export class FakeEvent {
  type: string; bubbles: boolean; cancelable: boolean; defaultPrevented = false; target: any = null; currentTarget: any = null;
  detail: any; [k: string]: any;
  _stop = false;
  constructor(type: string, init: any = {}) { this.type = type; this.bubbles = init.bubbles !== false; this.cancelable = !!init.cancelable; Object.assign(this, init); }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stop = true; }
  stopImmediatePropagation() { this._stop = true; }
}

export class FakeElement extends FakeNode {
  localName: string; namespaceURI = 'http://www.w3.org/1999/xhtml';
  attrs = new Map<string, string>();
  _ls = new Listeners();
  _value = ''; _checked: boolean | null = null;
  style: Record<string, any> = {};
  scrollTop = 0; scrollLeft = 0;
  constructor(tag: string, doc?: any) { super(); this.localName = tag.toLowerCase(); this.ownerDocument = doc || null; }
  get nodeType() { return 1; }
  get tagName() { return this.localName.toUpperCase(); }
  get nodeName() { return this.tagName; }
  get id() { return this.getAttribute('id') ?? ''; }
  set id(v: string) { this.setAttribute('id', v); }
  get className() { return this.getAttribute('class') ?? ''; }
  set className(v: string) { this.setAttribute('class', v); }
  get classList() { return new FakeClassList(this); }
  get children(): FakeElement[] { return this.childNodes.filter((c) => c.nodeType === 1) as FakeElement[]; }
  get childElementCount() { return this.children.length; }
  get firstElementChild() { return this.children[0] ?? null; }
  get lastElementChild() { const c = this.children; return c[c.length - 1] ?? null; }
  get previousElementSibling(): FakeElement | null { const s = this.parentElement ? this.parentElement.children : [this]; return s[s.indexOf(this) - 1] ?? null; }
  get nextElementSibling(): FakeElement | null { const s = this.parentElement ? this.parentElement.children : [this]; return s[s.indexOf(this) + 1] ?? null; }
  get dataset() {
    const el = this;
    return new Proxy({} as Record<string, string>, {
      get: (_t, k: string) => el.getAttribute('data-' + String(k).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase())) ?? undefined,
      set: (_t, k: string, v) => { el.setAttribute('data-' + String(k).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase()), String(v)); return true; },
      has: (_t, k: string) => el.hasAttribute('data-' + String(k).replace(/[A-Z]/g, (m) => '-' + m.toLowerCase())),
      ownKeys: () => [...el.attrs.keys()].filter((k) => k.startsWith('data-')).map((k) => k.slice(5).replace(/-([a-z])/g, (_m, c) => c.toUpperCase())),
      getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }),
    });
  }
  getAttribute(n: string) { const v = this.attrs.get(String(n).toLowerCase()); return v === undefined ? null : v; }
  setAttribute(n: string, v: any) { this.attrs.set(String(n).toLowerCase(), String(v)); }
  removeAttribute(n: string) { this.attrs.delete(String(n).toLowerCase()); }
  hasAttribute(n: string) { return this.attrs.has(String(n).toLowerCase()); }
  getAttributeNames() { return [...this.attrs.keys()]; }
  get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
  get value(): string {
    if (this.localName === 'option') return this.getAttribute('value') ?? this.textContent;
    if (this.localName === 'select') {
      const opts = this._qsa('option'); const sel = opts.find((o) => o.hasAttribute('selected')) ?? opts[0];
      return this._value !== '' ? this._value : sel ? sel.value : '';
    }
    return this._value;
  }
  set value(v: string) { this._value = String(v); }
  get href() { return this.getAttribute('href') ?? ''; }
  set href(v: string) { this.setAttribute('href', v); }
  get src() { return this.getAttribute('src') ?? ''; }
  get name() { return this.getAttribute('name') ?? ''; }
  get type() { return this.getAttribute('type') ?? (this.localName === 'input' ? 'text' : ''); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(v: boolean) { if (v) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }
  get checked() { return this._checked ?? this.hasAttribute('checked'); }
  set checked(v: boolean) { this._checked = !!v; }
  get hidden() { return this.hasAttribute('hidden'); }
  get textContent(): string { return super.textContent; }
  set textContent(v: string) { this.childNodes.forEach((c) => (c.parentNode = null)); this.childNodes = []; if (v !== '' && v != null) this.appendChild(new FakeText(String(v))); }
  get innerText() { return this.textContent; }
  set innerText(v: string) { this.textContent = v; }
  get innerHTML(): string { return this.childNodes.map((c) => serialize(c)).join(''); }
  set innerHTML(html: string) {
    this.childNodes.forEach((c) => (c.parentNode = null)); this.childNodes = [];
    parseInto(this, String(html), this.ownerDocument);
  }
  get outerHTML() { return serialize(this); }
  get options() { return this.querySelectorAll('option'); }
  get selectedOptions() { return nodeList(this.querySelectorAll('option').length ? Array.from(this.querySelectorAll('option')).filter((o: any) => o.hasAttribute('selected')) : []); }
  matches(sel: string) { return matchList(this, parseCompoundList(sel)); }
  closest(sel: string) { const l = parseCompoundList(sel); for (let e: FakeElement | null = this; e; e = e.parentElement) if (matchList(e, l)) return e; return null; }
  querySelectorAll(sel: string) { return nodeList(this._qsa(sel)); }
  querySelector(sel: string) { return this._qsa(sel, 1)[0] ?? null; }
  getElementsByTagName(t: string) { const w = t.toLowerCase(); return nodeList(this._qsa('*').filter((e) => w === '*' || e.localName === w)); }
  getElementsByClassName(c: string) { const cs = c.split(/\s+/).filter(Boolean); return nodeList(this._qsa('*').filter((e) => cs.every((x) => e.classList.contains(x)))); }
  _qsa(sel: string, limit = Infinity): FakeElement[] {
    const list = parseCompoundList(sel); const out: FakeElement[] = [];
    const walk = (n: FakeNode): boolean => {
      for (const c of n.childNodes) {
        if (c.nodeType !== 1) continue;
        const e = c as FakeElement;
        if (matchList(e, list)) { out.push(e); if (out.length >= limit) return true; }
        if (walk(e)) return true;
      }
      return false;
    };
    walk(this);
    return out;
  }
  addEventListener(t: string, fn: any, o?: any) { this._ls.add(t, fn, o); }
  removeEventListener(t: string, fn: any) { this._ls.remove(t, fn); }
  dispatchEvent(ev: any) {
    if (!ev.target) ev.target = this;
    for (let n: any = this; n; n = n.parentNode) {
      if (!n._ls) continue;
      ev.currentTarget = n; n._ls.fire(ev.type, ev, n);
      if (ev._stop || !ev.bubbles) break;
    }
    const w = (globalThis as any).window; if (ev.type === 'click' && this.localName === 'a' && !ev.defaultPrevented && w?.__fakeNavigate) w.__fakeNavigate(this.getAttribute('href'));
    return !ev.defaultPrevented;
  }
  click() { this.dispatchEvent(new FakeEvent('click', { bubbles: true, cancelable: true })); }
  focus() { const d: any = this.ownerDocument; if (d) d.activeElement = this; this.dispatchEvent(new FakeEvent('focus', { bubbles: false })); }
  blur() { this.dispatchEvent(new FakeEvent('blur', { bubbles: false })); }
  submit() { this.dispatchEvent(new FakeEvent('submit', { bubbles: true, cancelable: true })); }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  cloneNode(deep = false): FakeElement {
    const c = new FakeElement(this.localName, this.ownerDocument);
    for (const [k, v] of this.attrs) c.attrs.set(k, v);
    c._value = this._value;
    if (deep) for (const k of this.childNodes) c.appendChild(k.nodeType === 3 ? new FakeText((k as FakeText).data) : (k as FakeElement).cloneNode(true));
    return c;
  }
  append(...ns: any[]) { for (const n of ns) this.appendChild(typeof n === 'string' ? new FakeText(n) : n); }
  prepend(...ns: any[]) { for (const n of [...ns].reverse()) this.insertBefore(typeof n === 'string' ? new FakeText(n) : n, this.firstChild); }
  replaceChildren(...ns: any[]) { this.textContent = ''; this.append(...ns); }
  after(...ns: any[]) { const p = this.parentNode; if (!p) return; const ref = this.nextSibling; for (const n of ns) p.insertBefore(typeof n === 'string' ? new FakeText(n) : n, ref); }
  before(...ns: any[]) { const p = this.parentNode; if (!p) return; for (const n of ns) p.insertBefore(typeof n === 'string' ? new FakeText(n) : n, this); }
  get offsetParent() { return this.parentElement; }
  get offsetWidth() { return 1; } get offsetHeight() { return 1; }
}

function esc(s: string, attr = false) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(attr ? /"/g : /$^/g, '&quot;');
}
function serialize(n: any): string {
  if (n.nodeType === 3) return esc(n.data);
  const attrs = [...n.attrs].map(([k, v]: [string, string]) => ` ${k}="${esc(v, true)}"`).join('');
  if (VOID.has(n.localName)) return `<${n.localName}${attrs}>`;
  const inner = n.childNodes.map((c: any) => (RAWTEXT.has(n.localName) && c.nodeType === 3 ? c.data : serialize(c))).join('');
  return `<${n.localName}${attrs}>${inner}</${n.localName}>`;
}

const ENT: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
const decode = (s: string) => s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
  if (e[0] === '#') { const c = e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10); return Number.isFinite(c) ? String.fromCodePoint(c) : m; }
  return ENT[e.toLowerCase()] ?? m;
});

// Small forgiving HTML parser: tags, quoted/unquoted/boolean attributes, void elements,
// raw-text elements, comments/doctype skipped, unmatched end tags ignored.
function parseInto(root: FakeNode, html: string, doc: any) {
  let cur: FakeNode = root; let i = 0; const n = html.length;
  const text = (s: string) => { if (s) cur.appendChild(new FakeText(decode(s))); };
  while (i < n) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { text(html.slice(i)); break; }
    if (lt > i) text(html.slice(i, lt));
    i = lt;
    if (html.startsWith('<!--', i)) { const e = html.indexOf('-->', i + 4); i = e < 0 ? n : e + 3; continue; }
    if (html[i + 1] === '!' || html[i + 1] === '?') { const e = html.indexOf('>', i); i = e < 0 ? n : e + 1; continue; }
    if (html[i + 1] === '/') {
      const e = html.indexOf('>', i); const name = html.slice(i + 2, e < 0 ? n : e).trim().toLowerCase(); i = e < 0 ? n : e + 1;
      for (let p: any = cur; p && p !== root; p = p.parentNode) if (p.localName === name) { cur = p.parentNode!; break; }
      continue;
    }
    const m = /^<([a-zA-Z][\w:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/.exec(html.slice(i));
    if (!m) { text('<'); i++; continue; }
    const el = new FakeElement(m[1], doc);
    const am = m[2].replace(/\/\s*$/, '');
    const re = /([^\s"'<>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g; let a;
    while ((a = re.exec(am))) el.setAttribute(a[1], decode(a[2] ?? a[3] ?? a[4] ?? ''));
    if (el.localName === 'input' || el.localName === 'textarea') el._value = el.getAttribute('value') ?? '';
    cur.appendChild(el); i += m[0].length;
    if (RAWTEXT.has(el.localName)) {
      const close = html.toLowerCase().indexOf('</' + el.localName, i);
      const end = close < 0 ? n : close;
      if (end > i) el.appendChild(new FakeText(el.localName === 'script' || el.localName === 'style' ? html.slice(i, end) : decode(html.slice(i, end))));
      const gt = html.indexOf('>', end); i = gt < 0 ? n : gt + 1;
      if (el.localName === 'textarea') el._value = el.textContent;
      continue;
    }
    if (!VOID.has(el.localName) && !/\/\s*$/.test(m[2])) cur = el;
  }
}

export class FakeDocument extends FakeNode {
  documentElement!: FakeElement; head!: FakeElement; body!: FakeElement;
  cookie = ''; title = ''; readyState = 'complete'; activeElement: any = null;
  _ls = new Listeners(); defaultView: any = null; URL = 'about:blank'; referrer = '';
  constructor(html = '') {
    super(); this.ownerDocument = this;
    const root = new FakeElement('html', this);
    this.appendChild(root); this.documentElement = root;
    this.head = new FakeElement('head', this); this.body = new FakeElement('body', this);
    root.appendChild(this.head); root.appendChild(this.body);
    if (html) this.write(html);
  }
  get nodeType() { return 9; }
  get textContent() { return this.documentElement.textContent; }
  // Replace the whole document with `html` (a full page or just body content).
  write(html: string) {
    const tmp = new FakeElement('div', this); parseInto(tmp, html, this);
    const find = (n: FakeNode, t: string): FakeElement | null => { for (const c of n.childNodes) { if ((c as any).localName === t) return c as FakeElement; const r = find(c, t); if (r) return r; } return null; };
    const h = find(tmp, 'html'); const b = find(tmp, 'body'); const hd = find(tmp, 'head');
    this.head.childNodes.forEach((c) => (c.parentNode = null)); this.head.childNodes = [];
    this.body.childNodes.forEach((c) => (c.parentNode = null)); this.body.childNodes = [];
    if (h && b) { for (const [k, v] of h.attrs) this.documentElement.setAttribute(k, v); for (const k of [...b.childNodes]) this.body.appendChild(k); for (const [k, v] of b.attrs) this.body.setAttribute(k, v); if (hd) for (const k of [...hd.childNodes]) this.head.appendChild(k); }
    else for (const k of [...tmp.childNodes]) this.body.appendChild(k);
    const t = this.head.querySelector('title'); if (t) this.title = t.textContent;
  }
  querySelectorAll(sel: string) { return this.documentElement.matches(sel) ? nodeList([this.documentElement, ...this.documentElement._qsa(sel)]) : this.documentElement.querySelectorAll(sel); }
  querySelector(sel: string) { return this.documentElement.matches(sel) ? this.documentElement : this.documentElement.querySelector(sel); }
  getElementById(id: string) { return this.documentElement._qsa('*').find((e) => e.id === id) ?? null; }
  getElementsByTagName(t: string) { return this.documentElement.getElementsByTagName(t); }
  getElementsByClassName(c: string) { return this.documentElement.getElementsByClassName(c); }
  getElementsByName(n: string) { return nodeList(this.documentElement._qsa('*').filter((e) => e.getAttribute('name') === n)); }
  createElement(t: string) { return new FakeElement(t, this); }
  createTextNode(s: string) { const t = new FakeText(String(s)); t.ownerDocument = this; return t; }
  createDocumentFragment() { const f: any = new FakeElement('#fragment', this); Object.defineProperty(f, 'nodeType', { value: 11 }); return f; }
  createEvent(_t: string) { return { initEvent(this: any, type: string, bubbles = true, cancelable = true) { Object.assign(this, new FakeEvent(type, { bubbles, cancelable })); } }; }
  addEventListener(t: string, fn: any, o?: any) { this._ls.add(t, fn, o); }
  removeEventListener(t: string, fn: any) { this._ls.remove(t, fn); }
  dispatchEvent(ev: any) { ev.target = ev.target || this; ev.currentTarget = this; this._ls.fire(ev.type, ev, this); return !ev.defaultPrevented; }
  hasFocus() { return true; }
  get location() { return (globalThis as any).location; }
  get all() { return nodeList(this.documentElement._qsa('*')); }
  get forms() { return this.querySelectorAll('form'); }
  get links() { return this.querySelectorAll('a[href], area[href]'); }
  get images() { return this.querySelectorAll('img'); }
  get scripts() { return this.querySelectorAll('script'); }
  get visibilityState() { return 'visible'; } get hidden() { return false; }
}

// Detached document from an HTML string (no globals installed).
export function loadHtml(html: string) { return new FakeDocument(html); }

// ---------------------------------------------------------------------------
// window + fetch + XHR (installed on globalThis; window === globalThis)
// ---------------------------------------------------------------------------
function makeStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => (m.has(String(k)) ? m.get(String(k))! : null), setItem: (k: string, v: any) => { m.set(String(k), String(v)); },
    removeItem: (k: string) => { m.delete(String(k)); }, clear: () => m.clear(), key: (i: number) => [...m.keys()][i] ?? null,
    get length() { return m.size; },
  };
}

function makeLocation(initial: string, onNavigate: (u: string) => void) {
  let u = new URL(initial);
  const loc: any = {
    get href() { return u.href; }, set href(v: string) { loc.assign(v); },
    get origin() { return u.origin; }, get protocol() { return u.protocol; }, get host() { return u.host; },
    get hostname() { return u.hostname; }, get port() { return u.port; }, get pathname() { return u.pathname; },
    get search() { return u.search; }, get hash() { return u.hash; },
    assign: (v: string) => { u = new URL(String(v), u); onNavigate(u.href); }, replace: (v: string) => { u = new URL(String(v), u); onNavigate(u.href); },
    reload: () => {}, toString: () => u.href, _set: (v: string) => { u = new URL(String(v), u); },
  };
  return loc;
}

export function installFakeBrowser(opts: { route?: Route; selectors?: Record<string, any[]>; html?: string; url?: string; cookie?: string } = {}) {
  const route: Route = opts.route || (() => ({ status: 200, body: {} }));
  const overrides = opts.selectors || {};
  const g: any = globalThis;
  const saved: Record<string, any> = {};
  const KEYS = ['window', 'document', 'fetch', 'XMLHttpRequest', 'location', 'navigator', 'localStorage', 'sessionStorage',
    'Element', 'HTMLElement', 'Node', 'Document', 'MutationObserver', 'getComputedStyle', 'addEventListener',
    'removeEventListener', 'dispatchEvent', 'CustomEvent', 'Event', 'self', 'top', 'parent', 'history', 'innerWidth', 'innerHeight', 'scrollTo'];
  for (const k of KEYS) saved[k] = Object.getOwnPropertyDescriptor(g, k);
  const preexisting = new Set(Reflect.ownKeys(g));
  const fetchCalls: any[] = [];
  const xhrCalls: any[] = [];
  const navigations: string[] = [];
  const doc = new FakeDocument(opts.html || '');
  if (opts.cookie) doc.cookie = opts.cookie;
  const location = makeLocation(opts.url || 'https://example.test/', (u) => { navigations.push(u); doc.URL = u; });
  doc.URL = location.href;
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
    readyState = 0; status = 0; statusText = ''; responseText = ''; response: any = null;
    responseURL = ''; responseType = ''; timeout = 0; withCredentials = false; onload: any = null; onerror: any = null;
    onreadystatechange: any = null;
    _m = 'GET'; _u = ''; _h: Record<string, string> = {}; _rh: Record<string, string> = {}; _l: Record<string, any[]> = {};
    open(method: string, url: string) { this._m = String(method).toUpperCase(); this._u = String(url); this.readyState = 1; }
    setRequestHeader(k: string, v: string) { this._h[String(k).toLowerCase()] = String(v); }
    getResponseHeader(k: string) { return this._rh[String(k).toLowerCase()] ?? null; }
    getAllResponseHeaders() { return Object.entries(this._rh).map(([k, v]) => `${k}: ${v}\r\n`).join(''); }
    overrideMimeType() {}
    addEventListener(ev: string, fn: any) { (this._l[ev] = this._l[ev] || []).push(fn); }
    removeEventListener(ev: string, fn: any) { this._l[ev] = (this._l[ev] || []).filter((f) => f !== fn); }
    send(body?: any) {
      xhrCalls.push({ url: this._u, method: this._m, headers: { ...this._h }, body });
      const a = answer(this._u, this._m, { ...this._h }, body);
      queueMicrotask(() => {
        this.readyState = 4; this.status = a.status; this.responseURL = this._u; this._rh = headersToObject(a.headers);
        this.responseText = typeof a.body === 'string' ? a.body : JSON.stringify(a.body);
        this.response = this.responseType === 'json' ? (typeof a.body === 'string' ? JSON.parse(a.body) : a.body) : this.responseText;
        for (const fn of [this.onreadystatechange, this.onload, ...(this._l.readystatechange || []), ...(this._l.load || []), ...(this._l.loadend || [])]) {
          if (typeof fn === 'function') fn.call(this, { target: this, type: 'load' });
        }
      });
    }
    abort() {}
  }
  const wl = new Listeners();
  const define = (k: string, v: any) => Object.defineProperty(g, k, { value: v, writable: true, configurable: true });
  define('fetch', fakeFetch); define('XMLHttpRequest', FakeXHR);
  define('document', doc); define('window', g); define('self', g); define('top', g); define('parent', g);
  define('location', location); define('navigator', { userAgent: 'dispatch-env/1.0', language: 'en-US', languages: ['en-US'], platform: 'test', webdriver: false });
  define('localStorage', makeStorage()); define('sessionStorage', makeStorage());
  define('Element', FakeElement); define('HTMLElement', FakeElement); define('Node', FakeNode); define('Document', FakeDocument);
  define('Event', FakeEvent); define('CustomEvent', FakeEvent);
  define('MutationObserver', class { observe() {} disconnect() {} takeRecords() { return []; } });
  define('getComputedStyle', (el: any) => ({ ...(el?.style || {}), getPropertyValue: (p: string) => el?.style?.[p] ?? '', display: el?.style?.display ?? 'block', visibility: el?.style?.visibility ?? 'visible' }));
  define('addEventListener', (t: string, fn: any, o?: any) => wl.add(t, fn, o));
  define('removeEventListener', (t: string, fn: any) => wl.remove(t, fn));
  define('dispatchEvent', (ev: any) => { ev.target = g; wl.fire(ev.type, ev, g); return !ev.defaultPrevented; });
  define('history', { pushState: (_s: any, _t: string, u?: string) => { if (u) location._set(u); }, replaceState: (_s: any, _t: string, u?: string) => { if (u) location._set(u); }, back() {}, forward() {} });
  define('innerWidth', 1280); define('innerHeight', 800); define('scrollTo', () => {});
  define('__fakeNavigate', (h: string | null) => { if (h) location.assign(h); });
  doc.defaultView = g;
  // `selectors` override: an exact-selector map (legacy API) wins over the real tree.
  if (Object.keys(overrides).length) {
    const qsa = doc.querySelectorAll.bind(doc); const qs = doc.querySelector.bind(doc);
    doc.querySelectorAll = (s: string) => (s in overrides ? nodeList(overrides[s]) : qsa(s));
    doc.querySelector = (s: string) => (s in overrides ? overrides[s][0] ?? null : qs(s));
  }
  const restore = () => {
    for (const k of KEYS) { if (saved[k]) Object.defineProperty(g, k, saved[k]); else delete g[k]; }
    for (const k of Reflect.ownKeys(g)) {
      if (!preexisting.has(k) && !KEYS.includes(k as string)) { try { delete g[k as any]; } catch { /* non-configurable */ } }
    }
    delete g.__fakeNavigate;
  };
  return { fetchCalls, xhrCalls, navigations, FakeXHR, document: doc, window: g, location, restore };
}

// ---------------------------------------------------------------------------
// Playwright-shaped page / context
// ---------------------------------------------------------------------------
export function makeFakePage(opts: { url?: string; html?: string; gotoThrowsAfter?: number; window?: any; document?: any;
  gotoHtml?: Record<string, string> } = {}) {
  let current = opts.url || 'about:blank';
  const gotoCalls: any[] = [];
  const initScripts: any[] = [];
  const handlers: Record<string, Array<{ fn: any; once: boolean }>> = {};
  let closed = false;
  const g: any = globalThis;
  const getDoc = () => opts.document || g.document;
  const getWin = () => opts.window || g.window;
  const runInit = async () => {
    for (const s of initScripts) {
      if (typeof s === 'function') await s();
      else if (typeof s === 'string') (0, eval)(s);                       // a script SOURCE string, run against the fake window
      else if (s && typeof s.content === 'string') (0, eval)(s.content);
    }
  };
  const sel = (s: string) => { const d = getDoc(); if (!d) throw new Error('dispatch-env: no document installed (call installFakeBrowser() first)'); return d; };
  const timeoutErr = (what: string) => Object.assign(new Error(`Timeout exceeded while waiting for ${what}`), { name: 'TimeoutError' });
  const page: any = {
    gotoCalls, initScripts, mouse: { click: async () => {}, move: async () => {}, wheel: async () => {} },
    keyboard: { press: async () => {}, type: async () => {}, down: async () => {}, up: async () => {} },
    get window() { return getWin(); }, get document() { return getDoc(); },
    url: () => current,
    setUrl(u: string) { current = u; const loc = getWin()?.location; if (loc && loc._set) loc._set(u); },
    isClosed: () => closed, close: async () => { closed = true; emit('close'); },
    bringToFront: async () => {}, setViewportSize: async () => {}, viewportSize: () => ({ width: 1280, height: 800 }),
    setDefaultTimeout() {}, setDefaultNavigationTimeout() {}, setExtraHTTPHeaders: async () => {},
    context: () => (page._context || null),
    goto: async (url: string, o?: any) => {
      gotoCalls.push([url, o].filter((x) => x !== undefined));
      if (opts.gotoThrowsAfter !== undefined && gotoCalls.length > opts.gotoThrowsAfter) throw new Error('navigation failed');
      current = url; const loc = getWin()?.location; if (loc && loc._set) loc._set(url);
      const h = opts.gotoHtml && (opts.gotoHtml[url] ?? opts.gotoHtml['*']); if (h !== undefined && getDoc()?.write) getDoc().write(h);
      await runInit(); emit('framenavigated'); emit('load'); emit('domcontentloaded');
      return { ok: () => true, status: () => 200, url: () => url };
    },
    reload: async () => { await runInit(); return null; }, goBack: async () => null, goForward: async () => null,
    evaluate: async (fn: any, arg?: any) => (typeof fn === 'function' ? fn(arg) : (0, eval)(String(fn))),
    evaluateHandle: async (fn: any, arg?: any) => ({ jsonValue: async () => fn(arg), asElement: () => null, dispose: async () => {} }),
    $eval: async (s: string, fn: any, arg?: any) => { const e = sel(s).querySelector(s); if (!e) throw new Error(`Error: failed to find element matching selector "${s}"`); return fn(e, arg); },
    $$eval: async (s: string, fn: any, arg?: any) => fn(Array.from(sel(s).querySelectorAll(s)), arg),
    $: async (s: string) => sel(s).querySelector(s), $$: async (s: string) => Array.from(sel(s).querySelectorAll(s)),
    addInitScript: async (s: any) => { initScripts.push(s); },
    exposeFunction: async (name: string, fn: any) => { (getWin() as any)[name] = fn; },
    waitForLoadState: async () => {}, waitForTimeout: async () => {},
    waitForSelector: async (s: string, o: any = {}) => {
      const e = sel(s).querySelector(s);
      if (o.state === 'detached' || o.state === 'hidden') { if (!e) return null; throw timeoutErr(`selector "${s}" to be ${o.state}`); }
      if (e) return e; throw timeoutErr(`selector "${s}"`);
    },
    waitForFunction: async (fn: any, arg?: any, o?: any) => { const v = typeof fn === 'function' ? await fn(arg) : (0, eval)(String(fn)); if (v) return { jsonValue: async () => v }; throw timeoutErr('function'); },
    waitForURL: async (u: any) => { const ok = typeof u === 'string' ? (u.includes('*') ? new RegExp('^' + u.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*\*?/g, '.*') + '$').test(current) : current === u) : u instanceof RegExp ? u.test(current) : typeof u === 'function' ? !!u(new URL(current, 'https://x.test')) : false; if (!ok) throw timeoutErr(`URL ${String(u)}`); },
    waitForNavigation: async () => null, waitForResponse: async () => { throw timeoutErr('response'); }, waitForRequest: async () => { throw timeoutErr('request'); },
    waitForEvent: async (ev: string) => new Promise((res) => (handlers[ev] = (handlers[ev] || []).concat({ fn: res, once: true }))),
    click: async (s: string) => { const e = sel(s).querySelector(s); if (!e) throw timeoutErr(`selector "${s}"`); e.click(); },
    dblclick: async (s: string) => { const e = sel(s).querySelector(s); if (!e) throw timeoutErr(`selector "${s}"`); e.click(); e.click(); },
    fill: async (s: string, v: string) => { const e = sel(s).querySelector(s); if (!e) throw timeoutErr(`selector "${s}"`); e.value = String(v); e.dispatchEvent(new FakeEvent('input')); e.dispatchEvent(new FakeEvent('change')); },
    type: async (s: string, v: string) => { const e = sel(s).querySelector(s); if (!e) throw timeoutErr(`selector "${s}"`); e.value = (e.value || '') + String(v); e.dispatchEvent(new FakeEvent('input')); },
    press: async () => {}, check: async (s: string) => { const e = sel(s).querySelector(s); if (e) e.checked = true; }, hover: async () => {},
    selectOption: async (s: string, v: any) => { const e = sel(s).querySelector(s); if (e) e.value = Array.isArray(v) ? v[0] : v; return [].concat(v); },
    textContent: async (s: string) => sel(s).querySelector(s)?.textContent ?? null,
    innerText: async (s: string) => sel(s).querySelector(s)?.textContent ?? '',
    getAttribute: async (s: string, n: string) => sel(s).querySelector(s)?.getAttribute(n) ?? null,
    isVisible: async (s: string) => !!sel(s).querySelector(s), isHidden: async (s: string) => !sel(s).querySelector(s),
    inputValue: async (s: string) => sel(s).querySelector(s)?.value ?? '',
    locator: (s: string) => {
      const all = () => Array.from(sel(s).querySelectorAll(s)) as any[];
      const loc: any = {
        count: async () => all().length, first: () => loc, last: () => loc, nth: () => loc,
        click: async () => { const e = all()[0]; if (!e) throw timeoutErr(`locator "${s}"`); e.click(); },
        fill: async (v: string) => { const e = all()[0]; if (!e) throw timeoutErr(`locator "${s}"`); e.value = String(v); },
        textContent: async () => all()[0]?.textContent ?? null, innerText: async () => all()[0]?.textContent ?? '',
        isVisible: async () => all().length > 0, waitFor: async () => { if (!all().length) throw timeoutErr(`locator "${s}"`); },
        getAttribute: async (n: string) => all()[0]?.getAttribute(n) ?? null, allTextContents: async () => all().map((e) => e.textContent),
        evaluate: async (fn: any, arg?: any) => fn(all()[0], arg), evaluateAll: async (fn: any, arg?: any) => fn(all(), arg),
      };
      return loc;
    },
    content: async () => { const d = getDoc(); return d?.documentElement ? '<!DOCTYPE html>' + d.documentElement.outerHTML : '<html></html>'; },
    title: async () => getDoc()?.title ?? '',
    screenshot: async () => Buffer.alloc(0), pdf: async () => Buffer.alloc(0),
    on: (ev: string, fn: any) => { (handlers[ev] = handlers[ev] || []).push({ fn, once: false }); return page; },
    once: (ev: string, fn: any) => { (handlers[ev] = handlers[ev] || []).push({ fn, once: true }); return page; },
    off: (ev: string, fn: any) => { handlers[ev] = (handlers[ev] || []).filter((h) => h.fn !== fn); return page; },
    removeListener: (ev: string, fn: any) => page.off(ev, fn),
    emit: (ev: string, ...a: any[]) => emit(ev, ...a),
    listenerCount: (ev: string) => (handlers[ev] || []).length,
    mainFrame: () => page, frames: () => [page], frame: () => null,
    name: () => '', parentFrame: () => null, childFrames: () => [],
  };
  function emit(ev: string, ...a: any[]) {
    for (const h of [...(handlers[ev] || [])]) { if (h.once) handlers[ev] = handlers[ev].filter((x) => x !== h); h.fn(...a); }
    return (handlers[ev] || []).length > 0;
  }
  if (opts.html !== undefined && getDoc()?.write) getDoc().write(opts.html);
  return page;
}

export function makeFakeContext(pages: any[] = []) {
  const ctx: any = {
    pages: () => pages,
    newPage: async () => { const p = makeFakePage(); p._context = ctx; pages.push(p); return p; },
    addInitScript: async (s: any) => { for (const p of pages) await p.addInitScript(s); },
    storageState: async () => ({ cookies: [], origins: [] }),
    cookies: async () => [], addCookies: async () => {}, clearCookies: async () => {},
    close: async () => {}, on() { return ctx; }, off() { return ctx; }, setDefaultTimeout() {},
    route: async () => {}, unroute: async () => {}, browser: () => null,
  };
  for (const p of pages) p._context = ctx;
  return ctx;
}

// ---------------------------------------------------------------------------
// standalone fetch stub (no DOM)
// ---------------------------------------------------------------------------
export function installFetchStub(route: Route = () => ({ status: 200, body: {} })) {
  const g: any = globalThis;
  const saved = Object.getOwnPropertyDescriptor(g, 'fetch');
  const calls: any[] = [];
  const stub = async (input: any, init: any = {}) => {
    const url = typeof input === 'string' ? input : String(input?.url ?? input);
    const method = String(init.method || input?.method || 'GET').toUpperCase();
    const headers = headersToObject(init.headers ?? input?.headers);
    calls.push({ url, method, headers, body: init.body });
    const r = route(url, { method, headers, body: init.body }) || {};
    const res = fakeResponse(r.status ?? 200, r.body ?? {}, r.headers || {}); res.url = url;
    return res;
  };
  Object.defineProperty(g, 'fetch', { value: stub, writable: true, configurable: true });
  const restore = () => { if (saved) Object.defineProperty(g, 'fetch', saved); else delete g.fetch; };
  return { calls, restore };
}

// ---------------------------------------------------------------------------
// chrome.* extension API stub
// ---------------------------------------------------------------------------
function ev() {
  const ls: any[] = [];
  return {
    addListener: (fn: any) => { ls.push(fn); }, removeListener: (fn: any) => { const i = ls.indexOf(fn); if (i >= 0) ls.splice(i, 1); },
    hasListener: (fn: any) => ls.includes(fn), hasListeners: () => ls.length > 0,
    emit: (...a: any[]) => ls.slice().map((f) => f(...a)), listeners: ls,
  };
}

function storageArea(initial: Record<string, any>, onChanged: any, area: string) {
  const data: Record<string, any> = { ...initial };
  const clone = (v: any) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
  const dual = (fn: () => any, cb?: any) => { let r: any; try { r = fn(); } catch (e) { if (cb) { setTimeout(() => cb(), 0); return undefined; } throw e; } if (typeof cb === 'function') { queueMicrotask(() => cb(r)); return undefined; } return Promise.resolve(r); };
  return {
    _data: data,
    get: (keys?: any, cb?: any) => {
      if (typeof keys === 'function') { cb = keys; keys = null; }
      return dual(() => {
        if (keys == null) return clone(data);
        if (typeof keys === 'string') keys = [keys];
        const out: Record<string, any> = {};
        if (Array.isArray(keys)) { for (const k of keys) if (k in data) out[k] = clone(data[k]); }
        else for (const k of Object.keys(keys)) out[k] = k in data ? clone(data[k]) : clone(keys[k]);
        return out;
      }, cb);
    },
    set: (items: Record<string, any>, cb?: any) => dual(() => {
      const ch: Record<string, any> = {};
      for (const [k, v] of Object.entries(items)) { ch[k] = { oldValue: clone(data[k]), newValue: clone(v) }; data[k] = clone(v); }
      onChanged.emit(ch, area); return undefined;
    }, cb),
    remove: (keys: any, cb?: any) => dual(() => {
      const ch: Record<string, any> = {};
      for (const k of ([] as string[]).concat(keys)) if (k in data) { ch[k] = { oldValue: clone(data[k]) }; delete data[k]; }
      onChanged.emit(ch, area); return undefined;
    }, cb),
    clear: (cb?: any) => dual(() => { const ch: Record<string, any> = {}; for (const k of Object.keys(data)) { ch[k] = { oldValue: clone(data[k]) }; delete data[k]; } onChanged.emit(ch, area); return undefined; }, cb),
    getBytesInUse: (_k?: any, cb?: any) => dual(() => JSON.stringify(data).length, cb),
  };
}

export function installChromeStub(opts: { storage?: { local?: Record<string, any>; sync?: Record<string, any>; session?: Record<string, any> };
  tabs?: any[]; manifest?: any; id?: string; sendMessageReply?: (msg: any, sender?: any) => any } = {}) {
  const g: any = globalThis;
  const saved = Object.getOwnPropertyDescriptor(g, 'chrome');
  const onChanged = ev();
  const calls: Record<string, any[]> = { sendMessage: [], tabsSendMessage: [], tabsCreate: [], tabsRemove: [], executeScript: [], alarmsCreate: [], setBadge: [] };
  const tabs: any[] = (opts.tabs || []).map((t, i) => ({ id: i + 1, windowId: 1, active: i === 0, url: 'about:blank', status: 'complete', ...t }));
  let nextTabId = tabs.reduce((m, t) => Math.max(m, t.id), 0) + 1;
  const alarms = new Map<string, any>();
  const chrome: any = {
    runtime: {
      id: opts.id || 'dispatch-env-test-extension', lastError: undefined,
      sendMessage: (...a: any[]) => {
        const cb = typeof a[a.length - 1] === 'function' ? a.pop() : null; const msg = a[a.length - 1]; calls.sendMessage.push(msg);
        const sender = { id: chrome.runtime.id };
        const replies = chrome.runtime.onMessage.listeners.map((f: any) => { let out: any; let sync = false; const r = f(msg, sender, (v: any) => { out = v; sync = true; }); return sync ? out : r === true ? undefined : undefined; });
        const reply = opts.sendMessageReply ? opts.sendMessageReply(msg, sender) : replies.find((r: any) => r !== undefined);
        if (cb) { queueMicrotask(() => cb(reply)); return undefined; }
        return Promise.resolve(reply);
      },
      onMessage: ev(), onInstalled: ev(), onStartup: ev(), onConnect: ev(),
      getURL: (p: string) => `chrome-extension://${chrome.runtime.id}/${String(p).replace(/^\//, '')}`,
      getManifest: () => opts.manifest || { manifest_version: 3, name: 'dispatch-env', version: '0.0.0' },
      openOptionsPage: async () => {}, reload() {},
    },
    storage: { local: storageArea(opts.storage?.local || {}, onChanged, 'local'), sync: storageArea(opts.storage?.sync || {}, onChanged, 'sync'),
      session: storageArea(opts.storage?.session || {}, onChanged, 'session'), onChanged },
    tabs: {
      query: async (q: any = {}) => tabs.filter((t) => Object.entries(q).every(([k, v]) => k === 'url' ? (Array.isArray(v) ? v : [v]).some((p: any) => String(t.url).includes(String(p).replace(/\*/g, ''))) : t[k] === v)),
      create: async (p: any = {}) => { const t = { id: nextTabId++, windowId: 1, active: p.active !== false, url: p.url || 'about:blank', status: 'complete' }; tabs.push(t); calls.tabsCreate.push(p); return t; },
      update: async (id: number, p: any = {}) => { const t = tabs.find((x) => x.id === id); if (!t) return undefined; Object.assign(t, p); return t; },
      remove: async (ids: any) => { for (const id of ([] as number[]).concat(ids)) { calls.tabsRemove.push(id); const i = tabs.findIndex((t) => t.id === id); if (i >= 0) tabs.splice(i, 1); chrome.tabs.onRemoved.emit(id, {}); } },
      reload: async () => {}, get: async (id: number) => tabs.find((t) => t.id === id),
      sendMessage: async (id: number, msg: any) => { calls.tabsSendMessage.push([id, msg]); return opts.sendMessageReply ? opts.sendMessageReply(msg, { tab: { id } }) : undefined; },
      onUpdated: ev(), onRemoved: ev(), onActivated: ev(),
    },
    scripting: { executeScript: async (inj: any) => { calls.executeScript.push(inj); return [{ result: undefined, frameId: 0 }]; },
      registerContentScripts: async () => {}, unregisterContentScripts: async () => {} },
    alarms: { create: (name: any, info?: any) => { if (typeof name !== 'string') { info = name; name = ''; } alarms.set(name, { name, ...info }); calls.alarmsCreate.push([name, info]); },
      get: async (n: string) => alarms.get(n), getAll: async () => [...alarms.values()], clear: async (n: string) => alarms.delete(n), clearAll: async () => { alarms.clear(); return true; }, onAlarm: ev() },
    permissions: { request: async () => true, contains: async () => true, remove: async () => true, getAll: async () => ({ permissions: [], origins: [] }) },
    windows: { getCurrent: async () => ({ id: 1, focused: true }), getLastFocused: async () => ({ id: 1, focused: true }), create: async (p: any = {}) => ({ id: 2, ...p }), update: async () => ({}) },
    action: { setIcon: async () => {}, setBadgeText: async (d: any) => { calls.setBadge.push(d); }, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
  };
  Object.defineProperty(g, 'chrome', { value: chrome, writable: true, configurable: true });
  const restore = () => { if (saved) Object.defineProperty(g, 'chrome', saved); else delete g.chrome; };
  return { chrome, calls, tabs, restore };
}

// ---------------------------------------------------------------------------
// in-memory sqlite (node:sqlite built in; falls back to better-sqlite3)
// ---------------------------------------------------------------------------
// openMemoryDb({ sql: 'CREATE TABLE ...' | ['..', '..'], migrationsDir: 'prisma/migrations' })
// Migrations replay in sorted directory order (each <dir>/migration.sql), exactly like
// `prisma migrate`. Returns the driver's DB: exec(sql), prepare(sql).all/get/run(...params), close().
export function openMemoryDb(opts: { sql?: string | string[]; migrationsDir?: string } = {}) {
  let db: any;
  const builtin = (process as any).getBuiltinModule;
  try {
    const { DatabaseSync } = builtin('node:sqlite');
    db = new DatabaseSync(':memory:');
  } catch {
    const req = createRequire(join(process.cwd(), 'package.json'));
    const Better = req('better-sqlite3');
    db = new Better(':memory:');
  }
  if (opts.migrationsDir) {
    const dir = opts.migrationsDir;
    if (!existsSync(dir)) throw new Error(`dispatch-env: migrationsDir not found: ${dir}`);
    for (const d of readdirSync(dir).sort()) {
      const f = join(dir, d, 'migration.sql');
      if (existsSync(f)) db.exec(readFileSync(f, 'utf8'));
    }
  }
  for (const s of ([] as string[]).concat(opts.sql || [])) db.exec(s);
  return db;
}
