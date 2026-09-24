import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { adminDefaults, readAdminRoute, adminSearch, filterAdminInstances, moneyToCents, isAttention, currentRequestIdentity, resetRequestIdentity, notifyUnauthorized } from '../components/rental/admin-model.ts';

const row = (id, props = {}) => ({ id: String(id), name: 'gaudi-dev', owner: 'alice', nodeId: 'G2-002', state: 'stopped', mode: 'gpu', ...props });
const rows = [row(1), row(2, { nodeId: 'G2-003', owner: 'alice-extra', state: 'running', mode: 'headless' }), row(3, { nodeId: 'G2-003', state: 'error' }), row(4, { state: 'running' }), row(5, { state: 'starting', nodeOnline: false })];
const match = patch => filterAdminInstances(rows, { ...adminDefaults, ...patch }).map(r => r.id);
test('node names are searchable and case-insensitive', () => assert.deepEqual(match({ q: ' g2-003 ' }), ['3', '2']));
test('exact owner scope never includes prefixed accounts', () => { const result = match({ owner: 'alice' }); assert.equal(result.length, 4); assert(!result.includes('2')); });
test('node + state + mode filters compose', () => assert.deepEqual(match({ node: 'G2-003', status: 'running', mode: 'headless' }), ['2']));
test('attention includes errors and offline nodes', () => assert.deepEqual(match({ status: 'attention' }), ['5', '3']));
test('processing and stopped filters are distinct', () => { assert.deepEqual(match({ status: 'pending' }), ['5']); assert.deepEqual(match({ status: 'stopped' }), ['1']); });
test('unknown query produces no matches', () => assert.deepEqual(match({ q: 'no-matching-instance' }), []));
test('instance ID with # matches', () => assert.deepEqual(match({ q: '#4' }), ['4']));
test('filtering preserves input arrays', () => { const original = structuredClone(rows); match({}); assert.deepEqual(rows, original); });
test('legacy instance without node or mode stays G2-002 GPU', () => assert.equal(filterAdminInstances([row(8, { nodeId: undefined, mode: undefined })], { ...adminDefaults, node: 'G2-002', mode: 'gpu' }).length, 1));
test('invalid URL enums fall back safely', () => assert.deepEqual(readAdminRoute('?section=no&status=no&mode=no&accounts=no&finance=no'), adminDefaults));
test('navigation query round trips including scope and filters', () => { const route = { ...adminDefaults, section: 'finance', owner: 'alice', q: 'G2-003 #4', finance: 'codes' }; const search = adminSearch(route, '?extra=preserved'); assert.deepEqual(readAdminRoute(search), route); assert.equal(new URLSearchParams(search).get('extra'), 'preserved'); assert.equal(new URLSearchParams(search).get('view'), 'admin'); });
test('reset removes stale query keys', () => { const search = adminSearch(adminDefaults, '?q=test&owner=alice&node=G2-003&status=running'); assert(!search.includes('owner=')); assert.deepEqual(readAdminRoute(search), adminDefaults); });
for (const [input, expected] of [['0.01', 1], ['4', 400], ['6.66', 666], [' 100.10 ', 10010], ['10000', 1000000]]) test(`money exact cents: ${input}`, () => assert.equal(moneyToCents(input), expected));
for (const input of ['', '0', '-4', '1.001', 'NaN', 'Infinity', '1e3', '01', '10000.01']) test(`reject invalid money: ${input}`, () => assert.equal(moneyToCents(input), null));
test('manual recharge keeps existing server maximum', () => { assert.equal(moneyToCents('1000000', 100000000), 100000000); assert.equal(moneyToCents('1000000.01', 100000000), null); });
test('attention recognizes repair required', () => assert(isAttention(row(9, { state: 'repair_required' }))));
const source = await readFile(new URL('../components/rental/rental-panel.tsx', import.meta.url), 'utf8');
const admin = await readFile(new URL('../components/rental/admin-workspace.tsx', import.meta.url), 'utf8');
const nodes = await readFile(new URL('../components/rental/admin-nodes.tsx', import.meta.url), 'utf8');
const css = await readFile(new URL('../components/rental/admin-workspace.css', import.meta.url), 'utf8');
test('only admin role mounts management workspace', () => assert.match(source, /account\.role === 'admin' && <div hidden=\{!adminView\}>/));
test('admin login defaults to admin unless explicit customer view', () => assert.match(source, /result\.account\.role === 'admin'\) setTab\(new URLSearchParams\(location\.search\)\.get\('view'\) === 'workspace' \? 'instances' : 'admin'\)/));
test('codes remain mounted under hidden tab, not conditionally destroyed', () => assert.match(admin, /<div hidden=\{route.finance !== 'codes'\}>\{codes\(/));
test('refresh and generation guards retained', () => { assert.match(source, /beforeunload/); assert.match(source, /expectedCents/); assert.match(source, /X-Idempotency-Key/); assert.match(admin, /requestKeys.current.get\(intent\)/); });
test('obsolete stacked admin sections removed', () => { assert.doesNotMatch(source, /function AdminFleet|function AdminPanel|充值 SDK 生成器|rental-admin-customer-list/); });
test('native dialogs and hidden pages cannot be revived by CSS', () => assert.match(css, /dialog:not\(\[open\]\).*display: none !important/s));
test('no costly animated effects introduced', () => assert.doesNotMatch(css, /backdrop-filter|filter:\s*blur|transition:\s*all/));
test('node monitoring uses a bounded five-second admin-only poll', () => {
  assert.match(admin, /adminRequest<NodeTelemetryResponse>\('\/api\/admin\/nodes'/);
  assert.match(admin, /setInterval\(\(\) => void load\(\), 5_000\)/);
  assert.match(admin, /document\.hidden/);
});
test('node monitoring exposes requested host and electricity fields', () => {
  for (const label of ['CPU 型号', 'CPU 使用量', '内存规格', '内存使用量', 'Swap 使用量', '当前整机功耗', '近 24 小时平均', '历史电费', '近 24 小时电费', '预计 30 天电费']) assert.match(nodes, new RegExp(label));
  assert.match(nodes, /BMC DCMI · 整机输入实测/);
  assert.match(nodes, /coverageSeconds24h/);
});

test('customer reset updates both route fields in one navigation', () => assert.match(admin, /onReset: \(\) => navigate\(\{ customer: '', accounts: 'active' \}\)/));
test('customer ledger is scoped on server before record limit', () => assert.match(source, /\?owner=\$\{encodeURIComponent\(ownerFilter\)\}/));
test('account change clears private state and epochs stale requests', () => {
  assert.match(source, /setState\(initialState\); setLoading\(true\)/);
  assert.match(source, /key=\{account.name\}/);
  assert.match(source, /if \(epoch === authEpoch.current\) \{ refreshing.current = false; setLoading\(false\); \}/);
});
test('all customer-view shortcuts use URL-aware navigation', () => assert.doesNotMatch(source, /onClick=\{\(\) => setTab\(/));
test('expired requests cannot invalidate a newly authenticated account', () => {
  const original = globalThis.window;
  let events = 0;
  globalThis.window = { dispatchEvent: event => { assert.equal(event.type, 'rental:auth-expired'); events++; } };
  try {
    const old = currentRequestIdentity();
    notifyUnauthorized(401, old, '/api/admin/instances');
    assert.equal(events, 1);
    resetRequestIdentity();
    notifyUnauthorized(401, old, '/api/admin/instances');
    notifyUnauthorized(500, currentRequestIdentity(), '/api/admin/instances');
    notifyUnauthorized(401, currentRequestIdentity(), '/api/auth/login');
    assert.equal(events, 1);
    notifyUnauthorized(401, currentRequestIdentity(), '/api/rental/state');
    assert.equal(events, 2);
  } finally { if (original === undefined) delete globalThis.window; else globalThis.window = original; }
});
