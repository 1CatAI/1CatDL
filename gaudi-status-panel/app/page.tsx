'use client';

import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  XAxis,
  YAxis,
} from 'recharts';
import {
  Activity,
  AlertTriangle,
  ArrowDownLeft,
  ArrowUpRight,
  Box,
  ChevronRight,
  CheckCircle2,
  CircuitBoard,
  Clock3,
  Cpu,
  Database,
  Gauge,
  Network,
  RefreshCw,
  Server,
  Thermometer,
  Zap,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { BrandLogo } from '@/components/brand-logo';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from '@/components/ui/chart';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';

type LinkState = 'up' | 'degraded' | 'down' | 'unknown';
type ConnectionState = 'connecting' | 'live' | 'stale' | 'offline';

type GaudiNode = {
  id: number;
  bdf: string;
  rdma: string;
  temperatureC: number;
  utilizationPct: number;
  memoryUsedMiB: number;
  memoryTotalMiB: number;
  powerW: number;
  eccCorrected: number;
  eccUncorrected: number;
  txGBps: number;
  rxGBps: number;
  internalPortsUp: number;
  internalPortsTotal: number;
  p9Up: boolean;
  status: 'operational' | 'warning' | 'offline';
};

type FabricEdge = {
  source: number;
  target: number;
  sourceToTargetGBps: number;
  targetToSourceGBps: number;
  totalGBps: number;
  portsUp: number;
  portsTotal: number;
  state: LinkState;
  hasP9: boolean;
};

type ElectricityMetrics = {
  status: 'live' | 'stale' | 'unavailable';
  detail: string;
  meterStartedAt: string | null;
  latestSampleAt: string | null;
  dataAgeSeconds: number | null;
  rateCnyPerKwh: number;
  currentW: number | null;
  hourlyCostCny: number | null;
  averageSinceStartW: number | null;
  averages: {
    power5hW: number | null;
    power24hW: number | null;
  };
  coverageSeconds24h: number;
  energy: {
    totalKwh: number;
    history24hKwh: number;
    forecast24hKwh: number;
    forecast30dKwh: number;
  };
  cost: {
    totalCny: number;
    history24hCny: number;
    forecast24hCny: number;
    forecast30dCny: number;
  };
  forecastBasis: string;
  source: string;
  history: Array<{
    timestamp: string;
    watts: number;
  }>;
  components: {
    gaudiBoardsW: number | null;
    cpuPackageW: number | null;
    knownSubtotalW: number | null;
    platformAndConversionW: number | null;
    knownSharePct: number | null;
  };
  meterHealth: {
    fresh: boolean;
    samplesOk: number;
    samplesFailed: number;
    gaps: number;
    measuredSeconds: number;
    unmeasuredSeconds: number;
  };
};

type DashboardSnapshot = {
  generatedAt: string;
  frameSequence: number;
  streamFpsTarget: number;
  collectorFps: number;
  frameIntervalMs: number;
  fabricSweepMs: number;
  sampleIntervalMs: number;
  source: string;
  host: {
    hostname: string;
    os: string;
    kernel: string;
    cpu: string;
    cpuThreads: number;
    memoryTotalGiB: number;
    memoryUsedGiB: number;
    rootTotalGiB: number;
    rootUsedGiB: number;
    driver: string;
    firmware: string;
    uptime: string;
    cpuUtilizationPct: number;
    cpuFrequencyMHz: number;
    cpuFrequencyMinMHz: number;
    cpuFrequencyMaxMHz: number;
    cpuPowerW: number | null;
    cpuPackagePowerW: number[];
    cpuPowerAvailable: boolean;
    cpuPowerSource: string;
    cpuSampleIntervalMs: number;
  };
  fleet: {
    online: number;
    total: number;
    linksUp: number;
    linksTotal: number;
    p9Up: number;
    aggregateTxGBps: number;
    aggregateRxGBps: number;
  };
  electricity: ElectricityMetrics;
  nodes: GaudiNode[];
  edges: FabricEdge[];
  alerts: Array<{
    level: 'info' | 'warning' | 'critical';
    title: string;
    detail: string;
  }>;
};

type RealtimeFrame = {
  sequence: number;
  generatedAt: string;
  collectorFps: number;
  streamFpsTarget: number;
  frameIntervalMs: number;
  fabricSweepMs: number;
  host: Pick<
    DashboardSnapshot['host'],
    | 'cpuUtilizationPct'
    | 'cpuFrequencyMHz'
    | 'cpuFrequencyMinMHz'
    | 'cpuFrequencyMaxMHz'
    | 'cpuPowerW'
    | 'cpuPackagePowerW'
    | 'cpuPowerAvailable'
    | 'cpuSampleIntervalMs'
  >;
  fleet: Pick<DashboardSnapshot['fleet'], 'aggregateTxGBps' | 'aggregateRxGBps'>;
  nodes: Array<Pick<GaudiNode, 'id' | 'txGBps' | 'rxGBps'>>;
  edges: FabricEdge[];
};

const BDFS = [
  '0000:43:00.0',
  '0000:44:00.0',
  '0000:19:00.0',
  '0000:1a:00.0',
  '0000:cc:00.0',
  '0000:cd:00.0',
  '0000:b3:00.0',
  '0000:b4:00.0',
];

const NODE_POSITIONS = [
  { x: 142, y: 91 }, { x: 142, y: 221 },
  { x: 142, y: 351 }, { x: 142, y: 481 },
  { x: 818, y: 91 }, { x: 818, y: 221 },
  { x: 818, y: 351 }, { x: 818, y: 481 },
];

const P9_PAIRS = new Set(['0-4', '1-5', '2-6', '3-7']);
const UI_RENDER_FPS = 8;
const UI_RENDER_INTERVAL_MS = 1000 / UI_RENDER_FPS;

function emptyEdges(): FabricEdge[] {
  const edges: FabricEdge[] = [];
  for (let source = 0; source < 8; source += 1) {
    for (let target = source + 1; target < 8; target += 1) {
      edges.push({
        source,
        target,
        sourceToTargetGBps: 0,
        targetToSourceGBps: 0,
        totalGBps: 0,
        portsUp: 0,
        portsTotal: 3,
        state: 'unknown',
        hasP9: P9_PAIRS.has(`${source}-${target}`),
      });
    }
  }
  return edges;
}

const EMPTY_ELECTRICITY: ElectricityMetrics = {
  status: 'unavailable',
  detail: '等待整机电表首个有效样本',
  meterStartedAt: null,
  latestSampleAt: null,
  dataAgeSeconds: null,
  rateCnyPerKwh: 1.1,
  currentW: null,
  hourlyCostCny: null,
  averageSinceStartW: null,
  averages: {
    power5hW: null,
    power24hW: null,
  },
  coverageSeconds24h: 0,
  energy: {
    totalKwh: 0,
    history24hKwh: 0,
    forecast24hKwh: 0,
    forecast30dKwh: 0,
  },
  cost: {
    totalCny: 0,
    history24hCny: 0,
    forecast24hCny: 0,
    forecast30dCny: 0,
  },
  forecastBasis: '等待整机电表首个有效区间',
  source: 'BMC/IPMI DCMI',
  history: [],
  components: {
    gaudiBoardsW: null,
    cpuPackageW: null,
    knownSubtotalW: null,
    platformAndConversionW: null,
    knownSharePct: null,
  },
  meterHealth: {
    fresh: false,
    samplesOk: 0,
    samplesFailed: 0,
    gaps: 0,
    measuredSeconds: 0,
    unmeasuredSeconds: 0,
  },
};

const ELECTRICITY_CHART_CONFIG = {
  watts: {
    label: '整机输入功率',
    color: '#315cf6',
  },
} satisfies ChartConfig;

const INITIAL_SNAPSHOT: DashboardSnapshot = {
  generatedAt: '',
  frameSequence: 0,
  streamFpsTarget: 24,
  collectorFps: 0,
  frameIntervalMs: 41.67,
  fabricSweepMs: 1167,
  sampleIntervalMs: 2000,
  source: '等待实时采集',
  host: {
    hostname: 'ymzx-Intel-001',
    os: 'Ubuntu 24.04.4 LTS',
    kernel: '6.8.0-138-generic',
    cpu: '2 × Intel Xeon Gold 6330',
    cpuThreads: 112,
    memoryTotalGiB: 503,
    memoryUsedGiB: 0,
    rootTotalGiB: 937,
    rootUsedGiB: 0,
    driver: '1.24.1-b336d5e',
    firmware: '62.6.2-sec-11',
    uptime: '—',
    cpuUtilizationPct: 0,
    cpuFrequencyMHz: 0,
    cpuFrequencyMinMHz: 0,
    cpuFrequencyMaxMHz: 0,
    cpuPowerW: null,
    cpuPackagePowerW: [],
    cpuPowerAvailable: false,
    cpuPowerSource: 'unavailable',
    cpuSampleIntervalMs: 42,
  },
  fleet: {
    online: 0,
    total: 8,
    linksUp: 0,
    linksTotal: 84,
    p9Up: 0,
    aggregateTxGBps: 0,
    aggregateRxGBps: 0,
  },
  electricity: EMPTY_ELECTRICITY,
  nodes: BDFS.map((bdf, id) => ({
    id,
    bdf,
    rdma: '—',
    temperatureC: 0,
    utilizationPct: 0,
    memoryUsedMiB: 0,
    memoryTotalMiB: 98304,
    powerW: 0,
    eccCorrected: 0,
    eccUncorrected: 0,
    txGBps: 0,
    rxGBps: 0,
    internalPortsUp: 0,
    internalPortsTotal: 21,
    p9Up: false,
    status: 'offline',
  })),
  edges: emptyEdges(),
  alerts: [
    {
      level: 'info',
      title: '正在连接采集器',
      detail: '等待采集器返回实时数据。',
    },
  ],
};

const gb = (mib: number) => mib / 1024;
const formatRate = (rate: number) =>
  rate >= 10 ? rate.toFixed(1) : rate.toFixed(2);
const formatWholePower = (watts: number | null) => {
  if (watts === null || !Number.isFinite(watts)) return 'N/A';
  return watts >= 1000 ? `${(watts / 1000).toFixed(2)} kW` : `${watts.toFixed(0)} W`;
};
const formatEnergy = (kwh: number) =>
  kwh >= 100 ? `${kwh.toFixed(1)} kWh` : `${kwh.toFixed(3)} kWh`;
const formatCost = (cny: number | null) =>
  cny === null || !Number.isFinite(cny) ? 'N/A' : `¥${cny.toFixed(2)}`;
const formatDuration = (seconds: number) =>
  seconds >= 3600 ? `${(seconds / 3600).toFixed(1)} h` : `${Math.round(seconds / 60)} min`;
const formatMeterTime = (value: string | null) => {
  if (!value) return '等待首个样本';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('zh-CN', { hour12: false });
};

function mergeRealtimeFrame(snapshot: DashboardSnapshot, frame: RealtimeFrame): DashboardSnapshot {
  const nodesById = new Map(frame.nodes.map((node) => [node.id, node]));
  const edgesByPair = new Map(frame.edges.map((edge) => [`${edge.source}-${edge.target}`, edge]));
  return {
    ...snapshot,
    generatedAt: frame.generatedAt,
    frameSequence: frame.sequence,
    streamFpsTarget: frame.streamFpsTarget,
    collectorFps: frame.collectorFps,
    frameIntervalMs: frame.frameIntervalMs,
    fabricSweepMs: frame.fabricSweepMs,
    sampleIntervalMs: frame.fabricSweepMs,
    host: { ...snapshot.host, ...frame.host },
    fleet: { ...snapshot.fleet, ...frame.fleet },
    nodes: snapshot.nodes.map((node) => ({ ...node, ...nodesById.get(node.id) })),
    edges: snapshot.edges.map((edge) => ({
      ...edge,
      ...edgesByPair.get(`${edge.source}-${edge.target}`),
    })),
  };
}

function stateColor(state: LinkState, rate: number) {
  if (state === 'down') return '#ff8795';
  if (state === 'degraded') return '#ffc56c';
  if (state === 'unknown') return '#2b364b';
  return rate > 0.02 ? '#87adff' : '#415372';
}

function stateLabel(state: ConnectionState) {
  if (state === 'live') return '实时连接';
  if (state === 'stale') return '数据延迟';
  if (state === 'offline') return '连接中断';
  return '正在连接';
}

function Kpi({
  icon, label, value, hint, tone = 'cyan',
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint: string;
  tone?: 'cyan' | 'green' | 'amber';
}) {
  const isRate = value.endsWith(' GB/s');
  return (
    <div className={`kpi-tile kpi-${tone}`}>
      <div className="kpi-top"><span className="kpi-icon">{icon}</span><span className="kpi-label">{label}</span></div>
      <div className="kpi-value">{isRate ? value.slice(0, -5) : value}{isRate && <small>GB/s</small>}</div>
      <div className="kpi-hint">{hint}</div>
    </div>
  );
}

// Original lightweight implementation; visual concept inspired by Magic UI Animated Beam.
// Fixed SVG geometry avoids per-frame layout reads, observers per edge, or a motion runtime.
function fabricPath(edge: FabricEdge) {
  const a = NODE_POSITIONS[edge.source];
  const b = NODE_POSITIONS[edge.target];
  const ax = a.x + (edge.source < 4 ? 100 : -100);
  const bx = b.x + (edge.target < 4 ? 100 : -100);
  if ((edge.source < 4) === (edge.target < 4)) {
    const inset = 90 + Math.abs(edge.target - edge.source) * 45;
    const control = ax + (edge.source < 4 ? inset : -inset);
    return `M ${ax} ${a.y} C ${control} ${a.y}, ${control} ${b.y}, ${bx} ${b.y}`;
  }
  return `M ${ax} ${a.y} C 420 ${a.y}, 540 ${b.y}, ${bx} ${b.y}`;
}

function FabricGraph({
  snapshot, selected, onSelect, live,
}: {
  snapshot: DashboardSnapshot;
  selected: number;
  onSelect: (id: number) => void;
  live: boolean;
}) {
  const regionRef = useRef<HTMLDivElement>(null);
  const [motionEnabled, setMotionEnabled] = useState(true);
  const [visible, setVisible] = useState(false);
  const [motionAllowed, setMotionAllowed] = useState(false);

  useEffect(() => {
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const syncMotion = () => setMotionAllowed(!document.hidden && !preference.matches);
    syncMotion();
    document.addEventListener('visibilitychange', syncMotion);
    preference.addEventListener('change', syncMotion);
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { threshold: 0.05 });
    if (regionRef.current) observer.observe(regionRef.current);
    return () => {
      observer.disconnect();
      document.removeEventListener('visibilitychange', syncMotion);
      preference.removeEventListener('change', syncMotion);
    };
  }, []);

  const animate = motionEnabled && motionAllowed && visible && live;
  const orderedEdges = [...snapshot.edges].sort((a, b) =>
    Number(a.source === selected || a.target === selected) - Number(b.source === selected || b.target === selected));

  return (
    <div ref={regionRef} className={`fabric-wrap ${snapshot.generatedAt ? '' : 'awaiting-data'}`}>
      <div className="fabric-toolbar">
        <span><i className="fabric-online-dot" />{live ? '实时链路吞吐' : '等待实时连接'}<small>GB/s</small></span>
        <label htmlFor="fabric-motion" className="motion-control"><Switch id="fabric-motion" size="sm" checked={motionEnabled} onCheckedChange={setMotionEnabled} aria-label="连线流动效果" /><span>流动效果</span></label>
      </div>
      <div className="fabric-scroll">
        <svg className="fabric-svg" viewBox="0 0 960 572" aria-label="8 张 Gaudi2 的全互联拓扑与实时吞吐">
          <title>8 张 Gaudi2 的逻辑全互联，左右分组仅为显示布局，不表示物理分组</title>
          <defs>
            <pattern id="fabric-grid" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".8" fill="#28364f" /></pattern>
          </defs>
          <rect width="960" height="572" fill="url(#fabric-grid)" opacity=".65" />
          <text x="480" y="24" textAnchor="middle" className="fabric-map-caption">28 NODE PAIRS / ALL-TO-ALL</text>
          {orderedEdges.map((edge) => {
            const highlighted = selected === edge.source || selected === edge.target;
            const path = fabricPath(edge);
            const unhealthy = edge.state === 'down' || edge.state === 'degraded';
            const flowing = animate && highlighted && edge.state === 'up' && edge.totalGBps > 0.02;
            return (
              <g key={`${edge.source}-${edge.target}`}>
                <path d={path} fill="none" stroke={stateColor(edge.state, edge.totalGBps)}
                  strokeOpacity={highlighted || unhealthy ? .9 : .25}
                  strokeWidth={highlighted ? Math.min(3.5, 1.4 + edge.totalGBps / 16) : 1}
                  strokeDasharray={edge.state === 'down' ? '5 6' : undefined}>
                  <title>{`M${edge.source} ↔ M${edge.target} · ${formatRate(edge.sourceToTargetGBps)} / ${formatRate(edge.targetToSourceGBps)} GB/s · ${edge.portsUp}/${edge.portsTotal} 端口在线${edge.hasP9 ? ' · 含 p9' : ''}`}</title>
                </path>
                {flowing && <path d={path} pathLength="100" fill="none" stroke="#d6e6ff" strokeWidth="2.5" strokeLinecap="round" strokeDasharray="4 96" className="fabric-beam" aria-hidden="true" />}
                {edge.hasP9 && <g opacity={highlighted ? 1 : .55}>
                  <rect x="473" y={NODE_POSITIONS[edge.source].y - 7} width="14" height="14" rx="4" fill={edge.state === 'up' ? '#d8eea1' : edge.state === 'unknown' ? '#536077' : '#ffc56c'} stroke="#0c1426" strokeWidth="3" />
                </g>}
              </g>
            );
          })}
          {snapshot.nodes.map((node) => {
            const { x, y } = NODE_POSITIONS[node.id];
            const isSelected = node.id === selected;
            const known = Boolean(snapshot.generatedAt);
            const state = !known ? '#7888a6' : node.status === 'operational' ? '#bde596' : node.status === 'warning' ? '#ffc56c' : '#ff8795';
            return (
              <a key={node.id} href={`#module-${node.id}`} aria-label={`选择模块 M${node.id}`} aria-current={isSelected ? 'true' : undefined}
                onClick={(event) => { event.preventDefault(); onSelect(node.id); }}>
                <g className={`node-group ${isSelected ? 'node-selected' : ''}`} transform={`translate(${x - 100} ${y - 49})`}>
                  <rect className="node-outer" x="-4" y="-4" width="208" height="106" rx="18" fill="none" stroke={isSelected ? '#577dcd' : 'transparent'} strokeOpacity=".45" />
                  <rect className="node-surface" width="200" height="98" rx="14" fill={isSelected ? '#233c6b' : '#152039'} stroke={!known ? '#2c3953' : node.status === 'offline' ? '#a95868' : isSelected ? '#8cb3ff' : '#34435f'} />
                  <text x="17" y="29" className="node-title">M{node.id.toString().padStart(2, '0')}</text>
                  <circle cx="80" cy="24" r="3" fill={state} />
                  <text x="183" y="28" textAnchor="end" className="node-temp">{known ? `${node.temperatureC}°C` : '—'}</text>
                  <line x1="17" y1="42" x2="183" y2="42" stroke="#7a95c3" strokeOpacity=".2" />
                  <text x="17" y="61" className="node-label">TX ↑</text>
                  <text x="110" y="61" className="node-label">RX ↓</text>
                  <text x="17" y="84" className="node-rate node-rate-tx">{known ? formatRate(node.txGBps) : '—'}</text>
                  <text x="110" y="84" className="node-rate node-rate-rx">{known ? formatRate(node.rxGBps) : '—'}</text>
                  {known && !node.p9Up && <text x="105" y="28" className="p9-text">p9 !</text>}
                </g>
              </a>
            );
          })}
          <text x="480" y="553" textAnchor="middle" className="fabric-map-caption">LOGICAL FABRIC · 每对 3 × 100GbE</text>
        </svg>
      </div>
      <div className="fabric-legend">
        <span><i className="legend-dot legend-active" />有流量</span>
        <span><i className="legend-dot legend-idle" />空闲</span>
        <span><i className="legend-dot legend-error" />链路异常</span>
        <span><i className="legend-dot legend-p9" />含 p9</span>
        <small>流动仅示意活动，不表示方向或时延</small>
      </div>
    </div>
  );
}

function ModuleCard({ node, selected, onClick }: { node: GaudiNode; selected: boolean; onClick: () => void }) {
  const memoryPct = node.memoryTotalMiB ? (node.memoryUsedMiB / node.memoryTotalMiB) * 100 : 0;
  const stateClass = node.status === 'operational' ? 'module-online' : node.status === 'warning' ? 'module-warning' : 'module-offline';

  return (
    <button type="button" id={`module-${node.id}`} aria-pressed={selected} onClick={onClick} className={`module-card ${selected ? 'module-selected' : ''}`}>
      <div className="module-topline">
        <span className="module-name"><i className={stateClass} />M<span>{node.id.toString().padStart(2, '0')}</span></span>
        <span className="module-temp">{node.temperatureC || '—'}°C</span>
      </div>
      <div className="module-bdf">HL-225 <span>{node.bdf.replace('0000:', '')}</span></div>
      <div className="module-rates">
        <span><ArrowUpRight />{formatRate(node.txGBps)}</span>
        <span><ArrowDownLeft />{formatRate(node.rxGBps)}</span>
        <small>GB/s</small>
      </div>
      <Progress value={memoryPct} className="module-progress" />
      <div className="module-meta">
        <span>HBM {gb(node.memoryUsedMiB).toFixed(1)} / {gb(node.memoryTotalMiB).toFixed(0)} GiB</span>
        <span>链路 {node.internalPortsUp}/{node.internalPortsTotal}</span>
      </div>
    </button>
  );
}

const ElectricityPanel = memo(function ElectricityPanel({
  metrics,
}: {
  metrics: ElectricityMetrics;
}) {
  const coveragePct = Math.min(100, (metrics.coverageSeconds24h / 86400) * 100);
  const statusLabel =
    metrics.status === 'live'
      ? '计量正常'
      : metrics.status === 'stale'
        ? '样本过期'
        : '等待采样';
  const dataAge =
    metrics.dataAgeSeconds === null
      ? '暂无样本'
      : metrics.dataAgeSeconds < 90
        ? `${metrics.dataAgeSeconds.toFixed(0)} 秒前`
        : `${(metrics.dataAgeSeconds / 60).toFixed(1)} 分钟前`;
  const sourceLabel =
    metrics.source === 'ipmi-dcmi'
      ? 'BMC DCMI · 整机输入'
      : metrics.source === 'ipmi-sensor-pw-consumption'
        ? 'BMC PW Consumption · 整机输入'
        : metrics.source;

  return (
    <section id="energy" className="energy-section" aria-label="整机电量与电费统计">
      <div className="energy-title-row">
        <div>
          <span className="section-kicker">02 / ENERGY</span>
          <h2>整机电量与电费</h2>
        </div>
        <div className="energy-title-meta">
          <span className={`energy-status energy-status-${metrics.status}`}>
            <i />
            {statusLabel}
          </span>
          <span>{sourceLabel}</span>
          <span>暂按 ¥{metrics.rateCnyPerKwh.toFixed(2)} / 度</span>
        </div>
      </div>

      <Card className="energy-card">
        <CardContent className="energy-content">
          <div className="energy-now">
            <span className="energy-now-label">BMC 当前整机输入功率</span>
            <strong>{formatWholePower(metrics.currentW)}</strong>
            <div className="energy-now-cost">
              <Zap size={15} />
              <span>当前每小时约</span>
              <b>{formatCost(metrics.hourlyCostCny)}</b>
            </div>
            <div className="energy-now-details">
              <div>
                <span>累计平均</span>
                <strong>{formatWholePower(metrics.averageSinceStartW)}</strong>
              </div>
              <div>
                <span>最新采样</span>
                <strong>{dataAge}</strong>
              </div>
            </div>
          </div>

          <div className="energy-chart-wrap">
            <div className="energy-chart-heading">
              <div>
                <span>POWER HISTORY</span>
                <strong>最近 24 小时 BMC 输入功率</strong>
              </div>
              <small>覆盖 {formatDuration(metrics.coverageSeconds24h)} · {coveragePct.toFixed(1)}%</small>
            </div>
            {metrics.history.length > 1 ? (
              <ChartContainer
                config={ELECTRICITY_CHART_CONFIG}
                className="energy-chart"
                initialDimension={{ width: 520, height: 190 }}
              >
                <AreaChart
                  accessibilityLayer
                  data={metrics.history}
                  margin={{ top: 12, right: 12, bottom: 0, left: 0 }}
                >
                  <defs>
                    <linearGradient id="electricityFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="var(--color-watts)" stopOpacity={0.17} />
                      <stop offset="95%" stopColor="var(--color-watts)" stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid vertical={false} strokeDasharray="3 5" />
                  <XAxis
                    dataKey="timestamp"
                    axisLine={false}
                    tickLine={false}
                    minTickGap={42}
                    tickFormatter={(value: string) => {
                      const date = new Date(value);
                      return Number.isNaN(date.getTime())
                        ? value
                        : date.toLocaleTimeString('zh-CN', {
                            hour: '2-digit',
                            minute: '2-digit',
                            hour12: false,
                          });
                    }}
                  />
                  <YAxis
                    axisLine={false}
                    tickLine={false}
                    width={56}
                    tickFormatter={(value: number) =>
                      value >= 1000 ? `${(value / 1000).toFixed(1)}kW` : `${value}W`
                    }
                  />
                  <ChartTooltip
                    cursor={false}
                    content={<ChartTooltipContent indicator="line" />}
                  />
                  <Area
                    dataKey="watts"
                    type="monotone"
                    fill="url(#electricityFill)"
                    fillOpacity={1}
                    stroke="var(--color-watts)"
                    strokeWidth={2}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ChartContainer>
            ) : (
              <div className="energy-chart-empty">
                <Activity size={23} />
                <span>正在积累功率历史，两个有效样本后显示曲线</span>
              </div>
            )}
          </div>

          <div className="energy-metrics">
            <div>
              <span>累计用电</span>
              <strong>{formatEnergy(metrics.energy.totalKwh)}</strong>
              <small>自 {formatMeterTime(metrics.meterStartedAt)}</small>
            </div>
            <div>
              <span>累计电费</span>
              <strong>{formatCost(metrics.cost.totalCny)}</strong>
              <small>按当前电价重算</small>
            </div>
            <div>
              <span>近 24 小时</span>
              <strong>{formatEnergy(metrics.energy.history24hKwh)}</strong>
              <small>{formatCost(metrics.cost.history24hCny)}</small>
            </div>
            <div>
              <span>未来 24 小时预计</span>
              <strong>{formatEnergy(metrics.energy.forecast24hKwh)}</strong>
              <small>{formatCost(metrics.cost.forecast24hCny)}</small>
            </div>
            <div>
              <span>30 天预计</span>
              <strong>{formatEnergy(metrics.energy.forecast30dKwh)}</strong>
              <small>{formatCost(metrics.cost.forecast30dCny)}</small>
            </div>
            <div>
              <span>Gaudi 8 卡板载</span>
              <strong>{formatWholePower(metrics.components.gaudiBoardsW)}</strong>
              <small>hl-smi · 54V + 12V</small>
            </div>
            <div>
              <span>CPU Package</span>
              <strong>{formatWholePower(metrics.components.cpuPackageW)}</strong>
              <small>Intel RAPL · 不含 DRAM</small>
            </div>
            <div>
              <span>平台 / 风扇 / 转换</span>
              <strong>{formatWholePower(metrics.components.platformAndConversionW)}</strong>
              <small>
                已知项占整机 {metrics.components.knownSharePct?.toFixed(1) ?? '—'}%
              </small>
            </div>
            <div>
              <span>样本健康</span>
              <strong>{metrics.meterHealth.samplesOk} 成功</strong>
              <small>{metrics.meterHealth.samplesFailed} 失败 · {metrics.meterHealth.gaps} 缺口</small>
            </div>
          </div>
        </CardContent>
        <div className="energy-footer">
          <span>{metrics.detail}</span>
          <span>{metrics.forecastBasis}</span>
          <span>BMC 输入含内存、主板、风扇和电源转换损耗</span>
          <span>未计量 {formatDuration(metrics.meterHealth.unmeasuredSeconds)}</span>
        </div>
      </Card>
    </section>
  );
});

export default function Home() {
  const [snapshot, setSnapshot] = useState(INITIAL_SNAPSHOT);
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [selected, setSelected] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const lastSuccessRef = useRef<number | null>(null);
  const pendingPayloadRef = useRef<string | null>(null);

  const fetchStatus = useCallback(async (showSpinner = false) => {
    if (showSpinner) setRefreshing(true);
    try {
      const response = await fetch('/api/status', {
        cache: 'no-store',
        signal: AbortSignal.timeout(3500),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const received = (await response.json()) as DashboardSnapshot;
      const receivedElectricity = received.electricity;
      const next: DashboardSnapshot = {
        ...received,
        electricity: receivedElectricity
          ? {
              ...receivedElectricity,
              components:
                receivedElectricity.components ?? EMPTY_ELECTRICITY.components,
            }
          : EMPTY_ELECTRICITY,
      };
      setSnapshot(next);
      const successTime = Date.now();
      lastSuccessRef.current = successTime;
      setConnection('live');
    } catch {
      setConnection((current) => (lastSuccessRef.current ? 'stale' : current === 'connecting' ? 'connecting' : 'offline'));
    } finally {
      if (showSpinner) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void fetchStatus(), 0);
    const timer = window.setInterval(() => {
      if (!document.hidden) void fetchStatus();
    }, 5000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [fetchStatus]);

  useEffect(() => {
    let source: EventSource | null = null;
    let disposed = false;

    const closeStream = () => {
      const activeSource = source;
      source = null;
      pendingPayloadRef.current = null;
      if (!activeSource) return;
      activeSource.onopen = null;
      activeSource.onmessage = null;
      activeSource.onerror = null;
      activeSource.close();
    };

    const connectStream = () => {
      if (disposed || document.hidden || source) return;
      const nextSource = new EventSource('/api/stream');
      source = nextSource;
      nextSource.onopen = () => setConnection('live');
      nextSource.onmessage = (event) => {
        pendingPayloadRef.current = event.data;
        lastSuccessRef.current = Date.now();
      };
      nextSource.onerror = () => {
        if (document.hidden) return;
        const lastSuccess = lastSuccessRef.current;
        setConnection(lastSuccess && Date.now() - lastSuccess < 3000 ? 'stale' : 'offline');
      };
    };

    const renderTimer = window.setInterval(() => {
      if (document.hidden) return;
      const payload = pendingPayloadRef.current;
      if (!payload) return;
      pendingPayloadRef.current = null;
      try {
        const frame = JSON.parse(payload) as RealtimeFrame;
        setSnapshot((current) => mergeRealtimeFrame(current, frame));
        setConnection('live');
      } catch {
        setConnection('stale');
      }
    }, UI_RENDER_INTERVAL_MS);

    const handleVisibility = () => {
      if (document.hidden) {
        closeStream();
        return;
      }
      setConnection('connecting');
      void fetchStatus();
      connectStream();
    };

    connectStream();
    document.addEventListener('visibilitychange', handleVisibility);
    const watchdog = window.setInterval(() => {
      if (document.hidden) return;
      const lastSuccess = lastSuccessRef.current;
      if (!lastSuccess) {
        setConnection('offline');
        return;
      }
      if (Date.now() - lastSuccess > 3000) setConnection('stale');
    }, 1000);
    return () => {
      disposed = true;
      document.removeEventListener('visibilitychange', handleVisibility);
      closeStream();
      window.clearInterval(renderTimer);
      window.clearInterval(watchdog);
    };
  }, [fetchStatus]);

  const selectedNode = snapshot.nodes.find((node) => node.id === selected) ?? snapshot.nodes[0];
  const activeAlerts = snapshot.alerts.filter((alert) => alert.level !== 'info');
  const displayTime = useMemo(() => {
    if (!snapshot.generatedAt) return '等待首帧';
    const date = new Date(snapshot.generatedAt);
    return Number.isNaN(date.getTime()) ? snapshot.generatedAt : date.toLocaleTimeString('zh-CN', { hour12: false });
  }, [snapshot.generatedAt]);

  return (
    <TooltipProvider>
      <div className="dashboard-app" data-design-version="v3-20260905">
      <aside className="sidebar">
        <a className="rail-brand" href="#overview" aria-label="1CatDL 看板首页"><BrandLogo compact /></a>
        <nav aria-label="看板分区">
          <a href="#fabric"><Network size={21} /><span>互联</span></a>
          <a href="#energy"><Zap size={21} /><span>电费</span></a>
          <a href="#modules"><CircuitBoard size={21} /><span>设备</span></a>
        </nav>
        <div className="rail-bottom"><Server size={19} /><span>HLS2</span><small>V.03</small></div>
      </aside>
      <main id="overview" className="dashboard-shell">
        <a href="#fabric" className="skip-link">跳到互联状态</a>
        <header className="topbar">
          <div className="brand-lockup">
            <strong className="brand-wordmark"><BrandLogo /><span>CONTROL ROOM</span></strong>
          </div>

          <div className="host-pill">
            <Server size={15} /><span>计算节点</span><ChevronRight size={14} />
            <strong>{snapshot.host.hostname}</strong>
          </div>

          <div className="topbar-actions">
            <Tooltip>
              <TooltipTrigger render={<button type="button" className={`live-badge live-${connection}`}><span />{stateLabel(connection)}</button>} />
              <TooltipContent>最近数据：{displayTime} · 页面 {UI_RENDER_FPS} FPS · 矩阵一轮 {snapshot.fabricSweepMs} ms</TooltipContent>
            </Tooltip>
            <Button variant="outline" size="sm" className="refresh-button" disabled={refreshing} onClick={() => void fetchStatus(true)} aria-label="立即刷新">
              <RefreshCw size={16} className={refreshing ? 'spin' : ''} />
              <span>刷新</span>
            </Button>
          </div>
        </header>

        <div className="page-heading">
          <div><div className="section-kicker">COMPUTE / 01</div><h1>Gaudi2 <span>计算节点</span><Badge className="version-badge">HLS2</Badge></h1><p>{snapshot.host.hostname} <span>·</span> 8 × HL-225 <span>·</span> 768 GiB HBM</p></div>
          <div className="heading-context"><span><Clock3 size={14} />运行 {snapshot.host.uptime}</span><span>最后更新 <time>{displayTime}</time></span></div>
        </div>

        <section className="kpi-grid" aria-label="系统总览">
          <Kpi icon={<CircuitBoard size={19} />} label="在线加速卡" value={snapshot.generatedAt ? `${snapshot.fleet.online} / ${snapshot.fleet.total}` : '— / 8'} hint="Intel Gaudi2 · 768 GiB HBM" tone="green" />
          <Kpi icon={<Network size={19} />} label="内部链路" value={snapshot.generatedAt ? `${snapshot.fleet.linksUp} / ${snapshot.fleet.linksTotal}` : '— / 84'} hint="28 组节点对 · 每对 3 × 100GbE" />
          <Kpi icon={<ArrowUpRight size={19} />} label="互联合计发送" value={`${formatRate(snapshot.fleet.aggregateTxGBps)} GB/s`} hint="TX · 每卡 21 个内部端口" tone="green" />
          <Kpi icon={<ArrowDownLeft size={19} />} label="互联合计接收" value={`${formatRate(snapshot.fleet.aggregateRxGBps)} GB/s`} hint="RX · 当前节点接收吞吐" />
        </section>

        <section id="fabric" className="main-grid" aria-label="互联与节点详情">
          <Card className="fabric-card">
            <CardHeader className="panel-header">
              <div>
                <CardDescription className="section-kicker">01 / INTERCONNECT</CardDescription>
                <CardTitle><h2>全互联拓扑 <span>All-to-All</span></h2></CardTitle>
              </div>
              <div className="panel-header-meta">
                <Badge className={snapshot.fleet.p9Up === 8 ? 'badge-good' : 'badge-warn'}><Activity size={13} />p9 {snapshot.generatedAt ? snapshot.fleet.p9Up : '—'}/8</Badge>
              </div>
            </CardHeader>
            <CardContent className="fabric-content">
              <FabricGraph snapshot={snapshot} selected={selected} onSelect={setSelected} live={connection === 'live'} />
            </CardContent>
            <div className="fabric-bottom"><span><i />已选 <b>M{selected.toString().padStart(2, '0')}</b> <span className="fabric-bottom-divider">/</span> 7 组相邻互联</span><span>点击节点切换 · 布局为逻辑示意</span></div>
          </Card>

          <div className="right-rail">
            <Card className="detail-card">
              <CardHeader className="detail-header">
                <div>
                  <CardDescription className="section-kicker">SELECTED DEVICE</CardDescription>
                  <CardTitle><h2>设备详情 <span>M{selectedNode.id.toString().padStart(2, '0')}</span></h2></CardTitle>
                </div>
                <Badge className={selectedNode.status === 'operational' ? 'badge-good' : 'badge-warn'}>{!snapshot.generatedAt ? '连接中' : selectedNode.status === 'operational' ? '运行正常' : selectedNode.status === 'offline' ? '离线' : '需留意'}</Badge>
              </CardHeader>
              <CardContent>
                <div className="detail-hero">
                  <div className="device-icon">{selectedNode.id.toString().padStart(2, '0')}</div>
                  <div className="detail-address">
                    <small>PCIe 地址</small>
                    <strong>{selectedNode.bdf}</strong>
                    <span>{selectedNode.rdma} · Module {selectedNode.id}</span>
                  </div>
                </div>

                <div className="detail-rate-grid">
                  <div><ArrowUpRight /><small>TX</small><strong>{formatRate(selectedNode.txGBps)}</strong><span>GB/s</span></div>
                  <div><ArrowDownLeft /><small>RX</small><strong>{formatRate(selectedNode.rxGBps)}</strong><span>GB/s</span></div>
                </div>

                <div className="metric-list">
                  <div><span><Database />HBM</span><strong>{gb(selectedNode.memoryUsedMiB).toFixed(1)} / {gb(selectedNode.memoryTotalMiB).toFixed(0)} GiB</strong></div>
                  <Progress value={selectedNode.memoryTotalMiB ? (selectedNode.memoryUsedMiB / selectedNode.memoryTotalMiB) * 100 : 0} className="detail-progress" />
                  <div><span><Gauge />计算占用</span><strong>{selectedNode.utilizationPct}%</strong></div>
                  <div><span><Thermometer />芯片温度</span><strong>{selectedNode.temperatureC || '—'}°C</strong></div>
                  <div><span><Zap />板卡功耗</span><strong>{selectedNode.powerW || '—'} W</strong></div>
                  <div><span><Network />内部端口</span><strong>{selectedNode.internalPortsUp} / {selectedNode.internalPortsTotal}</strong></div>
                  <div className="ecc-row"><span>ECC 已纠正 / 未纠正</span><strong>{selectedNode.eccCorrected} / {selectedNode.eccUncorrected}</strong></div>
                </div>
              </CardContent>
            </Card>

            <Card className="host-card">
              <CardHeader className="compact-header">
                <div>
                  <CardDescription className="section-kicker">HOST PLATFORM</CardDescription>
                  <CardTitle><h2>CPU 与主机</h2></CardTitle>
                </div>
                <Server size={20} />
              </CardHeader>
              <div className="cpu-load-track" aria-label={`CPU 占用 ${snapshot.host.cpuUtilizationPct.toFixed(1)}%`}>
                <Progress value={snapshot.host.cpuUtilizationPct} />
              </div>
              <div className="cpu-strip">
                <div><span>CPU 占用</span><strong>{snapshot.host.cpuUtilizationPct.toFixed(1)}<small>%</small></strong></div>
                <div title={`${(snapshot.host.cpuFrequencyMinMHz / 1000).toFixed(2)}–${(snapshot.host.cpuFrequencyMaxMHz / 1000).toFixed(2)} GHz`}><span>平均频率</span><strong>{snapshot.host.cpuFrequencyMHz ? (snapshot.host.cpuFrequencyMHz / 1000).toFixed(2) : '—'}<small>GHz</small></strong></div>
                <div title={snapshot.host.cpuPowerSource}><span>CPU 功耗</span><strong>{snapshot.host.cpuPowerAvailable && snapshot.host.cpuPowerW !== null ? snapshot.host.cpuPowerW.toFixed(0) : '—'}<small>W</small></strong></div>
              </div>
              <CardContent className="host-specs">
                <div><Cpu /><span>CPU</span><strong>{snapshot.host.cpu}</strong></div>
                <div><Box /><span>OS</span><strong>{snapshot.host.os}</strong></div>
                <div><Database /><span>内存</span><strong>{snapshot.host.memoryUsedGiB.toFixed(1)} / {snapshot.host.memoryTotalGiB.toFixed(0)} GiB</strong></div>
                <div><CircuitBoard /><span>Driver</span><strong>{snapshot.host.driver}</strong></div>
              </CardContent>
            </Card>
          </div>
        </section>

        <ElectricityPanel metrics={snapshot.electricity} />

        <section id="modules" className="modules-section">
          <div className="section-title-row">
            <div><span className="section-kicker">03 / DEVICES</span><h2>全部加速卡</h2></div>
            <span className="module-hint">点击模块可联动拓扑与详情</span>
          </div>
          <div className="module-grid">
            {snapshot.nodes.map((node) => <ModuleCard key={node.id} node={node} selected={selected === node.id} onClick={() => setSelected(node.id)} />)}
          </div>
        </section>

        <footer className="status-footer">
          <div className={activeAlerts.length || connection !== 'live' ? 'footer-alert' : 'footer-ok'}>
            {activeAlerts.length || connection !== 'live' ? <AlertTriangle size={16} /> : <CheckCircle2 size={16} />}
            <strong>{connection !== 'live' ? stateLabel(connection) : activeAlerts.length ? `${activeAlerts.length} 项告警` : '状态正常'}</strong>
            <span>{connection !== 'live' ? '当前显示最近收到的数据' : activeAlerts[0]?.detail ?? `${snapshot.fleet.online} 张卡在线 · 正在监控 p9 链路`}</span>
          </div>
          <div className="footer-source">
            <Tooltip><TooltipTrigger render={<button type="button" className="telemetry-info">采集 {snapshot.collectorFps.toFixed(1)} FPS · 界面 {UI_RENDER_FPS} FPS</button>} /><TooltipContent>{snapshot.source} · 矩阵一轮 {snapshot.fabricSweepMs} ms · Kernel {snapshot.host.kernel} · FW {snapshot.host.firmware}</TooltipContent></Tooltip>
            <span>1CAT / FABRIC V.03</span>
          </div>
        </footer>
      </main>
      </div>
    </TooltipProvider>
  );
}
