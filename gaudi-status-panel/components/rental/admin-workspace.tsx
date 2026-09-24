'use client';

import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from 'react';
import { ArrowUpRight, CheckCircle2, ChevronLeft, ChevronRight, CircleHelp, Cpu, LogOut, RefreshCw, Search, Server, Settings, ShieldCheck, Users, Wallet, X } from 'lucide-react';
import { AdminNodes } from './admin-nodes';
import { gpuCapacityReason } from './gpu-plans';
import { BrandLogo } from '@/components/brand-logo';
import type { AdminCustomer, HostTelemetry, RentalAccount, RentalInstance, RentalState } from './rental-panel';
import { adminDefaults, adminRequest, adminRequestKey, adminSearch, filterAdminInstances, formatDate, formatMoney, isAttention, moneyToCents, readAdminRoute, stateLabel, type AdminRoute } from './admin-model';

export type AdminAccountProps = {
  active: boolean; revision: number; query: string; status: string;
  onQuery: (value: string) => void; onStatus: (value: string) => void; onReset: () => void; onChanged: () => void;
  onRecharge: (customer: AdminCustomer) => void; onInstances: (owner: string) => void; onLedger: (owner: string) => void;
};
type Props = {
  account: RentalAccount; state: RentalState; active: boolean; onRefresh: () => void;
  onLogout: () => void; onCustomerView: () => void;
  accounts: (props: AdminAccountProps) => ReactNode;
  codes: (active: boolean, revision: number) => ReactNode;
  ledger: (owner: string, revision: number, active: boolean) => ReactNode;
  settings: ReactNode;
};
type Notice = { error?: boolean; text: string } | null;
type Operation = { row: RentalInstance; operation: 'start' | 'stop' | 'delete'; mode?: 'gpu' | 'headless' };
type NodeTelemetryResponse = { nodes: Array<{ id: string; online: boolean; telemetry: HostTelemetry }>; updatedAt: string };
const sections = [
  { id: 'instances', label: '实例', icon: Server, description: '查看运行状态，处理实例与资源问题。' },
  { id: 'customers', label: '客户', icon: Users, description: '找到客户后，直接充值或查看实例与账单。' },
  { id: 'finance', label: '财务', icon: Wallet, description: '充值码、账户流水和管理操作记录。' },
  { id: 'nodes', label: '节点', icon: Cpu, description: '比较主机资源，查看实例分配与整机能耗。' },
  { id: 'settings', label: '设置', icon: Settings, description: '调整优先节点、新开机价格与新客户注册赠金。' },
] as const;

export function AdminWorkspace({ account, state, active, onRefresh, onLogout, onCustomerView, accounts, codes, ledger, settings }: Props) {
  const [route, setRoute] = useState<AdminRoute>(adminDefaults);
  const [fleet, setFleet] = useState<RentalState | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState<Notice>(null);
  const [recharge, setRecharge] = useState<AdminCustomer | null>(null);
  const [detailsId, setDetailsId] = useState<string | null>(null);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [busy, setBusy] = useState(false);
  const [telemetryByNode, setTelemetryByNode] = useState<Record<string, HostTelemetry>>({});
  const [telemetryError, setTelemetryError] = useState('');
  const inFlight = useRef(false);
  const rechargeKeys = useRef(new Map<string, string>());
  const [page, setPage] = useState(1);
  useEffect(() => {
    const restore = () => { setRoute(readAdminRoute(location.search)); setPage(1); };
    restore(); window.addEventListener('popstate', restore);
    return () => window.removeEventListener('popstate', restore);
  }, []);
  const navigate = (patch: Partial<AdminRoute>, push = false) => {
    const next = { ...route, ...patch };
    setRoute(next); setPage(1); setNotice(null);
    const url = `${location.pathname}${adminSearch(next, location.search)}${location.hash}`;
    if (push) history.pushState(null, '', url); else history.replaceState(null, '', url);
  };
  useEffect(() => {
    if (!active) return;
    let disposed = false, fetching = false;
    const abort = new AbortController();
    const load = async () => {
      if (document.hidden || fetching) return;
      fetching = true;
      try {
        const data = await adminRequest<RentalState>('/api/admin/instances', { signal: abort.signal });
        if (!disposed && data.account.name !== account.name) { window.dispatchEvent(new Event('rental:auth-expired')); return; }
        if (!disposed) { setFleet(data); setError(''); }
      } catch (e) { if (!disposed) setError(e instanceof Error ? e.message : '资源更新失败'); }
      finally { fetching = false; if (!disposed) setLoading(false); }
    };
    void load(); const timer = setInterval(() => void load(), 10000);
    const visible = () => { if (!document.hidden) void load(); };
    document.addEventListener('visibilitychange', visible);
    return () => { disposed = true; abort.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [account.name, active, revision]);
  useEffect(() => {
    if (!active || route.section !== 'nodes') return;
    let disposed = false, fetching = false;
    const abort = new AbortController();
    const load = async () => {
      if (document.hidden || fetching) return;
      fetching = true;
      try {
        const data = await adminRequest<NodeTelemetryResponse>('/api/admin/nodes', { signal: abort.signal });
        if (!disposed) {
          setTelemetryByNode(Object.fromEntries(data.nodes.map(node => [node.id, node.telemetry])));
          setTelemetryError('');
        }
      } catch (e) {
        if (!disposed) setTelemetryError(e instanceof Error ? e.message : '主机监控更新失败');
      } finally { fetching = false; }
    };
    void load();
    const timer = setInterval(() => void load(), 5_000);
    const visible = () => { if (!document.hidden) void load(); };
    document.addEventListener('visibilitychange', visible);
    return () => { disposed = true; abort.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [active, route.section, revision]);
  const changed = () => { setRevision(value => value + 1); onRefresh(); };
  const resource = fleet ?? state;
  const rows = fleet?.instances ?? [];
  const nodes = resource.nodes ?? [];
  const available = resource.slots.filter(slot => slot.state === 'available').length;
  const running = rows.filter(row => row.state === 'running');
  const attention = rows.filter(isAttention).length;
  const filtered = filterAdminInstances(rows, route);
  const pageCount = Math.max(1, Math.ceil(filtered.length / 15));
  const currentPage = Math.min(page, pageCount);
  const visibleRows = filtered.slice((currentPage - 1) * 15, currentPage * 15);
  const current = sections.find(item => item.id === route.section)!;
  const details = rows.find(row => row.id === detailsId);
  const showInstances = (owner: string) => navigate({ section: 'instances', owner, q: '', node: 'all', status: 'all', mode: 'all' }, true);
  const showLedger = (owner: string) => navigate({ section: 'finance', finance: 'ledger', owner }, true);
  const startReason = (row: RentalInstance, mode: 'gpu' | 'headless') => {
    const node = nodes.find(item => item.id === (row.nodeId || 'G2-002'));
    if (error || !fleet) return '状态未更新，请先刷新';
    if (resource.service === 'maintenance') return '平台维护中';
    if (row.nodeOnline === false || (node && (!node.online || !node.imageReady))) return '节点或镜像暂不可用';
    if (node?.storage?.lowSpace) return '节点存储不足';
    if (mode === 'headless') return (node?.headlessAvailable ?? resource.headless?.available ?? 0) <= 0 ? '无头资源不足' : '';
    if (rows.some(other => other.id !== row.id && other.owner === row.owner && other.mode !== 'headless' && ['running', 'creating', 'starting', 'stopping', 'repair_required'].includes(other.state))) return '该客户已有占用 GPU 的实例';
    return gpuCapacityReason(resource, row.nodeId, row.gpuCount ?? 1);
  };
  const perform = async () => {
    if (!operation || inFlight.current) return;
    inFlight.current = true; setBusy(true); setNotice(null);
    const target = operation;
    try {
      await adminRequest(`/api/admin/instances/${encodeURIComponent(target.row.id)}/${target.operation}`, { method: 'POST', body: JSON.stringify(target.operation === 'start' ? { mode: target.mode } : {}) });
      setOperation(null); setNotice({ text: `实例 #${target.row.id} 的${target.operation === 'start' ? '开机' : target.operation === 'stop' ? '关机' : '释放'}请求已提交，请以更新后的状态为准。` }); changed();
    } catch (e) { setOperation(null); setNotice({ error: true, text: e instanceof Error ? e.message : '操作失败' }); changed(); }
    finally { inFlight.current = false; setBusy(false); }
  };
  const actions = (row: RentalInstance) => <div className="admin-row-actions">
    <button className="admin-link" onClick={() => setDetailsId(row.id)} aria-label={`查看实例 #${row.id} 详情`}>详情</button>
    {['running', 'creating', 'starting', 'repair_required'].includes(row.state) && <button className="rental-small-button" disabled={busy || !!error} onClick={() => setOperation({ row, operation: 'stop' })}>{row.state === 'running' ? '关机' : row.state === 'repair_required' ? '尝试回收' : '取消开机'}</button>}
    {['stopped', 'error'].includes(row.state) && <button className="rental-small-button" disabled={busy || !!startReason(row, 'gpu')} title={startReason(row, 'gpu')} onClick={() => setOperation({ row, operation: 'start', mode: 'gpu' })}>{row.gpuCount ?? 1}卡开机</button>}
    {['stopped', 'error', 'repair_required'].includes(row.state) && <details className="admin-more"><summary aria-label={`实例 #${row.id} 更多操作`}>更多</summary><div>
      {row.state !== 'repair_required' && <button disabled={busy || !!startReason(row, 'headless')} title={startReason(row, 'headless')} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); setOperation({ row, operation: 'start', mode: 'headless' }); }}>无头开机 · ¥0.08/h</button>}
      <button className="admin-danger" disabled={busy || !!error} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); setOperation({ row, operation: 'delete' }); }}>永久释放实例</button>
    </div></details>}
  </div>;

  return <main className="admin-app"><div className="admin-layout">
    <aside className="admin-sidebar"><div className="admin-brand"><BrandLogo /><strong>1CatDL <span>管理后台</span></strong></div>
      <nav className="admin-nav" aria-label="管理员导航">{sections.map(item => <button key={item.id} aria-current={route.section === item.id ? 'page' : undefined} onClick={() => navigate({ section: item.id }, true)}><item.icon size={18} />{item.label}{item.id === 'instances' && attention > 0 && <span className="admin-badge">{attention}</span>}</button>)}</nav>
      <div className="admin-nav-footer"><span><ShieldCheck size={15} />{account.name}</span><button onClick={onCustomerView}><ArrowUpRight size={16} />客户视图</button><button onClick={onLogout}><LogOut size={16} />退出登录</button></div>
    </aside>
    <div className="admin-content">
      <header className="admin-topbar"><div className="admin-heading"><span>管理后台 / {current.label}</span><h1>{current.label === '实例' ? '实例管理' : current.label === '客户' ? '客户管理' : current.label}</h1><p>{current.description}</p></div><div className="admin-topbar-actions"><span className="admin-muted">{fleet ? `更新于 ${new Date(fleet.updatedAt).toLocaleTimeString('zh-CN', { hour12: false })}` : '连接中…'}</span><button className="rental-small-button" onClick={changed}><RefreshCw size={15} />刷新</button></div></header>
      {error && <div className="admin-notice is-error" role="alert">更新失败，显示的状态可能已过期：{error}<button onClick={changed}>重试</button></div>}
      {notice && <div className={`admin-notice ${notice.error ? 'is-error' : 'is-success'}`} role={notice.error ? 'alert' : 'status'}>{notice.error ? <CircleHelp size={17} /> : <CheckCircle2 size={17} />}<span>{notice.text}</span><button aria-label="关闭管理提示" onClick={() => setNotice(null)}><X size={16} /></button></div>}
      {resource.service === 'maintenance' && <output className="admin-notice">{resource.serviceMessage}</output>}
      {route.section === 'instances' && <>
        <div className="admin-stats">
          <div className="admin-stat"><span>可用 GPU</span><strong>{fleet ? available : '—'}<small> / {fleet ? resource.slots.length : '—'}</small></strong><small>{nodes.filter(node => node.online).length} / {nodes.length} 节点在线</small></div>
          <div className="admin-stat"><span>GPU 运行</span><strong>{fleet ? running.filter(row => row.mode !== 'headless').length : '—'}<small> 台</small></strong><small>每位客户最多 1 台</small></div>
          <div className="admin-stat"><span>无头运行</span><strong>{fleet ? running.filter(row => row.mode === 'headless').length : '—'}<small> 台</small></strong><small>独立环境，不占显卡</small></div>
          <button className={`admin-stat ${attention ? 'is-warning' : ''}`} onClick={() => navigate({ status: 'attention', node: 'all', mode: 'all', owner: '', q: '' })}><span>需要关注</span><strong>{fleet ? attention : '—'}<small> 台</small></strong><small>异常或节点离线 · 点击查看</small></button>
        </div>
        <section className="rental-card">
          <div className="admin-toolbar"><label className="admin-search"><Search size={16} /><input aria-label="搜索客户实例" placeholder="搜索客户、实例、ID 或节点" value={route.q} onChange={e => navigate({ q: e.target.value })} /></label>
            <select aria-label="实例节点" value={route.node} onChange={e => navigate({ node: e.target.value })}><option value="all">全部节点</option>{Array.from(new Set([...nodes.map(node => node.id), ...rows.map(row => row.nodeId || 'G2-002')])).map(id => <option key={id}>{id}</option>)}</select>
            <select aria-label="实例状态" value={route.status} onChange={e => navigate({ status: e.target.value })}><option value="all">全部状态</option><option value="running">运行中</option><option value="stopped">已关机</option><option value="pending">处理中</option><option value="attention">需要关注</option></select>
            <select aria-label="实例模式" value={route.mode} onChange={e => navigate({ mode: e.target.value })}><option value="all">全部模式</option><option value="gpu">GPU</option><option value="headless">无头</option></select>
            <button className="admin-link" onClick={() => navigate({ q: '', node: 'all', status: 'all', mode: 'all', owner: '' })}>重置筛选</button>
          </div>
          {route.owner && <div className="admin-scope">客户：<strong>{route.owner}</strong><button onClick={() => navigate({ owner: '' })} aria-label="清除客户筛选"><X size={14} /></button><button className="admin-link" onClick={() => showLedger(route.owner)}>查看该客户账单</button></div>}
          <section className="admin-table-wrap" aria-label="实例列表，可横向滚动"><table className="admin-table admin-instance-table"><thead><tr><th>实例 / 客户</th><th>节点 / 模式</th><th>状态</th><th>算力单价</th><th>操作</th></tr></thead><tbody>{visibleRows.map(row => <tr key={row.id}>
            <td className="admin-primary-cell"><button className="admin-link" onClick={() => setDetailsId(row.id)}>{row.name} <span className="admin-muted">#{row.id}</span></button><button className="admin-subtext admin-link" onClick={() => navigate({ section: 'customers', customer: row.owner || '', accounts: 'all' }, true)}>{row.owner || '—'}</button></td>
            <td>{row.nodeId || 'G2-002'}<span className="admin-subtext">{row.mode === 'headless' ? '无头 · 无显卡' : `${row.gpuCount ?? 1}卡 · ${row.slots?.length ? row.slots.join(' / ') : row.slot || '待分配'}`}</span></td>
            <td><span className={`rental-state rental-state-${row.state}`}><i />{stateLabel(row.state)}</span>{isAttention(row) ? <span className="admin-subtext admin-danger admin-ellipsis" title={row.message || undefined}>{row.nodeOnline === false ? '节点离线' : row.message || '请查看详情'}</span> : row.releaseAt && <span className="admin-subtext" title={`${formatDate(row.releaseAt)} 自动释放`}>{Math.max(0, Math.ceil((new Date(row.releaseAt).getTime() - new Date(resource.updatedAt).getTime()) / 3600000))} 小时内自动释放</span>}</td>
            <td>{formatMoney(row.rateCentsPerHour ?? 0)}<small> / h</small><span className="admin-subtext">{row.state === 'stopped' || row.state === 'error' ? '算力已停计费' : '本次锁定费率'}{row.dataDiskGiB > 0 ? ' · 另收存储费' : ''}</span></td><td>{actions(row)}</td>
          </tr>)}</tbody></table></section>
          {visibleRows.length === 0 && <div className="admin-empty">{loading ? '正在读取实例…' : !fleet && error ? '实例暂时无法加载，请重试。' : rows.length ? '没有符合筛选条件的实例，可重置筛选。' : '目前没有保留实例。'}</div>}
          <AdminPagination page={currentPage} pages={pageCount} total={filtered.length} fullTotal={rows.length} onPage={setPage} />
        </section>
      </>}
      <div hidden={route.section !== 'customers'}>{accounts({ active: active && route.section === 'customers', revision, query: route.customer, status: route.accounts, onQuery: value => navigate({ customer: value }), onStatus: value => navigate({ accounts: value }), onReset: () => navigate({ customer: '', accounts: 'active' }), onChanged: changed, onRecharge: setRecharge, onInstances: showInstances, onLedger: showLedger })}</div>
      <div hidden={route.section !== 'finance'}>
        <nav className="admin-subnav" aria-label="财务分类">{[{ id: 'ledger', label: '账户流水' }, { id: 'codes', label: '充值码' }, { id: 'audit', label: '操作记录' }].map(item => <button key={item.id} aria-current={route.finance === item.id ? 'page' : undefined} onClick={() => navigate({ finance: item.id }, true)}>{item.label}</button>)}</nav>
        {route.owner && route.finance === 'ledger' && <div className="admin-scope">仅查看客户 <strong>{route.owner}</strong><button aria-label="显示全部客户账单" onClick={() => navigate({ owner: '' })}><X size={14} /></button></div>}
        <div hidden={route.finance !== 'ledger'}>{ledger(route.owner, revision, active && route.section === 'finance' && route.finance === 'ledger')}</div>
        <div hidden={route.finance !== 'codes'}>{codes(active && route.section === 'finance' && route.finance === 'codes', revision)}</div>
        {route.section === 'finance' && route.finance === 'audit' && <AdminAudit active={active} revision={revision} />}
      </div>
      {route.section === 'nodes' && <AdminNodes nodes={nodes} slots={resource.slots} telemetryByNode={telemetryByNode} selectedId={route.node} error={telemetryError} loading={loading} onSelect={node => navigate({ node })} onInstance={setDetailsId} onViewInstances={node => navigate({ section: 'instances', node, owner: '', q: '', status: 'all', mode: 'all' }, true)} />}
      <div className="admin-settings" hidden={route.section !== 'settings'}>{route.section === 'settings' && <><AdminPrice currentRate={resource.billing.rateCentsPerHour} onSaved={changed} />{settings}</>}</div>
    </div>
  </div>
    {detailsId && <AdminDialog title={details ? `${details.name} · #${details.id}` : `实例 #${detailsId}`} onClose={() => setDetailsId(null)}>{details ? <><div className="admin-detail-grid">{[['客户', details.owner], ['节点', details.nodeId || 'G2-002'], ['状态', stateLabel(details.state)], ['计算规格', `${details.vcpu} vCPU / ${details.memoryGB} GB`], ['系统盘 / 数据盘', `${details.systemDiskGiB || 50} / ${details.dataDiskGiB} GiB`], ['本次 / 上次单价', `${formatMoney(details.rateCentsPerHour ?? 0)}/h`], ['累计计算费用', formatMoney(details.computeCents ?? 0)], ['累计存储费用', formatMoney(details.storageCents ?? 0)], ['数据盘每日费用', `¥${(details.storageCnyPerDay ?? 0).toFixed(3)}`], ['创建时间', formatDate(details.createdAt)], ['计费开始', formatDate(details.billableAt)], ['自动释放时间', formatDate(details.releaseAt)]].map(([label, value]) => <div key={label}><span>{label}</span><strong>{value || '—'}</strong></div>)}</div><p className={isAttention(details) ? 'admin-danger' : 'admin-help'}>{details.message}</p><p className="admin-help">关机停止算力计费；数据盘保留期间仍计费。关机 48 小时后自动释放，请客户提前备份。</p><div className="admin-dialog-actions"><button className="rental-small-button" onClick={() => { setDetailsId(null); showLedger(details.owner || ''); }}>查看客户账单</button><button className="rental-small-button" onClick={() => setDetailsId(null)}>关闭</button></div></> : <p>该实例已释放或暂不在当前列表中，请刷新核对。</p>}</AdminDialog>}
    {operation && <AdminOperationDialog operation={operation} busy={busy} gpuRate={resource.billing.rateCentsPerHour ?? 0} onCancel={() => { if (!busy) setOperation(null); }} onConfirm={() => void perform()} />}
    {recharge && <AdminRecharge customer={recharge} requestKeys={rechargeKeys} onClose={() => setRecharge(null)} onDone={text => { setRecharge(null); setNotice({ text }); changed(); }} />}
  </main>;
}

export function AdminPagination({ page, pages, total, fullTotal = total, onPage }: { page: number; pages: number; total: number; fullTotal?: number; onPage: (page: number) => void }) {
  return <div className="admin-pagination"><span>{total === fullTotal ? `共 ${total} 条` : `${total} 条匹配 / 共 ${fullTotal} 条`}</span><div><button className="rental-small-button" aria-label="上一页" disabled={page <= 1} onClick={() => onPage(page - 1)}><ChevronLeft size={15} /></button><span>{page} / {pages}</span><button className="rental-small-button" aria-label="下一页" disabled={page >= pages} onClick={() => onPage(page + 1)}><ChevronRight size={15} /></button></div></div>;
}

function AdminDialog({ title, children, onClose, busy = false }: { title: string; children: ReactNode; onClose: () => void; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog className="admin-dialog" ref={ref} aria-labelledby={id} onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}><div className="admin-dialog-head"><h2 id={id}>{title}</h2><button className="admin-icon-button" aria-label="关闭窗口" disabled={busy} onClick={onClose}><X size={18} /></button></div><div className="admin-dialog-body">{children}</div></dialog>;
}

function AdminOperationDialog({ operation: { row, operation, mode }, busy, gpuRate, onCancel, onConfirm }: { operation: Operation; busy: boolean; gpuRate: number; onCancel: () => void; onConfirm: () => void }) {
  const [typed, setTyped] = useState('');
  const deleting = operation === 'delete';
  return <AdminDialog title={deleting ? '永久释放实例' : operation === 'stop' ? '确认关闭实例' : `确认${mode === 'headless' ? '无头' : 'GPU'}开机`} onClose={onCancel} busy={busy}>
    <p><strong>{row.owner} / {row.name} · #{row.id}</strong><br />{row.nodeId || 'G2-002'}</p>
    <p className={deleting ? 'admin-danger' : 'admin-help'}>{deleting ? '系统盘和数据盘将永久删除，无法恢复。请确认客户已完成备份。' : operation === 'stop' ? '运行任务将中断，停止后不再收算力费，数据盘继续计费。关机 48 小时后自动释放。' : `成功开机后从该客户余额扣费：${formatMoney(mode === 'headless' ? 8 : gpuRate * (row.gpuCount ?? 1))}/小时。保留磁盘与环境，最终资源和余额校验由服务端执行。`}</p>
    {deleting && <label className="rental-field"><span>输入实例 ID {row.id} 确认</span><input aria-label="释放确认实例 ID" value={typed} onChange={e => setTyped(e.target.value)} autoComplete="off" /></label>}
    <div className="admin-dialog-actions"><button className="rental-small-button" disabled={busy} onClick={onCancel}>取消</button><button className={deleting ? 'rental-small-button rental-small-danger' : 'admin-primary-button'} disabled={busy || deleting && typed !== row.id} onClick={onConfirm}>{busy ? '提交中…' : deleting ? '确认永久释放' : '确认操作'}</button></div>
  </AdminDialog>;
}

function AdminRecharge({ customer, requestKeys, onClose, onDone }: { customer: AdminCustomer; requestKeys: RefObject<Map<string, string>>; onClose: () => void; onDone: (text: string) => void }) {
  const [amount, setAmount] = useState('');
  const [note, setNote] = useState('');
  const [review, setReview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const inFlight = useRef(false);
  const cents = moneyToCents(amount, 100_000_000);
  const submit = async () => {
    if (cents === null || inFlight.current) return;
    if (!review) { setReview(true); return; }
    inFlight.current = true; setBusy(true); setError('');
    const intent = JSON.stringify({ user: customer.name, cents, note: note.trim() || '管理员面板充值' });
    if (!requestKeys.current.has(intent)) requestKeys.current.set(intent, adminRequestKey());
    try {
      await adminRequest('/api/admin/recharge', { method: 'POST', headers: { 'X-Idempotency-Key': requestKeys.current.get(intent)! }, body: intent });
      requestKeys.current.delete(intent);
      onDone(`已为 ${customer.name} 充值 ${formatMoney(cents)}，客户余额与流水已重新加载。`);
    } catch (e) { setError(`${e instanceof Error ? e.message : '充值未完成'}。可重试同一请求；请勿重复提交不同金额。`); }
    finally { inFlight.current = false; setBusy(false); }
  };
  return <AdminDialog title={review ? '核对充值信息' : `给 ${customer.name} 充值`} busy={busy} onClose={onClose}>
    <p className="admin-help">充值对象：<strong>{customer.name}</strong> · 列表余额 {formatMoney(customer.balanceCents)}（以实时账本为准）</p>
    {review ? <div className="admin-detail-grid"><div><span>入账账户</span><strong>{customer.name}</strong></div><div><span>增加余额</span><strong>{formatMoney(cents ?? 0)}</strong></div><div><span>备注</span><strong>{note || '管理员面板充值'}</strong></div></div> : <><label className="rental-field"><span>充值金额（元）</span><input aria-label="充值金额（元）" inputMode="decimal" placeholder="请输入金额" value={amount} onChange={e => setAmount(e.target.value)} /></label><label className="rental-field"><span>收款编号 / 备注（可选）</span><input value={note} maxLength={120} onChange={e => setNote(e.target.value)} placeholder="用于后续核对，不填写密码或密钥" /></label>{amount && cents === null && <p className="admin-danger">请输入 0.01–1,000,000 元，最多两位小数。</p>}</>}
    {error && <div className="admin-notice is-error" role="alert">{error}</div>}
    <p className="admin-help">这会直接增加客户余额，不代表已在线收款。请先核实到账与账户。</p>
    <div className="admin-dialog-actions"><button className="rental-small-button" disabled={busy} onClick={review ? () => setReview(false) : onClose}>{review ? '返回修改' : '取消'}</button><button className="admin-primary-button" disabled={busy || cents === null} onClick={() => void submit()}>{busy ? '入账中…' : review ? '确认入账' : '核对充值'}</button></div>
  </AdminDialog>;
}

function AdminPrice({ currentRate, onSaved }: { currentRate?: number; onSaved: () => void }) {
  const [price, setPrice] = useState('');
  const [confirm, setConfirm] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const dirty = useRef(false), inFlight = useRef(false);
  useEffect(() => { if (!dirty.current && currentRate !== undefined) setPrice((currentRate / 100).toFixed(2)); }, [currentRate]);
  const save = async () => {
    if (confirm === null || inFlight.current) return;
    inFlight.current = true; setBusy(true);
    try { await adminRequest('/api/admin/price', { method: 'POST', body: JSON.stringify({ cents: confirm }) }); dirty.current = false; setConfirm(null); setNotice({ text: '新开机价格已保存，运行实例费率不变。' }); onSaved(); }
    catch (e) { setConfirm(null); setNotice({ error: true, text: e instanceof Error ? e.message : '保存失败' }); }
    finally { inFlight.current = false; setBusy(false); }
  };
  const cents = moneyToCents(price);
  return <section className="rental-card"><div className="rental-card-head"><h2>GPU 卡时价格</h2><span className="admin-muted">仅影响下次开机</span></div><div className="rental-admin-body"><label className="rental-field"><span>单卡新开机价格（元 / 小时）</span><input value={price} inputMode="decimal" onChange={e => { dirty.current = true; setPrice(e.target.value); }} /></label><p className="admin-help">四卡价格为单卡的 4 倍（64 vCPU / 250 GB / 50 GiB），数据盘另计。无头模式固定 ¥0.08 / 小时；本项不会修改正在运行实例的锁定费率。</p><button className="admin-primary-button" disabled={busy || cents === null || currentRate === undefined || cents === currentRate} onClick={() => setConfirm(cents)}>保存价格</button>{notice && <div className={`admin-notice ${notice.error ? 'is-error' : 'is-success'}`} role={notice.error ? 'alert' : 'status'}>{notice.text}</div>}</div>{confirm !== null && <AdminDialog title="确认修改卡时价格" busy={busy} onClose={() => setConfirm(null)}><p>单卡新开机价格从 {formatMoney(currentRate ?? 0)} / h 改为 <strong>{formatMoney(confirm)} / h</strong>。已运行实例不受影响，操作会记录审计。</p><div className="admin-dialog-actions"><button className="rental-small-button" disabled={busy} onClick={() => setConfirm(null)}>取消</button><button className="admin-primary-button" disabled={busy} onClick={() => void save()}>{busy ? '保存中…' : '确认修改'}</button></div></AdminDialog>}</section>;
}

type AuditEntry = { id: number; actor: string; event: string; target: string; detail: string; created_at: string };
function AdminAudit({ active, revision }: { active: boolean; revision: number }) {
  const [events, setEvents] = useState<AuditEntry[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  useEffect(() => {
    if (!active) return;
    let disposed = false;
    void adminRequest<{ events: AuditEntry[] }>('/api/admin/audit').then(data => { if (!disposed) { setEvents(data.events); setError(''); } }).catch(e => { if (!disposed) setError(String(e)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [active, revision]);
  const labels: Record<string, string> = { customer_deleted: '删除客户账户', customer_restored: '恢复客户账户', registration_bonus_changed: '修改注册赠金', registration_bonus_granted: '发放注册赠金', price_changed: '修改卡时价', instance_start: '启动实例', instance_stop: '关闭实例', instance_delete: '释放实例', guest_poweroff_detected: '检测到关机', recharge_codes_issued: '生成充值码', recharge_codes_revoked: '作废充值码', recharge_code_redeemed: '兑换入账' };
  const filtered = events.filter(row => `${row.actor} ${row.target} ${labels[row.event] || row.event}`.toLowerCase().includes(query.trim().toLowerCase()));
  const pages = Math.max(1, Math.ceil(filtered.length / 20)), current = Math.min(page, pages);
  return <section className="rental-card"><div className="rental-card-head"><h2>操作记录</h2><span className="admin-muted">最近 {events.length} 条</span></div><div className="admin-toolbar"><label className="admin-search"><Search size={16} /><input aria-label="搜索操作记录" placeholder="搜索操作者、目标或操作" value={query} onChange={e => { setQuery(e.target.value); setPage(1); }} /></label></div>{error && <div className="admin-notice is-error" role="alert">{error}</div>}<div className="admin-table-wrap"><table className="admin-table"><thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>目标</th><th>详情</th></tr></thead><tbody>{filtered.slice((current - 1) * 20, current * 20).map(row => <tr key={row.id}><td>{formatDate(row.created_at)}</td><td>{row.actor}</td><td>{labels[row.event] || row.event}</td><td>{row.target}</td><td><details><summary aria-label={`查看操作记录 #${row.id} 详情`}>查看详情</summary><p className="admin-help">{row.detail}</p></details></td></tr>)}</tbody></table></div>{!filtered.length && <div className="admin-empty">{loading ? '读取中…' : '没有匹配的操作记录'}</div>}<AdminPagination page={current} pages={pages} total={filtered.length} fullTotal={events.length} onPage={setPage} /></section>;
}
