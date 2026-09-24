import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildPowerChart, formatPercent, telemetryIsFresh } from '../components/rental/node-telemetry.ts';

const start = Date.parse('2026-09-22T08:00:00Z');
const sample = (seconds, watts = 1700) => ({ timestamp: new Date(start + seconds * 1000).toISOString(), watts });
test('chart positions readings by elapsed time, not their index', () => {
  const chart = buildPowerChart([sample(0), sample(10), sample(40)]);
  assert.equal(chart.path.split(' ')[1].split(',')[0], 'L209.50');
});
test('long missing intervals do not become a continuous line', () => {
  const chart = buildPowerChart([sample(0), sample(10), sample(20), sample(1000), sample(1010)]);
  assert.equal(chart.gapCount, 1);
  assert.equal((chart.path.match(/M/g) || []).length, 2);
});
test('chart ignores invalid values and deduplicates timestamps', () => {
  assert.equal(buildPowerChart([sample(0), sample(0, 1800), sample(10, NaN), { timestamp: 'bad', watts: 1 }]), null);
  const chart = buildPowerChart([sample(10), sample(0), sample(10, 1900)]);
  assert.equal(chart.first, start);
  assert.equal(chart.last, start + 10000);
  assert(!/NaN|Infinity/.test(chart.path));
});
test('idle constant readings retain a nonzero scale with zero baseline', () => {
  const chart = buildPowerChart([sample(0, 0), sample(10, 0)]);
  assert(chart.ceiling > 0);
  assert.equal(chart.ticks[0].watts, 0);
});
test('offline, stale and future telemetry are never labelled live', () => {
  const telemetry = { status: 'live', observedAt: new Date(start).toISOString() };
  assert.equal(telemetryIsFresh(telemetry, true, start + 5000), true);
  assert.equal(telemetryIsFresh(telemetry, false, start + 5000), false);
  assert.equal(telemetryIsFresh(telemetry, true, start + 46000), false);
  assert.equal(telemetryIsFresh(telemetry, true, start - 61000), false);
  assert.equal(telemetryIsFresh(undefined, true, start), false);
});
test('unknown percentages are distinct from a measured zero', () => {
  assert.equal(formatPercent(NaN), '—');
  assert.equal(formatPercent(null), '—');
  assert.equal(formatPercent(0), '0.0%');
});
