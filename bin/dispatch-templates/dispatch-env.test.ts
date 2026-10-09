// Self-test for dispatch-env.ts: `node --experimental-strip-types --test dispatch-env.test.ts`
// Run from ~/bin/dispatch-templates. The fixture is only trustworthy because THIS passes.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadHtml, installFakeBrowser, makeFakePage, makeFakeContext, installFetchStub, installChromeStub,
  openMemoryDb, nodeList, fakeResponse, jsonResponse, headersToObject } from './dispatch-env.ts';

const PAGE = `<!doctype html><html><head><title>Orders &amp; more</title></head><body>
<div id="app" class="shell main"><ul class="orders">
  <li class="order first" data-testid="orderCard-1"><a href="/orders?orderID=111">One</a></li>
  <li class="order" data-testid="orderGroup-2"><a href="/x/order-details/222">Two</a></li>
  <li class="order last"><a href="/other">Three</a></li>
</ul>
<form id="f"><input name="u" type="text" value="bob"><input name="c" type="checkbox" checked><select id="Showing"><option value="a">A</option><option value="b" selected>B</option></select><button disabled>go</button></form>
<p>a &lt; b <br> <span>x</span><span>y</span></p><script>var x = "<b>";</script></div></body></html>`;

test('selectors: comma lists, attribute operators, combinators, pseudo', () => {
  const d = loadHtml(PAGE);
  // the exact selector loginFlow.js uses (comma list + attribute substring operators)
  const sel = 'a[href*="orderID="], a[href*="orderId="], a[href*="order-details"], [data-testid*="orderGroup"], [data-testid*="order-card"], [data-testid*="orderCard"]';
  const hits = d.querySelectorAll(sel);
  assert.equal(hits.length, 4); // a(111), li(orderCard-1) , a(order-details), li(orderGroup-2): document order
  assert.deepEqual(Array.from(hits as any).map((e: any) => e.localName + ':' + (e.getAttribute('href') ?? e.getAttribute('data-testid'))),
    ['li:orderCard-1', 'a:/orders?orderID=111', 'li:orderGroup-2', 'a:/x/order-details/222']);
  assert.equal(d.querySelectorAll('li.first, li.last').length, 2);
  assert.equal(d.querySelectorAll('li.nope, li.last').length, 1);
  assert.equal(d.querySelectorAll('#app > ul > li').length, 3);
  assert.equal(d.querySelectorAll('ul li a').length, 3);
  assert.equal(d.querySelectorAll('li.first + li').length, 1);
  assert.equal(d.querySelectorAll('li.first ~ li').length, 2);
  assert.equal(d.querySelectorAll('li:not(.first)').length, 2);
  assert.equal(d.querySelectorAll('li:first-child, li:last-child').length, 2);
  assert.equal(d.querySelectorAll('li:nth-child(2)').length, 1);
  assert.equal(d.querySelectorAll('li:nth-child(odd)').length, 2);
  assert.equal(d.querySelectorAll('li:nth-child(2n+1)').length, 2);
  assert.equal(d.querySelectorAll('input:checked').length, 1);
  assert.equal(d.querySelectorAll('button:disabled').length, 1);
  assert.equal(d.querySelectorAll('[class~="order"]').length, 3);
  assert.equal(d.querySelectorAll('[href^="/orders"]').length, 1);
  assert.equal(d.querySelectorAll('[href$="/other"]').length, 1);
  assert.equal(d.querySelectorAll('[type=text]').length, 1);
  assert.equal(d.querySelectorAll('[TYPE="TEXT" i]').length, 1);
  assert.equal(d.querySelectorAll('*').length > 10, true);
  assert.equal(d.querySelector('.nothing'), null);
  assert.equal((d.querySelector('li') as any).dataset.testid, 'orderCard-1');
  assert.equal((d.querySelector('a') as any).closest('ul').className, 'orders');
  assert.equal((d.querySelector('a') as any).matches('li > a, p'), true);
});

test('selectors: unsupported syntax THROWS instead of silently matching nothing', () => {
  const d = loadHtml(PAGE);
  assert.throws(() => d.querySelector('li:hover'), /not supported/);
  assert.throws(() => d.querySelector('a,'), /invalid selector/);
  assert.throws(() => d.querySelector('a['), /unclosed|bad attribute/);
  assert.throws(() => d.querySelector(''), /empty|invalid/);
});

test('querySelectorAll returns a NodeList, not an Array', () => {
  const d = loadHtml(PAGE);
  const nl: any = d.querySelectorAll('li');
  assert.equal(typeof nl.map, 'undefined');
  assert.equal(typeof nl.some, 'undefined');
  assert.equal(typeof nl.forEach, 'function');
  assert.equal(nl.length, 3);
  assert.equal(nl.item(0), nl[0]);
  assert.equal(Array.from(nl).length, 3);
  assert.equal([...nl].length, 3);
  assert.equal(nl.item(9), null);
  assert.equal(nodeList([1, 2]).length, 2);
});

test('document: parse, text, entities, attributes, mutation', () => {
  const d = loadHtml(PAGE);
  assert.equal(d.title, 'Orders & more');
  assert.equal(d.getElementById('Showing')!.localName, 'select');
  assert.equal(d.getElementById('nope'), null);
  const p: any = d.querySelector('p');
  assert.equal(p.textContent, 'a < b  xy');
  assert.equal(d.querySelector('script')!.textContent, 'var x = "<b>";');
  assert.equal((d.querySelector('input[name=u]') as any).value, 'bob');
  assert.equal((d.querySelector('input[name=c]') as any).checked, true);
  assert.equal((d.querySelector('option[selected]') as any).value ?? 'b', 'b');
  const ul: any = d.querySelector('ul');
  const li = d.createElement('li'); li.className = 'order new'; li.textContent = 'Four'; ul.appendChild(li);
  assert.equal(d.querySelectorAll('li.order').length, 4);
  assert.equal(d.querySelector('li.new')!.textContent, 'Four');
  li.classList.add('extra'); li.classList.remove('new');
  assert.equal(li.className, 'order extra');
  li.remove();
  assert.equal(d.querySelectorAll('li.order').length, 3);
  ul.innerHTML = '<li class="z">zz</li>';
  assert.equal(d.querySelectorAll('li').length, 1);
  assert.match(ul.outerHTML, /^<ul class="orders"><li class="z">zz<\/li><\/ul>$/);
  const frag = loadHtml('<p>only body content</p>');
  assert.equal(frag.body.textContent, 'only body content');
});

test('events: click bubbles, handlers run, preventDefault, once', () => {
  const d = loadHtml('<div id=o><button id=b>x</button></div>');
  const seen: string[] = [];
  d.getElementById('o')!.addEventListener('click', (e: any) => seen.push('outer:' + e.target.id));
  d.getElementById('b')!.addEventListener('click', () => seen.push('inner'), { once: true });
  d.getElementById('b')!.click(); d.getElementById('b')!.click();
  assert.deepEqual(seen, ['inner', 'outer:b', 'outer:b']);
});

test('installFakeBrowser: window===globalThis, document, location, fetch, XHR, restore', async (t) => {
  const g: any = globalThis;
  const before = Reflect.ownKeys(g).length;
  const fb = installFakeBrowser({ html: PAGE, url: 'https://www.costco.com/orders', route: (url, init) =>
    url.endsWith('/gql') ? { status: 200, body: { data: { n: 1 } } } : { status: 404, body: 'nope' } });
  t.after(() => fb.restore());
  assert.equal(g.window, g);
  assert.equal(g.window.document, g.document);
  assert.equal(g.location.hostname, 'www.costco.com');
  assert.equal(g.document.querySelectorAll('li').length, 3);
  g.window.__costcoAuth = { token: 't' };
  assert.deepEqual(fb.window.__costcoAuth, { token: 't' });
  const r = await g.fetch('https://x/gql', { method: 'post', headers: new Map([['X-A', '1']]), body: '{}' });
  assert.equal(r.ok, true); assert.equal((await r.json()).data.n, 1);
  assert.equal(fb.fetchCalls[0].method, 'POST'); assert.equal(fb.fetchCalls[0].headers['x-a'], '1');
  assert.equal((await g.fetch('https://x/other')).status, 404);
  const xhr = new g.XMLHttpRequest(); xhr.open('POST', 'https://x/gql'); xhr.setRequestHeader('Content-Type', 'a');
  const done = new Promise((res) => { xhr.onload = res; }); xhr.send('b'); assert.equal(xhr.readyState, 1); await done;
  assert.equal(xhr.status, 200); assert.equal(JSON.parse(xhr.responseText).data.n, 1);
  assert.equal(fb.xhrCalls[0].headers['content-type'], 'a');
  g.localStorage.setItem('k', 'v'); assert.equal(g.localStorage.getItem('k'), 'v'); assert.equal(g.localStorage.getItem('z'), null);
  g.location.assign('/signin'); assert.equal(fb.navigations.at(-1), 'https://www.costco.com/signin');
  const seen: string[] = []; g.addEventListener('ping', (e: any) => seen.push(e.type)); g.dispatchEvent(new g.Event('ping'));
  assert.deepEqual(seen, ['ping']);
  fb.restore();
  assert.equal(typeof g.window, 'undefined'); assert.equal(typeof g.document, 'undefined');
  assert.equal(typeof g.__costcoAuth, 'undefined', 'page state stashed on window is dropped by restore()');
  assert.equal(Reflect.ownKeys(g).length, before, 'restore() leaks no globals');
});

test('installFakeBrowser: selectors override map (legacy) still works', () => {
  const fb = installFakeBrowser({ html: '<p>x</p>', selectors: { '.hit': [{ id: 1 }, { id: 2 }] } });
  try {
    assert.equal(document.querySelectorAll('.hit').length, 2);
    assert.equal((document.querySelector('.hit') as any).id, 1);
    assert.equal(document.querySelectorAll('p').length, 1);
  } finally { fb.restore(); }
});

test('makeFakePage: evaluate runs against the fake window; .window/.document exposed; waitFor stubs', async (t) => {
  const fb = installFakeBrowser({ html: PAGE, url: 'https://www.costco.com/orders' });
  t.after(() => fb.restore());
  const page = makeFakePage({ url: 'https://www.costco.com/orders' });
  assert.equal(page.window, globalThis); assert.equal(page.document, document);
  page.window.__costcoAuth = { token: 'abc' };
  // costco.js style: state read inside evaluate, with a catch fallback
  const s = await page.evaluate(() => ({ auth: !!(window as any).__costcoAuth, pages: ((window as any).__costcoAllOrders || []).length, hasSelect: !!document.getElementById('Showing') }));
  assert.deepEqual(s, { auth: true, pages: 0, hasSelect: true });
  // loginFlow.js style: comma selector inside evaluate
  assert.equal(await page.evaluate(() => document.querySelectorAll('a[href*="orderID="], [data-testid*="orderGroup"]').length > 0), true);
  assert.equal(await page.evaluate((n: number) => n + 1, 41), 42);
  assert.equal(await page.$eval('#Showing', (e: any) => e.localName), 'select');
  assert.equal((await page.$$eval('li.order', (els: any[]) => els.map((e) => e.className))).length, 3);
  assert.equal((await page.$$('li')).length, 3); assert.equal(await page.$('.nope'), null);
  await assert.rejects(page.waitForSelector('.nope'), /Timeout exceeded/);
  assert.equal((await page.waitForSelector('#app') as any).id, 'app');
  assert.equal(await page.waitForSelector('.nope', { state: 'detached' }), null);
  await assert.rejects(page.waitForFunction(() => false), /Timeout/);
  await page.waitForFunction(() => true);
  await page.waitForLoadState('domcontentloaded'); await page.waitForTimeout(50);
  await page.waitForURL('**/orders'); await assert.rejects(page.waitForURL(/signin/), /Timeout/);
  assert.equal(await page.title(), 'Orders & more');
  assert.match(await page.content(), /^<!DOCTYPE html><html>/);
  assert.equal(page.url(), 'https://www.costco.com/orders');
  await page.goto('https://www.costco.com/signin', { waitUntil: 'domcontentloaded' });
  assert.equal(page.url(), 'https://www.costco.com/signin'); assert.equal(location.pathname, '/signin');
  assert.deepEqual(page.gotoCalls[0], ['https://www.costco.com/signin', { waitUntil: 'domcontentloaded' }]);
  assert.deepEqual(page.frames(), [page]); assert.equal(page.mainFrame(), page);
  await page.fill('input[name=u]', 'alice'); assert.equal(await page.inputValue('input[name=u]'), 'alice');
  const loc = page.locator('li.order'); assert.equal(await loc.count(), 3);
  assert.equal(await page.locator('.nope').count(), 0); await assert.rejects(page.locator('.nope').click(), /Timeout/);
  const got: any[] = []; page.on('response', (r: any) => got.push(r)); page.emit('response', 1); page.emit('response', 2);
  assert.deepEqual(got, [1, 2]); page.once('close', () => got.push('c')); await page.close(); assert.equal(page.isClosed(), true); assert.equal(got.at(-1), 'c');
});

test('makeFakePage: addInitScript re-runs on goto; gotoThrowsAfter; context', async () => {
  const page = makeFakePage({ url: 'about:blank', gotoThrowsAfter: 1 });
  let n = 0; await page.addInitScript(() => { n++; });
  await page.goto('https://a.test/1'); assert.equal(n, 1);
  await assert.rejects(page.goto('https://a.test/2'), /navigation failed/);
  const ctx = makeFakeContext(); const p2 = await ctx.newPage(); assert.equal(ctx.pages().length, 1); assert.equal(p2.context(), ctx);
  await ctx.addInitScript(() => {}); assert.deepEqual(await ctx.cookies(), []);
});

test('makeFakePage works without a browser installed for non-DOM calls and says so for DOM calls', async () => {
  const page = makeFakePage({ url: 'https://z.test/' });
  assert.equal(page.url(), 'https://z.test/'); assert.equal(await page.evaluate((x: number) => x * 2, 4), 8);
  await assert.rejects(page.$('a'), /no document installed/);
});

test('installFetchStub + response helpers', async () => {
  const f = installFetchStub((url, init) => ({ status: init.method === 'POST' ? 201 : 200, body: { url } }));
  try {
    const r = await fetch('https://a/b', { method: 'POST', headers: { 'X-Y': 'z' }, body: 'q' });
    assert.equal(r.status, 201); assert.equal((await r.clone().json()).url, 'https://a/b'); assert.equal(await r.text(), '{"url":"https://a/b"}');
    assert.equal(f.calls[0].headers['x-y'], 'z');
  } finally { f.restore(); }
  assert.equal(jsonResponse({ a: 1 }).headers.get('Content-Type'), 'application/json');
  assert.equal(fakeResponse(500).ok, false);
  assert.deepEqual(headersToObject(new Map([['A', 'b']])), { a: 'b' });
});

test('installChromeStub: storage (promise + callback), events, runtime, tabs, alarms, restore', async () => {
  const c = installChromeStub({ storage: { local: { a: 1 }, sync: { s: 's' } }, tabs: [{ url: 'https://x.test/' }, { url: 'https://y.test/' }] });
  try {
    const { chrome } = c;
    assert.deepEqual(await chrome.storage.local.get('a'), { a: 1 });
    assert.deepEqual(await chrome.storage.local.get({ a: 0, b: 2 }), { a: 1, b: 2 });
    assert.deepEqual(await chrome.storage.local.get(null), { a: 1 });
    const changes: any[] = []; chrome.storage.onChanged.addListener((ch: any, area: string) => changes.push([ch, area]));
    await chrome.storage.local.set({ a: 2, o: { k: [1] } });
    assert.deepEqual(changes[0], [{ a: { oldValue: 1, newValue: 2 }, o: { oldValue: undefined, newValue: { k: [1] } } }, 'local']);
    const viaCb = await new Promise((res) => chrome.storage.sync.get('s', res)); assert.deepEqual(viaCb, { s: 's' });
    await chrome.storage.local.remove('a'); assert.equal((await chrome.storage.local.get('a')).a, undefined);
    chrome.runtime.onMessage.addListener((msg: any, _s: any, send: any) => { if (msg.type === 'ping') send({ pong: true }); });
    assert.deepEqual(await chrome.runtime.sendMessage({ type: 'ping' }), { pong: true });
    assert.deepEqual(c.calls.sendMessage, [{ type: 'ping' }]);
    assert.match(chrome.runtime.getURL('/a.js'), /^chrome-extension:\/\/.+\/a\.js$/);
    assert.equal(chrome.runtime.getManifest().manifest_version, 3);
    assert.equal((await chrome.tabs.query({})).length, 2);
    assert.equal((await chrome.tabs.query({ url: ['https://y.test/*'] })).length, 1);
    const t = await chrome.tabs.create({ url: 'https://n.test/' }); assert.equal(c.tabs.length, 3);
    await chrome.tabs.remove(t.id); assert.equal(c.tabs.length, 2);
    chrome.alarms.create('tick', { periodInMinutes: 1 }); assert.equal((await chrome.alarms.get('tick')).periodInMinutes, 1);
    const fired: any[] = []; chrome.runtime.onInstalled.addListener((d: any) => fired.push(d)); chrome.runtime.onInstalled.emit({ reason: 'install' });
    assert.deepEqual(fired, [{ reason: 'install' }]);
    await chrome.scripting.executeScript({ target: { tabId: 1 }, func: () => 1 }); assert.equal(c.calls.executeScript.length, 1);
  } finally { c.restore(); }
  assert.equal(typeof (globalThis as any).chrome, 'undefined');
});

test('openMemoryDb: sql + migrations replay in order; isolated per call', () => {
  const db = openMemoryDb({ sql: ['CREATE TABLE t (id INTEGER PRIMARY KEY, n TEXT)', "INSERT INTO t (n) VALUES ('a'),('b')"] });
  assert.deepEqual(db.prepare('SELECT n FROM t ORDER BY id').all().map((r: any) => r.n), ['a', 'b']);
  assert.equal(db.prepare('SELECT COUNT(*) AS c FROM t').get().c, 2);
  assert.equal(db.prepare('INSERT INTO t (n) VALUES (?)').run('c').changes, 1);
  const other = openMemoryDb(); assert.throws(() => other.prepare('SELECT * FROM t'), /no such table/);
  const dir = mkdtempSync(join(tmpdir(), 'mig-'));
  for (const [name, sql] of [['20260102_b', 'ALTER TABLE a ADD COLUMN y INTEGER DEFAULT 7;'], ['20260101_a', 'CREATE TABLE a (x INTEGER);'], ['README', '']]) {
    mkdirSync(join(dir, name)); if (sql) writeFileSync(join(dir, name, 'migration.sql'), sql);
  }
  const m = openMemoryDb({ migrationsDir: dir, sql: 'INSERT INTO a (x) VALUES (1)' });
  assert.equal(m.prepare('SELECT y FROM a').get().y, 7);
  assert.throws(() => openMemoryDb({ migrationsDir: join(dir, 'missing') }), /migrationsDir not found/);
});

// Regression for the shape the sidecar's costco interceptor relies on: an init script (function OR
// source string) that wraps window.fetch / XMLHttpRequest.prototype and stashes state on window.
test('init scripts: function and string sources patch fetch + XHR prototype against the fake window', async () => {
  const fb = installFakeBrowser({ url: 'https://www.costco.com/orders', route: () => ({ status: 200, body: { ok: 1 } }) });
  try {
    const page = makeFakePage({ url: 'https://www.costco.com/orders' });
    await page.addInitScript(function () {
      const w: any = window; if (w.__inst) return; w.__inst = true;
      const orig = w.fetch.bind(w); w.__seen = [];
      w.fetch = async (u: any, i: any) => { w.__seen.push(String(u)); return orig(u, i); };
      const open = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = function (this: any, m: string, u: string, ...rest: any[]) { w.__seen.push('xhr:' + u); return (open as any).call(this, m, u, ...rest); };
    });
    await page.addInitScript('window.__fromString = (window.__fromString || 0) + 1;');
    await page.goto('https://www.costco.com/orders');
    await (globalThis as any).fetch('https://a.test/1');
    const x = new (globalThis as any).XMLHttpRequest(); x.open('GET', 'https://a.test/2'); x.send();
    assert.deepEqual((window as any).__seen, ['https://a.test/1', 'xhr:https://a.test/2']);
    assert.equal((window as any).__fromString, 1);
    await page.goto('https://www.costco.com/orders'); // guard keeps the wrap single; string script re-runs
    assert.equal((window as any).__fromString, 2);
  } finally { fb.restore(); }
  assert.equal(typeof (globalThis as any).__seen, 'undefined');
  assert.equal(typeof (globalThis as any).__inst, 'undefined');
});
