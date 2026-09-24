import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';
import { withRequestTimeout } from '../lib/request-timeout.mjs';
import { adminRequest, currentRequestIdentity, notifyUnauthorized } from '../components/rental/admin-model.ts';

const rentalSource = await readFile(new URL('../components/rental/rental-panel.tsx', import.meta.url), 'utf8');
const parsed = ts.createSourceFile('rental-panel.tsx', rentalSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const requestNode = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'rentalRequest');
assert(requestNode, 'test the real customer request function, not a duplicate');
const compiled = ts.transpileModule(requestNode.getText(parsed), { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.None } }).outputText;
const rentalRequest = runInNewContext(`${compiled}; rentalRequest;`, {
  withRequestTimeout, currentRequestIdentity, notifyUnauthorized, Headers, Error,
  fetch: (...args) => globalThis.fetch(...args),
});

function legacyStaticMethods(t) {
  for (const name of ['timeout', 'any']) {
    const descriptor = Object.getOwnPropertyDescriptor(AbortSignal, name);
    Object.defineProperty(AbortSignal, name, { configurable: true, writable: true, value: undefined });
    t.after(() => { if (descriptor) Object.defineProperty(AbortSignal, name, descriptor); else delete AbortSignal[name]; });
  }
}

function pendingFetch(t) {
  let child;
  t.mock.method(globalThis, 'fetch', async (_path, init) => {
    child = init.signal;
    return new Promise((_resolve, reject) => child.addEventListener('abort', () => reject(child.reason), { once: true }));
  });
  return () => child;
}

test('reproduce the reported failure when the static timeout method is absent', t => {
  legacyStaticMethods(t);
  assert.throws(() => AbortSignal.timeout(15000), /AbortSignal.timeout is not a function/);
});

for (const path of ['/api/auth/register', '/api/auth/login', '/api/rental/state']) {
  test(`customer ${path} works without static abort methods`, async t => {
    legacyStaticMethods(t);
    const fetchSpy = t.mock.method(globalThis, 'fetch', async (actualPath, init) => {
      assert.equal(actualPath, path);
      assert.equal(init.cache, 'no-store');
      assert.equal(init.headers.get('Content-Type'), 'application/json');
      assert.equal(init.headers.get('X-Idempotency-Key'), 'isolated-test');
      assert.equal(init.signal.aborted, false);
      assert.equal(init.body, JSON.stringify({ name: 'isolated-fixture' }));
      return new Response(JSON.stringify({ account: { name: 'isolated-fixture' } }));
    });
    const result = await rentalRequest(path, { method: 'POST', headers: { 'X-Idempotency-Key': 'isolated-test' }, body: JSON.stringify({ name: 'isolated-fixture' }) });
    assert.equal(result.account.name, 'isolated-fixture');
    assert.equal(fetchSpy.mock.callCount(), 1);
  });
}

test('admin polling works with caller cancellation and no static abort methods', async t => {
  legacyStaticMethods(t);
  const parent = new AbortController();
  t.mock.method(globalThis, 'fetch', async (_path, init) => {
    assert.notEqual(init.signal, parent.signal);
    return new Response(JSON.stringify({ instances: [] }));
  });
  assert.deepEqual(await adminRequest('/api/admin/instances', { signal: parent.signal }), { instances: [] });
});

test('success clears the timeout and removes its parent listener', async t => {
  const parent = new AbortController();
  const timers = t.mock.method(globalThis, 'setTimeout');
  const clears = t.mock.method(globalThis, 'clearTimeout');
  const add = t.mock.method(parent.signal, 'addEventListener');
  const remove = t.mock.method(parent.signal, 'removeEventListener');
  assert.equal(await withRequestTimeout(async () => 42, 15000, parent.signal), 42);
  assert.equal(clears.mock.calls.at(-1).arguments[0], timers.mock.calls[0].result);
  assert.equal(remove.mock.calls[0].arguments[1], add.mock.calls[0].arguments[1]);
  assert.equal(parent.signal.aborted, false);
});

test('network failures also clean up timers and cancellation listeners', async t => {
  const parent = new AbortController();
  const clears = t.mock.method(globalThis, 'clearTimeout');
  const remove = t.mock.method(parent.signal, 'removeEventListener');
  const failure = new Error('isolated network failure');
  await assert.rejects(withRequestTimeout(async () => { throw failure; }, 15000, parent.signal), error => error === failure);
  assert.equal(clears.mock.callCount(), 1);
  assert.equal(remove.mock.callCount(), 1);
});

test('an already cancelled caller never sends a request', async () => {
  const parent = new AbortController(); parent.abort();
  let calls = 0;
  await assert.rejects(withRequestTimeout(async () => { calls++; }, 15000, parent.signal), { name: 'AbortError' });
  assert.equal(calls, 0);
});

test('caller cancellation aborts the in-flight fetch', async t => {
  legacyStaticMethods(t);
  const child = pendingFetch(t);
  const parent = new AbortController();
  const promise = rentalRequest('/api/rental/state', { signal: parent.signal });
  const rejected = assert.rejects(promise, { name: 'AbortError' });
  parent.abort();
  await rejected;
  assert.equal(child().aborted, true);
});

test('supplying a caller signal does not disable the 15 second timeout', async t => {
  legacyStaticMethods(t);
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const child = pendingFetch(t);
  const parent = new AbortController();
  const promise = adminRequest('/api/admin/instances', { signal: parent.signal });
  const rejected = assert.rejects(promise, { name: 'TimeoutError', message: '请求超时，请检查网络后重试。' });
  t.mock.timers.tick(15000);
  await rejected;
  assert.equal(child().aborted, true);
  assert.equal(parent.signal.aborted, false);
});

test('timeout includes slow JSON reads even when the existing JSON fallback catches abort', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  t.mock.method(globalThis, 'fetch', async (_path, init) => ({
    ok: true, status: 200,
    json: () => new Promise((_resolve, reject) => {
      if (init.signal.aborted) reject(new Error('aborted body'));
      else init.signal.addEventListener('abort', () => reject(new Error('aborted body')), { once: true });
    }),
  }));
  const promise = rentalRequest('/api/auth/register');
  const rejected = assert.rejects(promise, { name: 'TimeoutError' });
  await Promise.resolve();
  t.mock.timers.tick(15000);
  await rejected;
});

test('each poll has its own timeout and leaves the shared caller signal active', async t => {
  legacyStaticMethods(t);
  const parent = new AbortController();
  const seen = new Set();
  const add = t.mock.method(parent.signal, 'addEventListener');
  const remove = t.mock.method(parent.signal, 'removeEventListener');
  for (let index = 0; index < 100; index++) {
    await withRequestTimeout(async signal => { seen.add(signal); }, 15000, parent.signal);
  }
  assert.equal(seen.size, 100);
  assert.equal(parent.signal.aborted, false);
  assert.equal(add.mock.callCount(), 100);
  assert.equal(remove.mock.callCount(), 100);
});

test('backend errors remain visible and write requests are not retried', async t => {
  const spy = t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ message: '账户已存在' }), { status: 409 }));
  await assert.rejects(rentalRequest('/api/auth/register', { method: 'POST' }), /409: 账户已存在/);
  assert.equal(spy.mock.callCount(), 1);
});

for (const file of ['app/page.tsx', 'components/rental/rental-panel.tsx', 'components/rental/admin-model.ts', 'components/rental/admin-workspace.tsx']) {
  test(`${file} contains no unguarded static AbortSignal dependencies`, async () => {
    const source = await readFile(new URL(`../${file}`, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /AbortSignal\.(timeout|any)\s*\(/);
    if (file.endsWith('admin-workspace.tsx')) assert.match(source, /signal: abort.signal/);
    else assert.match(source, /withRequestTimeout/);
  });
}
