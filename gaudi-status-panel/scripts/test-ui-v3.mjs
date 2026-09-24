import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import ts from 'typescript';

// Pure source/component-tree tests, not browser or hardware rendering tests.
const source = await readFile(new URL('../app/page.tsx', import.meta.url), 'utf8');
const css = await readFile(new URL('../app/globals.css', import.meta.url), 'utf8');
const require = createRequire(import.meta.url);
let states = [];
const react = {
  ...require('react'),
  memo: (component) => component,
  useRef: (current) => ({ current }),
  useState: (initial) => [states.length ? states.shift() : initial, () => {}],
  useEffect: () => {},
};
const testModule = { exports: {} };
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText;
vm.runInNewContext(compiled + '\nmodule.exports.test = { emptyEdges, fabricPath, mergeRealtimeFrame, INITIAL_SNAPSHOT, FabricGraph, Kpi };', {
  exports: testModule.exports, module: testModule,
  require: (name) => name === 'react' ? react : name === 'react/jsx-runtime' ? require(name) : new Proxy({}, { get: () => () => null }),
});
const api = testModule.exports.test;
const edges = api.emptyEdges();
assert.equal(edges.length, 28);
assert.equal(new Set(edges.map((e) => `${e.source}-${e.target}`)).size, 28);
assert.equal(edges.filter((e) => e.hasP9).length, 4);
for (const edge of edges) {
  assert.equal(edge.portsTotal, 3);
  assert.match(api.fabricPath(edge), /^M [\d.]+ [\d.]+ C /);
  assert.doesNotMatch(api.fabricPath(edge), /NaN|undefined/);
}
const snapshot = structuredClone(api.INITIAL_SNAPSHOT);
snapshot.generatedAt = '2026-09-05T00:00:00Z';
snapshot.edges = edges.map((edge) => ({ ...edge, state: 'up', totalGBps: 1, portsUp: 3 }));
snapshot.nodes = snapshot.nodes.map((node) => ({ ...node, status: 'operational', p9Up: true }));
function walk(element, callback) {
  if (Array.isArray(element)) { element.forEach((item) => walk(item, callback)); return; }
  if (!element || typeof element !== 'object' || !element.props) return;
  callback(element);
  walk(element.props.children, callback);
}
function graphCounts(data, hookStates, live = true, selected = 0) {
  states = [...hookStates];
  const tree = api.FabricGraph({ snapshot: data, selected, onSelect() {}, live });
  const counts = { nodes: 0, beams: 0, links: 0 };
  walk(tree, (element) => {
    if (element.type === 'a') counts.nodes++;
    if (element.props.className === 'fabric-beam') counts.beams++;
    else if (element.type === 'path') counts.links++;
  });
  return counts;
}
for (let selected = 0; selected < 8; selected++) {
  assert.deepEqual(graphCounts(snapshot, [true, true, true], true, selected), { nodes: 8, beams: 7, links: 28 });
}
assert.equal(graphCounts(snapshot, [false, true, true]).beams, 0, 'manual motion off');
assert.equal(graphCounts(snapshot, [true, false, true]).beams, 0, 'out of view');
assert.equal(graphCounts(snapshot, [true, true, false]).beams, 0, 'hidden or reduced motion');
assert.equal(graphCounts(snapshot, [true, true, true], false).beams, 0, 'stale/disconnected');
const idle = { ...snapshot, edges: snapshot.edges.map((edge) => ({ ...edge, totalGBps: 0 })) };
assert.equal(graphCounts(idle, [true, true, true]).beams, 0, 'no invented traffic');
const broken = { ...snapshot, edges: snapshot.edges.map((edge) => ({ ...edge, state: 'down' })) };
assert.equal(graphCounts(broken, [true, true, true]).beams, 0, 'fault links never flow');
const frame = { sequence: 2, generatedAt: '2026-09-05T00:00:01Z', host: { cpuPowerW: 120 }, fleet: { aggregateTxGBps: 2 }, nodes: [{ id: 0, txGBps: 2, rxGBps: 1 }], edges: [] };
const merged = api.mergeRealtimeFrame(snapshot, frame);
assert.equal(merged.electricity, snapshot.electricity, 'fast frames retain memoized electricity reference');
assert.equal(merged.nodes[0].txGBps, 2);
assert.equal(snapshot.nodes[0].txGBps, 0, 'no snapshot mutation');
assert.equal(merged.nodes[0].bdf, snapshot.nodes[0].bdf);
assert.equal(merged.host.cpuPowerW, 120);
assert.equal(merged.edges.length, 28);
assert.match(source, /const UI_RENDER_FPS = 8/);
assert.match(source, /pendingPayloadRef.current = event.data/);
assert.match(source, /observer.disconnect\(\)/);
assert.match(source, /preference.removeEventListener\('change'/);
assert.match(source, /document.removeEventListener\('visibilitychange', syncMotion\)/);
assert.match(source, /activeSource.close\(\)/);
assert.match(source, /isAnimationActive=\{false\}/);
assert.match(css, /prefers-reduced-motion: reduce/);
assert.doesNotMatch(css, /backdrop-filter|filter:\s*blur|transition:\s*all/);
assert.doesNotMatch(source, /requestAnimationFrame|<canvas|Math.random/);
console.log(JSON.stringify({ status: 'passed', nodeSelections: 8, nodePairs: 28, maximumBeams: 7, pauseConditions: 6, electricityReferencePreserved: true, uiFpsCap: 8, browserTest: false }));
