import type { HostTelemetry } from './rental-panel';

export const finiteValue = (value: number | null | undefined): value is number => typeof value === 'number' && Number.isFinite(value);

export function formatPercent(value: number | null | undefined) {
  return finiteValue(value) ? `${value.toFixed(1)}%` : '—';
}

export function formatBytes(value: number | null | undefined) {
  if (!finiteValue(value)) return '—';
  return `${(value / 1024 ** 3).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} GiB`;
}

export function formatPower(value: number | null | undefined) {
  if (!finiteValue(value)) return '—';
  return value >= 1000 ? `${(value / 1000).toFixed(2)} kW` : `${value.toFixed(0)} W`;
}

export function formatCost(value: number | null | undefined) {
  return finiteValue(value) ? `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—';
}

export function formatCoverage(seconds: number | undefined) {
  if (!finiteValue(seconds) || seconds <= 0) return '等待有效采样';
  return seconds < 3600 ? `已采集 ${Math.max(1, Math.floor(seconds / 60))} 分钟` : `已采集 ${(seconds / 3600).toFixed(1)} 小时`;
}

export function telemetryIsFresh(telemetry: HostTelemetry | undefined, online: boolean, now = Date.now()) {
  const observed = Date.parse(telemetry?.observedAt ?? '');
  return online && telemetry?.status !== 'unavailable' && Number.isFinite(observed) && now - observed <= 45_000 && now - observed >= -60_000;
}

export function buildPowerChart(points: Array<{ timestamp: string; watts: number }>) {
  const samples = [...new Map(points.filter(point => finiteValue(point.watts) && point.watts >= 0 && Number.isFinite(Date.parse(point.timestamp)))
    .map(point => [Date.parse(point.timestamp), { time: Date.parse(point.timestamp), watts: point.watts }])).values()].sort((a, b) => a.time - b.time);
  if (samples.length < 2) return null;
  const first = samples[0].time, last = samples[samples.length - 1].time;
  const step = Math.max(100, Math.ceil(Math.max(...samples.map(point => point.watts)) / 4 / 100) * 100);
  const ceiling = step * 4;
  const intervals = samples.slice(1).map((point, i) => point.time - samples[i].time).sort((a, b) => a - b);
  const typicalInterval = intervals[Math.floor((intervals.length - 1) / 2)];
  const gapLimit = Math.max(60_000, Math.min(1_800_000, typicalInterval * 3));
  const x = (time: number) => 54 + (time - first) / (last - first) * 622;
  const y = (watts: number) => 166 - watts / ceiling * 148;
  let gapCount = 0;
  const path = samples.map((point, i) => {
    const gap = i > 0 && point.time - samples[i - 1].time > gapLimit;
    if (gap) gapCount++;
    return `${i === 0 || gap ? 'M' : 'L'}${x(point.time).toFixed(2)},${y(point.watts).toFixed(2)}`;
  }).join(' ');
  return { path, first, last, ceiling, gapCount, ticks: [0, 1, 2, 3, 4].map(i => ({ watts: i * step, y: y(i * step) })), lastPoint: { x: x(last), y: y(samples[samples.length - 1].watts) } };
}
