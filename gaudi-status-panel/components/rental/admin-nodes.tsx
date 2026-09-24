'use client';

import { Activity, ArrowUpRight, Check, ChevronDown, Cpu, HardDrive, MemoryStick, Server, TriangleAlert, Zap } from 'lucide-react';
import type { HostTelemetry, RentalState } from './rental-panel';
import { formatDate, stateLabel } from './admin-model';
import { buildPowerChart, finiteValue, formatBytes, formatCost, formatCoverage, formatPercent, formatPower, telemetryIsFresh } from './node-telemetry';

type Node = NonNullable<RentalState['nodes']>[number];
type Props = {
  nodes: Node[]; slots: RentalState['slots']; telemetryByNode: Record<string, HostTelemetry>;
  selectedId: string; error: string; loading: boolean;
  onSelect: (id: string) => void; onInstance: (id: string) => void; onViewInstances: (id: string) => void;
};

function Status({ online, stale = false }: { online: boolean; stale?: boolean }) {
  return <span className={`node-status ${!online ? 'is-offline' : stale ? 'is-stale' : 'is-online'}`}><i aria-hidden="true" />{!online ? '离线' : stale ? '待更新' : '在线'}</span>;
}

function Meter({ label, value, detail, warning = false }: { label: string; value: number | null | undefined; detail?: string; warning?: boolean }) {
  return <div className={`node-meter ${warning ? 'is-warning' : ''}`}>
    <div className="node-meter-label"><span>{label}</span><strong>{formatPercent(value)}</strong></div>
    <div className="node-meter-track" role={finiteValue(value) ? 'meter' : 'img'} aria-label={finiteValue(value) ? label : `${label}：暂无数据`} aria-valuemin={finiteValue(value) ? 0 : undefined} aria-valuemax={finiteValue(value) ? 100 : undefined} aria-valuenow={finiteValue(value) ? Math.max(0, Math.min(100, value)) : undefined}><span style={{ width: `${finiteValue(value) ? Math.max(0, Math.min(100, value)) : 0}%` }} /></div>
    {detail && <small>{detail}</small>}
  </div>;
}

function PowerChart({ points }: { points: Array<{ timestamp: string; watts: number }> }) {
  const chart = buildPowerChart(points);
  if (!chart) return <div className="node-chart-empty"><Activity size={20} /><span>正在积累功耗记录</span><small>取得连续样本后将在这里显示曲线。</small></div>;
  const time = (value: number) => new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
  return <figure className="node-chart">
    <div className="node-chart-plot"><div className="node-chart-y" aria-hidden="true"><span className="node-chart-unit">kW</span>{chart.ticks.map(tick => <span key={tick.watts} style={{ top: `${tick.y / 180 * 100}%` }}>{(tick.watts / 1000).toFixed(1)}</span>)}</div><svg viewBox="54 0 622 180" preserveAspectRatio="none" aria-label={`整机功耗，从 ${time(chart.first)} 到 ${time(chart.last)}，纵轴 0 到 ${chart.ceiling} W`}>
      <title>整机功耗，按实际采集时间排列</title>
      {chart.ticks.map(tick => <line key={tick.watts} x1="54" x2="676" y1={tick.y} y2={tick.y} />)}
      <path className="node-chart-line" d={chart.path} />
    </svg></div><div className="node-chart-times" aria-hidden="true"><span>{time(chart.first)}</span><span>{time((chart.first + chart.last) / 2)}</span><span>{time(chart.last)}</span></div>
    {chart.gapCount > 0 && <figcaption>采样中断的区间已断开显示。</figcaption>}
  </figure>;
}

function PowerPanel({ telemetry, fresh }: { telemetry?: HostTelemetry; fresh: boolean }) {
  const power = telemetry?.power;
  const hasSamples = !!power?.samplesOk;
  const live = fresh && power?.status === 'live';
  const hasIssue = !!power && (power.status === 'stale' || power.samplesFailed > 0 || power.gaps > 0);
  return <section className="node-energy" aria-labelledby="node-energy-title">
    <div className="node-section-heading"><h3 id="node-energy-title"><Zap size={17} />整机功耗与电费</h3><span className="node-secondary">{power?.source === 'ipmi-dcmi' ? 'BMC DCMI · 整机输入实测' : power?.source || '等待电表'}</span></div>
    <div className="node-power-readings">
      <div><span>当前整机功耗</span><strong>{formatPower(power?.currentW)}</strong><small>{live ? '实时采样' : hasSamples ? '上次读数 · 非实时' : '等待首个样本'}</small></div>
      <div><span>近 24 小时平均</span><strong>{formatPower(power?.average24hW)}</strong><small>{formatCoverage(power?.coverageSeconds24h)}</small></div>
    </div>
    <PowerChart points={power?.history24h ?? []} />
    <div className="node-costs">
      <div><span>历史电费</span><strong>{formatCost(hasSamples ? power?.totalCostCny : null)}</strong><small>自开始采集累计</small></div>
      <div><span>近 24 小时电费</span><strong>{formatCost(hasSamples ? power?.cost24hCny : null)}</strong><small>按有效样本统计</small></div>
      <div><span>预计 30 天电费</span><strong>{formatCost(power?.forecast30dCny)}</strong><small>按当前平均功耗估算</small></div>
    </div>
    <div className="node-energy-note"><span>电价 {formatCost(power?.rateCnyPerKwh)} / 度</span><span>{(power?.coverageSeconds24h ?? 0) < 86_000 ? '不足 24 小时，均值与预估基于已有样本' : '统计窗口：最近 24 小时'}</span></div>
    {hasIssue && <p className="node-inline-warning"><TriangleAlert size={15} />{power?.status === 'stale' ? '电表样本已过期，请检查采集状态。' : '存在采样失败或缺口，电费仅累计有效区间。'}</p>}
    <details className="node-diagnostics"><summary>采集详情<ChevronDown size={14} /></summary><dl><div><dt>最近采集</dt><dd>{formatDate(power?.lastSampleAt)}</dd></div><div><dt>开始采集</dt><dd>{formatDate(power?.firstSampleAt)}</dd></div><div><dt>累计用电</dt><dd>{finiteValue(power?.totalKwh) ? `${power.totalKwh.toFixed(3)} 度` : '—'}</dd></div><div><dt>成功 / 失败 / 缺口</dt><dd>{power?.samplesOk ?? 0} / {power?.samplesFailed ?? 0} / {power?.gaps ?? 0}</dd></div></dl></details>
  </section>;
}

export function AdminNodes({ nodes, slots, telemetryByNode, selectedId, error, loading, onSelect, onInstance, onViewInstances }: Props) {
  const selected = nodes.find(node => node.id === selectedId) ?? nodes[0];
  if (!selected) return <div className="node-empty"><Server size={24} /><h2>{loading ? '正在读取节点' : '暂无节点数据'}</h2><p>{loading ? '资源与监控信息正在加载。' : '请使用页面右上角的刷新重试。'}</p></div>;
  const telemetry = telemetryByNode[selected.id] ?? selected.telemetry;
  const fresh = !error && telemetryIsFresh(telemetry, selected.online);
  const cpu = telemetry?.cpu, memory = telemetry?.memory, swap = telemetry?.swap;
  const selectedSlots = slots.filter(slot => (slot.nodeId || 'G2-002') === selected.id);
  const free = selectedSlots.filter(slot => slot.state === 'available').length;
  const storage = selected.storage;
  const storagePct = storage && storage.totalGiB > 0 ? storage.usedGiB / storage.totalGiB * 100 : null;
  return <div className="admin-nodes">
    {error && <div className="node-inline-warning" role="alert"><TriangleAlert size={16} /><span>监控更新失败，当前显示上次数据。请刷新重试。</span></div>}
    <section className="node-comparison" aria-label="节点资源对照">
      <div className="node-comparison-heading"><h2>节点概览</h2><span>{nodes.filter(node => node.online).length} / {nodes.length} 在线 · 选择节点查看详情</span></div>
      <div className="node-comparison-scroll"><table className="node-comparison-table"><thead><tr><th scope="col">节点</th><th scope="col">GPU 可用</th><th scope="col">CPU</th><th scope="col">内存</th><th scope="col">Swap</th><th scope="col">整机功耗</th><th scope="col">存储用量</th></tr></thead><tbody>{nodes.map(node => {
        const metrics = telemetryByNode[node.id] ?? node.telemetry;
        const nodeSlots = slots.filter(slot => (slot.nodeId || 'G2-002') === node.id);
        const nodeFresh = !error && telemetryIsFresh(metrics, node.online);
        const isSelected = node.id === selected.id;
        return <tr key={node.id} data-selected={isSelected}>
          <th scope="row"><button className="node-select" onClick={() => onSelect(node.id)} aria-pressed={isSelected} aria-controls="node-detail"><span><Server size={17} /><strong>{node.id}</strong>{isSelected && <Check size={15} aria-label="已选中" />}</span><Status online={node.online} stale={!nodeFresh} /></button>{node.online && (!node.imageReady || node.storage?.lowSpace) && <small className="node-row-alert">{[!node.imageReady && '镜像未就绪', node.storage?.lowSpace && '存储不足'].filter(Boolean).join(' · ')}</small>}</th>
          <td data-label="GPU 可用"><strong>{node.online ? nodeSlots.filter(slot => slot.state === 'available').length : '—'}<span className="node-secondary"> / {nodeSlots.length}</span></strong></td>
          <td data-label="CPU"><Meter label={`${node.id} CPU`} value={metrics?.cpu?.usagePct} /></td>
          <td data-label="内存"><Meter label={`${node.id} 内存`} value={metrics?.memory?.usagePct} /></td>
          <td data-label="Swap"><Meter label={`${node.id} Swap`} value={metrics?.swap?.usagePct} warning={(metrics?.swap?.usagePct ?? 0) >= 70} /></td>
          <td data-label="整机功耗"><strong>{formatPower(metrics?.power?.currentW)}</strong>{!nodeFresh || metrics?.power?.status !== 'live' ? <small className="node-secondary">{metrics?.power?.currentW == null ? '等待采样' : '上次读数'}</small> : null}</td>
          <td data-label="存储用量"><Meter label={`${node.id} 存储`} value={node.storage && node.storage.totalGiB > 0 ? node.storage.usedGiB / node.storage.totalGiB * 100 : null} warning={node.storage?.lowSpace} /></td>
        </tr>;
      })}</tbody></table></div>
    </section>

    <section className="node-detail" id="node-detail" aria-labelledby="node-detail-title">
      <header className="node-detail-heading"><div><h2 id="node-detail-title">{selected.id}<Status online={selected.online} stale={!fresh} /></h2><p>{selected.online ? fresh ? '主机资源与实例分配' : '正在等待最新监控，已有数值为上次记录' : '节点离线，以下保留最后记录'}</p></div><button className="rental-small-button" onClick={() => onViewInstances(selected.id)}>查看节点实例<ArrowUpRight size={15} /></button></header>
      <dl className="node-hardware"><div><dt><Cpu size={16} />CPU 型号</dt><dd>{cpu?.model || '等待主机信息'}<small>{cpu ? `${cpu.sockets} 路 · ${cpu.threads} 逻辑线程` : '—'}</small></dd></div><div><dt><MemoryStick size={16} />内存规格</dt><dd>{formatBytes(memory?.totalBytes)}<small>{memory?.speedMTs ? `${memory.speedMTs} MT/s` : '频率待获取'}{memory?.modules ? ` · ${memory.modules} 条` : ''}</small></dd></div><div><dt>基础镜像</dt><dd>{selected.imageReady ? '就绪' : '未就绪'}<small>可启动无头实例 {selected.online ? selected.headlessAvailable : '—'} 台</small></dd></div></dl>
      <div className="node-detail-columns">
        <div className="node-resources">
          <section aria-labelledby="node-resources-title"><div className="node-section-heading"><h3 id="node-resources-title">资源使用</h3><span className="node-secondary">{fresh ? '实时' : '上次记录'}</span></div><div className="node-resource-meters"><Meter label="CPU 使用量" value={cpu?.usagePct} detail={cpu ? `${cpu.threads} 逻辑线程` : '等待采样'} /><Meter label="内存使用量" value={memory?.usagePct} detail={memory ? `${formatBytes(memory.usedBytes)} / ${formatBytes(memory.totalBytes)}` : '等待采样'} /><Meter label="Swap 使用量" value={swap?.usagePct} detail={swap ? swap.totalBytes ? `${formatBytes(swap.usedBytes)} / ${formatBytes(swap.totalBytes)}` : '未配置 Swap' : '等待采样'} warning={(swap?.usagePct ?? 0) >= 70} /></div></section>
          <section className="node-gpus" aria-labelledby="node-gpus-title"><div className="node-section-heading"><h3 id="node-gpus-title">GPU 分配</h3><span>{selected.online ? `${free} / ${selectedSlots.length} 可用` : '节点离线'}</span></div><div className="node-gpu-grid">{selectedSlots.map(slot => <div className="node-gpu" data-state={slot.state} key={slot.slot}><strong>GPU {String(slot.slot).padStart(2, '0')}</strong><span className="node-gpu-state"><i aria-hidden="true" />{stateLabel(slot.state)}</span><div className="node-gpu-assignment">{slot.instanceId ? <button className="node-instance-link" onClick={() => onInstance(slot.instanceId!)} aria-label={`查看 GPU ${slot.slot} 的实例 #${slot.instanceId}`}>实例 #{slot.instanceId}<ArrowUpRight size={13} /></button> : <span>{slot.state === 'available' ? '未分配实例' : '暂无实例信息'}</span>}</div></div>)}</div>{!selectedSlots.length && <p className="node-secondary">暂无 GPU 清单，请刷新核对。</p>}</section>
          <section className="node-storage" aria-labelledby="node-storage-title"><div className="node-section-heading"><h3 id="node-storage-title"><HardDrive size={17} />实例存储池</h3><span className="node-secondary">{storage ? `${storage.totalGiB.toLocaleString('zh-CN', { maximumFractionDigits: 1 })} GiB 总容量` : '等待存储信息'}</span></div><Meter label="实际使用" value={storagePct} warning={storage?.lowSpace} detail={storage ? `已用 ${storage.usedGiB.toLocaleString('zh-CN', { maximumFractionDigits: 1 })} GiB` : '暂无数据'} /><dl className="node-storage-details"><div><dt>物理剩余</dt><dd>{storage ? `${storage.freeGiB.toLocaleString('zh-CN', { maximumFractionDigits: 1 })} GiB` : '—'}</dd></div><div><dt>规格预留</dt><dd>{storage ? `${storage.reservedGiB} / ${storage.budgetGiB} GiB` : '—'}</dd></div></dl>{storage?.lowSpace && <p className="node-inline-warning"><TriangleAlert size={15} />存储空间不足或节点不可用，新建及开机受限。</p>}</section>
        </div>
        <PowerPanel telemetry={telemetry} fresh={fresh} />
      </div>
    </section>
  </div>;
}
