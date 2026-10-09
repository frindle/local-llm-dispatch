// Proves fake-browser.ts behaves like the real browser surfaces the bfmr/sidecar
// targets touch. Run: node --test ~/bin/dispatch-templates/fake-browser.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installFakeBrowser, makeFakePage, makeFakeContext, headersToObject, nodeList, fakeResponse } from './fake-browser.ts';

// A real-shaped interceptor source (same shape as sidecar/src/bfmr.js): wraps
// window.fetch and XHR open/setRequestHeader/send, stashes captured auth headers.
function interceptorSource() {
  return function () {
    const w: any = (globalThis as any).window;
    if (w.__installed) return;
    w.__installed = true;
    w.__captured = [];
    const origFetch = w.fetch.bind(w);
    w.fetch = async function (input: any, init: any) {
      const res = await origFetch(input, init);
      const h = init && init.headers ? init.headers : {};
      const auth = h instanceof Map ? h.get('Authorization') : (h.Authorization || h.authorization);
      w.__captured.push({ via: 'fetch', status: res.status, auth, body: await res.clone().json() });
      return res;
    };
    const X = (globalThis as any).XMLHttpRequest;
    const oOpen = X.prototype.open, oSet = X.prototype.setRequestHeader, oSend = X.prototype.send;
    X.prototype.open = function (m: string, u: string) { this.__u = u; this.__h = {}; return oOpen.apply(this, arguments as any); };
    X.prototype.setRequestHeader = function (k: string, v: string) { this.__h[k.toLowerCase()] = v; return oSet.apply(this, arguments as any); };
    X.prototype.send = function () {
      this.addEventListener('load', () => w.__captured.push({ via: 'xhr', status: this.status, auth: this.__h.authorization }));
      return oSend.apply(this, arguments as any);
    };
  };
}

test('window === globalThis: an interceptor wrapping window.fetch also wraps bare fetch', async (t) => {
  const env = installFakeBrowser({ route: (url) => (url.includes('/api/my-tracker') ? { status: 200, body: { data: { my_tracker: [1] } } } : undefined) });
  t.after(env.restore);
  interceptorSource()();
  const res = await fetch('https://www.bfmr.com/api/my-tracker?page_no=1', { headers: { Authorization: 'Bearer abc' } });
  assert.equal(res.ok, true);
  assert.deepEqual(await res.json(), { data: { my_tracker: [1] } });
  assert.equal((globalThis as any).window.__captured[0].auth, 'Bearer abc');
  assert.equal(env.fetchCalls[0].headers.authorization, 'Bearer abc');
});

test('XHR completes asynchronously and fires load listeners', async (t) => {
  const env = installFakeBrowser({ route: () => ({ status: 201, body: { ok: 1 } }) });
  t.after(env.restore);
  interceptorSource()();
  const x = new (globalThis as any).XMLHttpRequest();
  let loaded = false;
  x.onload = () => { loaded = true; };
  x.open('post', 'https://www.bfmr.com/api/x');
  x.setRequestHeader('Authorization', 'Bearer xhr');
  x.send('{}');
  assert.equal(loaded, false, 'load must not fire synchronously');
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(loaded, true);
  assert.equal(x.status, 201);
  assert.equal(env.xhrCalls[0].method, 'POST');
  assert.equal((globalThis as any).window.__captured.at(-1).auth, 'Bearer xhr');
});

test('each install builds a FRESH XHR class (prototype patches never leak)', async (t) => {
  const a = installFakeBrowser();
  a.FakeXHR.prototype.send = function () { throw new Error('patched'); };
  a.restore();
  const b = installFakeBrowser();
  t.after(b.restore);
  assert.notEqual(a.FakeXHR, b.FakeXHR);
  assert.doesNotThrow(() => { const x = new b.FakeXHR(); x.open('GET', 'u'); x.send(); });
});

test('restore() puts the real globals back', () => {
  const before = (globalThis as any).fetch;
  const env = installFakeBrowser();
  assert.notEqual((globalThis as any).fetch, before);
  env.restore();
  assert.equal((globalThis as any).fetch, before);
  assert.equal('document' in globalThis, false);
});

test('querySelectorAll returns a NodeList-like (iterable, no .some)', (t) => {
  const env = installFakeBrowser({ selectors: { input: [{ type: 'email' }, { type: 'password' }] } });
  t.after(env.restore);
  const nl: any = (globalThis as any).document.querySelectorAll('input');
  assert.equal(nl.length, 2);
  assert.equal(typeof nl.some, 'undefined');
  assert.equal(Array.from(nl).some((i: any) => i.type === 'password'), true);
  assert.equal((globalThis as any).document.querySelector('meta[name="csrf-token"]'), null);
});

test('page.evaluate runs the in-page fn against the fakes; goto re-runs init scripts', async (t) => {
  const env = installFakeBrowser({ route: () => ({ status: 401, body: {} }) });
  t.after(env.restore);
  const page = makeFakePage({ url: 'https://www.bfmr.com/my-tracker' });
  const ctx = makeFakeContext([page]);
  await ctx.addInitScript(interceptorSource());
  await page.goto('https://www.bfmr.com/my-tracker', { waitUntil: 'domcontentloaded' });
  assert.equal((globalThis as any).window.__installed, true, 'init script ran on navigation');
  const r = await page.evaluate(async ({ url }: any) => {
    const res = await fetch(url, { headers: (globalThis as any).window.__authHeaders || {} });
    return { ok: res.ok, status: res.status };
  }, { url: 'https://www.bfmr.com/api/my-tracker' });
  assert.deepEqual(r, { ok: false, status: 401 });
  assert.deepEqual(page.gotoCalls[0], ['https://www.bfmr.com/my-tracker', { waitUntil: 'domcontentloaded' }]);
});

test('headersToObject normalises Headers/Map/array/object', () => {
  assert.deepEqual(headersToObject(new Headers({ 'X-A': '1' })), { 'x-a': '1' });
  assert.deepEqual(headersToObject(new Map([['X-B', '2']])), { 'x-b': '2' });
  assert.deepEqual(headersToObject([['X-C', '3']]), { 'x-c': '3' });
  assert.deepEqual(headersToObject({ 'X-D': '4' }), { 'x-d': '4' });
  assert.equal(nodeList([1]).item(5), null);
  assert.equal(fakeResponse(404).ok, false);
});
