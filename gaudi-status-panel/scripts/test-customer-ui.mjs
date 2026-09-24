import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { customerSearch, filterCustomerInstances, readCustomerTab, remainingTime } from '../components/rental/customer-model.ts';

const row = (id, state, extra = {}) => ({ id: String(id), name: `demo-${id}`, state, nodeId: 'G2-002', mode: 'gpu', ...extra });
const rows = [row(1, 'running'), row(2, 'stopped'), row(3, 'starting'), row(4, 'error'), row(5, 'running', { mode: 'headless', nodeId: 'G2-003' }), row(6, 'repair_required'), row(7, 'stopped', { nodeOnline: false })];
const ids = (query = '', filter = 'all') => filterCustomerInstances(rows, query, filter).map(row => row.id);
test('customer page selection survives URL reloads', () => {
  for (const tab of ['instances', 'create', 'wallet']) assert.equal(readCustomerTab(customerSearch(tab, ''), 'customer'), tab);
});
test('customer URLs cannot select the administrator workspace', () => assert.equal(readCustomerTab('?view=admin&tab=admin', 'customer'), 'instances'));
test('administrator retains existing landing and explicit workspace routing', () => {
  assert.equal(readCustomerTab('', 'admin'), 'admin');
  assert.equal(readCustomerTab('?view=workspace&tab=wallet', 'admin'), 'wallet');
});
test('unknown tabs have a safe fallback', () => assert.equal(readCustomerTab('?tab=anything', 'customer'), 'instances'));
test('customer navigation preserves unrelated URL state', () => {
  const query = new URLSearchParams(customerSearch('wallet', '?section=nodes&node=G2-003'));
  assert.equal(query.get('node'), 'G2-003');
  assert.equal(query.get('view'), 'workspace');
});
test('instance search matches explicit ID and node case-insensitively', () => {
  assert.deepEqual(ids(' #1 '), ['1']);
  assert.deepEqual(ids('g2-003'), ['5']);
});
test('mode search includes headless instances', () => assert.deepEqual(ids('无头'), ['5']));
test('state filters separate running, stopped and transitional instances', () => {
  assert.deepEqual(ids('', 'running'), ['1', '5']);
  assert.deepEqual(ids('', 'stopped'), ['2', '7']);
  assert.deepEqual(ids('', 'pending'), ['3']);
});
test('attention includes error, repair and offline node', () => assert.deepEqual(ids('', 'attention'), ['4', '6', '7']));
test('composed filters and empty search do not mutate original order', () => {
  const original = structuredClone(rows);
  assert.deepEqual(ids('g2-003', 'stopped'), []);
  assert.deepEqual(ids('missing'), []);
  assert.deepEqual(rows, original);
});
test('balance estimate never promises negative or infinite runtime', () => {
  assert.equal(remainingTime(-10, 400), '约 0 分钟');
  assert.equal(remainingTime(200, 400), '约 30 分钟');
  assert.equal(remainingTime(600, 400), '约 1.5 小时');
  assert.equal(remainingTime(19200, 400), '约 2.0 天');
  assert.equal(remainingTime(200, 0), '暂无持续扣费');
  assert.equal(remainingTime(NaN, 400), '暂无持续扣费');
});

const source = await readFile(new URL('../components/rental/rental-panel.tsx', import.meta.url), 'utf8');
const view = await readFile(new URL('../components/rental/customer-workspace.tsx', import.meta.url), 'utf8');
const css = await readFile(new URL('../components/rental/customer-workspace.css', import.meta.url), 'utf8');
test('UI delegates ordering to existing idempotent API path', () => {
  assert.match(source, /'X-Idempotency-Key': orderKey.current/);
  assert.match(source, /orderIntent.current !== intent/);
  assert.match(view, /if \(o.canOrder && !loading\) o.onOrder\(\)/);
});
test('creation permission and GPU-start capacity remain separate', () => {
  assert.match(source, /selectedPlacement\?\.allowed.*selectedPlacement\?\.createAvailable/);
  assert.match(source, /gpuCapacityReason\(state, instance.nodeId, instance.gpuCount \?\? 1\)/);
  assert.match(view, /创建不自动开机、不预占 GPU/);
});
test('existing instance GPU switch is a stopped-only inline action with no eight-card upgrade', () => {
  assert.match(source, /instance\.state === 'stopped' && !instance\.owner && <button/);
  assert.match(source, /更改配置/);
  assert.match(source, /\(\[1, 4\] as const\)\.map\(option/);
  assert.match(source, /targetCount === count \|\| targetCount === 8/);
  assert.match(source, /\/api\/rental\/instances\/\$\{encodeURIComponent\(id\)\}\/gpu-plan/);
  assert.match(source, /原八卡赠送的 600 GiB 数据盘原容量保留，继续免费/);
  assert.match(css, /\.customer-plan-change/);
});
test('credentials remain opt-in and masked with expansion state', () => {
  assert.match(source, /\[showPassword, setShowPassword\] = useState\(false\)/);
  assert.match(source, /aria-expanded=\{connectionOpen\}/);
  assert.match(source, /running && connectionOpen && !instance.owner/);
  assert.match(source, /showPassword \? instance.password/);
});
test('critical pricing and deletion conditions remain in customer UI', () => {
  assert.match(view, /关机后仍收费/);
  assert.match(view, /关机后保留 48 小时/);
  assert.match(source, /无法恢复。请先备份重要数据/);
  assert.match(source, /type="submit" disabled=\{submitting \|\| !name.trim\(\) \|\| !password\}/);
});
test('resource overview no longer renders other instance identifiers', () => assert.doesNotMatch(view, /slot\.instanceId/));
test('list rendering is bounded and hidden customer view stays hidden', () => {
  assert.match(source, /visibleInstances.slice\(\(currentInstancePage - 1\) \* 10, currentInstancePage \* 10\)/);
  assert.match(source, /const displayed = visible.slice\(\(current - 1\) \* 20, current \* 20\)/);
  assert.match(css, /\.customer-ui\[hidden\] \{ display: none !important/);
});
test('new customer surface adds no costly animation or decorative layers', () => assert.doesNotMatch(css, /backdrop-filter|filter:\s*blur|transition:\s*all|animation:/));
