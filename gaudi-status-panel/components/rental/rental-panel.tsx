'use client';

import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { BrandLogo } from '@/components/brand-logo';
import { AdminWorkspace, AdminPagination, type AdminAccountProps } from './admin-workspace';
import { gpuCapacityReason } from './gpu-plans';
import { CustomerWorkspace } from './customer-workspace';
import { customerSearch, filterCustomerInstances, readCustomerTab, type CustomerFilter } from './customer-model';
import { PlacementSettings, type PlacementPolicy, type PlacementQuote } from './placement-settings';
import { currentRequestIdentity, resetRequestIdentity, notifyUnauthorized, gpuInstanceQuotaReason } from './admin-model';
import { withRequestTimeout } from '../../lib/request-timeout.mjs';
import {
  ArrowRight,
  CheckCircle2,
  CircleHelp,
  Cpu,
  LoaderCircle,
  MessageCircle,
  Plus,
  Power,
  RefreshCw,
  Server,
  ShieldCheck,
  Terminal,
  Wallet,
  XCircle,
  Copy, Eye, EyeOff, Clock3, Search,
} from 'lucide-react';

type InstanceState = 'creating' | 'starting' | 'running' | 'stopping' | 'stopped' | 'error' | 'repair_required';
type SlotState = 'available' | 'creating' | 'running' | 'stopping' | 'stopped' | 'error' | 'repair_required';
type InstanceMode = 'gpu' | 'headless';

export type RentalInstance = {
  id: string;
  nodeId?: string;
  nodeOnline?: boolean;
  name: string;
  slot: number;
  gpuCount?: 1 | 4 | 8;
  slots?: number[];
  mode?: InstanceMode;
  state: InstanceState;
  vcpu: number;
  memoryGB: number;
  systemDiskGiB: number;
  dataDiskGiB: number;
  billableDataDiskGiB?: number;
  giftDataDiskGiB?: number;
  eightCardGiftDisk?: boolean;
  image: string;
  publicHost: string | null;
  publicPort: number | null;
  username: string | null;
  password: string | null;
  billableAt: string | null;
  createdAt: string;
  message: string | null;
  releaseAt?: string | null;
  rateCentsPerHour?: number;
  computeCents?: number;
  storageCents?: number;
  storageCnyPerDay?: number;
  connectivity?: 'ready' | 'pending';
  owner?: string;
  ownerGpuInstanceLimit?: number | null;
  observedAt?: string;
};

export type RentalAccount = {
  name: string;
  role: 'customer' | 'admin';
  balanceCents: number;
  createdAt: string;
  lastActivityAt: string | null;
  gpuInstanceLimit?: number | null;
  gpuActiveCount?: number;
};

export type HostTelemetry = {
  status: 'live' | 'partial' | 'unavailable';
  observedAt: string | null;
  cpu?: { model: string; sockets: number; threads: number; usagePct: number | null };
  memory?: { totalBytes: number; usedBytes: number; usagePct: number | null; speedMTs: number | null; modules: number };
  swap?: { totalBytes: number; usedBytes: number; usagePct: number | null };
  power?: {
    status: 'live' | 'stale' | 'collecting' | 'unavailable'; source: string;
    currentW: number | null; average24hW: number | null; coverageSeconds24h: number;
    rateCnyPerKwh: number; totalKwh: number; totalCostCny: number; cost24hCny: number;
    forecast30dCny: number | null; firstSampleAt: string | null; lastSampleAt: string | null;
    sampleAgeSeconds: number | null; samplesOk: number; samplesFailed: number; gaps: number;
    history24h: Array<{ timestamp: string; watts: number }>;
  };
  error?: string;
};

export type RentalState = {
  placementPolicy?: PlacementPolicy;
  service: 'ready' | 'degraded' | 'blocked' | 'maintenance';
  serviceMessage: string;
  account: RentalAccount;
  nodes?: Array<{ id: string; online: boolean; imageReady: boolean; headlessAvailable: number; availableCpu?: number; availableMemoryMB?: number; storage?: RentalState['storage']; telemetry?: HostTelemetry }>;
  slots: Array<{
    slot: number;
    nodeId?: string;
    state: SlotState;
    instanceId: string | null;
    gpu: string;
  }>;
  instances: RentalInstance[];
  headless?: { available: number; running: number; capacity: number; vcpu: number; memoryGB: number; rateCentsPerHour: number };
  image: {
    id: string;
    name: string;
    version: string;
    ready: boolean;
    detail: string;
  };
  limits: {
    vcpu: [number, number];
    memoryGB: [number, number];
    dataDiskGiB: [number, number];
    systemDiskGiB: number;
  };
  billing: {
    mode: 'balance_metered' | 'meter_only' | 'configured';
    currency: string;
    detail: string;
    rateCentsPerHour?: number;
    headlessRateCentsPerHour?: number;
    gpuPlans?: Array<{ gpuCount: number; minCreateBalanceExclusiveCents?: number }>;
    includedCpu?: number;
    includedMemoryGB?: number;
    freeDataDiskGiB?: number;
    extraDataDiskCnyPerGiBDay?: number;
  };
  updatedAt: string;
  storage?: { totalGiB: number; usedGiB: number; freeGiB: number; safetyGiB: number; reservedGiB: number; budgetGiB: number; lowSpace: boolean; mode: string };
};

export type AdminCustomer = { name: string; balanceCents: number; createdAt: string; deletedAt: string | null; deletedBy: string | null; instanceCount: number; gpuInstanceLimit: number | null; gpuActiveCount: number };

const initialState: RentalState = {
  service: 'degraded',
  serviceMessage: '正在连接资源调度器…',
  account: { name: '', role: 'customer', balanceCents: 0, createdAt: '', lastActivityAt: null },
  slots: Array.from({ length: 8 }, (_, index) => ({ slot: index + 1, state: 'available', instanceId: null, gpu: `Gaudi2 · HPU ${index + 1}` })),
  instances: [],
  image: {
    id: 'gaudi-ubuntu24.04',
    name: 'Gaudi Ubuntu 24.04',
    version: '待制备',
    ready: false,
    detail: '正在等待已安装 Habana 环境的母盘',
  },
  limits: { vcpu: [16, 16], memoryGB: [62.5, 62.5], dataDiskGiB: [0, 200], systemDiskGiB: 50 },
  billing: { mode: 'balance_metered', currency: 'CNY', detail: '¥4.00/小时含16核、62.5 GB内存和50 GiB系统盘；数据盘按日额外计费' },
  updatedAt: '',
};

function formatDate(value: string | null) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { hour12: false });
}

function formatMoney(cents: number) {
  return `¥${(cents / 100).toFixed(2)}`;
}

function stateLabel(state: InstanceState | SlotState) {
  const labels: Record<string, string> = {
    available: '可用', creating: '创建中', starting: '开机中', running: '运行中', stopping: '关机中',
    stopped: '已关机', error: '启动失败', repair_required: '待修复',
  };
  return labels[state] ?? state;
}

function stateClass(state: InstanceState | SlotState) {
  return `rental-state rental-state-${state}`;
}

async function rentalRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const generation = currentRequestIdentity();
  const headers = new Headers(init?.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  return withRequestTimeout(async signal => {
    const response = await fetch(path, { ...init, headers, cache: 'no-store', signal });
    const body = (await response.json().catch(() => ({}))) as { error?: string; message?: string } & T;
    notifyUnauthorized(response.status, generation, path);
    if (!response.ok) throw new Error(`${response.status}: ${body.message || body.error || '请求失败'}`);
    return body as T;
  }, 15000, init?.signal);
}

function requestKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, '0')).join('');
}

export function RentalPanel() {
  const [account, setAccount] = useState<RentalAccount | null | undefined>(undefined);
  const [state, setState] = useState<RentalState>(initialState);
  const [dataDiskGiB, setDataDiskGiB] = useState(0);
  const [mode, setMode] = useState<InstanceMode>('gpu');
  const [gpuCount, setGpuCount] = useState<1 | 4 | 8>(1);
  const [nodeId, setNodeId] = useState('auto');
  const [placementResult, setPlacementResult] = useState<{ key: string; value: PlacementQuote } | null>(null);
  const [placementError, setPlacementError] = useState('');
  const [instanceName, setInstanceName] = useState('gaudi-dev');
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [actionId, setActionId] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: 'info' | 'success' | 'error'; text: string } | null>(null);
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [authName, setAuthName] = useState('');
  const [authPassword, setAuthPassword] = useState('');
  const [authSubmitting, setAuthSubmitting] = useState(false);
  const refreshing = useRef(false);
  const authEpoch = useRef(0);
  const orderKey = useRef<string | null>(null);
  const orderIntent = useRef('');
  const [tab, setTab] = useState<'instances' | 'create' | 'wallet' | 'admin'>('instances');
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<CustomerFilter>('all');
  const [instancePage, setInstancePage] = useState(1);
  const [walletRevision, setWalletRevision] = useState(0);
  const [confirmation, setConfirmation] = useState<{ id: string; operation: 'stop' | 'delete'; name: string } | null>(null);

  const resetPrivateState = useCallback(() => {
    authEpoch.current += 1;
    resetRequestIdentity();
    refreshing.current = false;
    orderKey.current = null; orderIntent.current = '';
    setState(initialState); setLoading(true); setNotice(null);
    setConfirmation(null); setActionId(null); setSubmitting(false);
    setTab('instances'); setSearch(''); setFilter('all'); setInstancePage(1); setWalletRevision(0);
    setAuthPassword('');
    setNodeId('auto'); setPlacementResult(null); setPlacementError('');
  }, []);
  useEffect(() => {
    const expired = () => {
      resetPrivateState(); setAccount(null);
      setNotice({ tone: 'info', text: '登录已失效，已清空本页账户信息，请重新登录。' });
    };
    window.addEventListener('rental:auth-expired', expired);
    return () => window.removeEventListener('rental:auth-expired', expired);
  }, [resetPrivateState]);

  useEffect(() => {
    let disposed = false;
    const epoch = authEpoch.current;
    void rentalRequest<{ account: RentalAccount }>('/api/auth/me')
      .then((result) => { if (disposed || epoch !== authEpoch.current) return; setAccount(result.account); if (result.account.role === 'admin' && new URLSearchParams(location.search).get('view') !== 'workspace') setTab('admin'); })
      .catch(() => { if (!disposed && epoch === authEpoch.current) setAccount(null); });
    return () => { disposed = true; };
  }, []);

  const accountName = account?.name;
  const adminView = account?.role === 'admin' && tab === 'admin';
  const selectTab = (next: 'instances' | 'create' | 'wallet' | 'admin') => {
    setTab(next);
    history.pushState(null, '', `${location.pathname}${customerSearch(next, location.search)}${location.hash}`);
  };
  useEffect(() => {
    if (!account?.role) return;
    const restore = () => setTab(readCustomerTab(location.search, account.role));
    restore(); window.addEventListener('popstate', restore);
    return () => window.removeEventListener('popstate', restore);
  }, [account?.role]);
  const refresh = useCallback(async () => {
    if (!accountName || refreshing.current) return;
    refreshing.current = true;
    const epoch = authEpoch.current;
    try {
      const next = await rentalRequest<RentalState>('/api/rental/state');
      if (epoch !== authEpoch.current) return;
      if (next.account.name !== accountName) { resetPrivateState(); setAccount(next.account); return; }
      setState(next);
      setAccount(next.account);
    } catch (error) {
      if (epoch !== authEpoch.current) return;
      if (error instanceof Error && /登录|unauthorized|401/i.test(error.message)) { resetPrivateState(); setAccount(null); }
      else setNotice({ tone: 'error', text: error instanceof Error ? error.message : '资源调度器暂时不可用' });
    } finally {
      if (epoch === authEpoch.current) { refreshing.current = false; setLoading(false); }
    }
  }, [accountName, resetPrivateState]);

  useEffect(() => {
    if (!accountName) return;
    const initial = window.setTimeout(() => { void refresh(); }, 0);
    const timer = window.setInterval(() => { if (!adminView && !document.hidden) void refresh(); }, 5000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [accountName, refresh, adminView]);

  const submitAuth = async () => {
    const epoch = authEpoch.current;
    setAuthSubmitting(true);
    setNotice(null);
    try {
      const result = await rentalRequest<{ account: RentalAccount }>(`/api/auth/${authMode}`, {
        method: 'POST',
        body: JSON.stringify({ name: authName.trim(), password: authPassword }),
      });
      if (epoch !== authEpoch.current) return;
      resetPrivateState();
      setAccount(result.account);
      if (result.account.role === 'admin') setTab(new URLSearchParams(location.search).get('view') === 'workspace' ? 'instances' : 'admin');
      setAuthPassword('');
      setNotice({ tone: 'success', text: authMode === 'register' ? '账户已创建并登录。' : '登录成功。' });
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '认证失败' });
    } finally {
      setAuthSubmitting(false);
    }
  };

  const placementKey = `${accountName}|${mode}|${gpuCount}|${dataDiskGiB}`;
  useEffect(() => {
    if (!accountName || adminView || tab !== 'create') return;
    let disposed = false;
    const abort = new AbortController();
    const timer = window.setTimeout(() => {
      const query = new URLSearchParams({ mode, gpuCount: String(gpuCount), dataDiskGiB: String(dataDiskGiB) });
      void rentalRequest<PlacementQuote>(`/api/rental/placement?${query}`, { signal: abort.signal })
        .then(value => { if (!disposed) { setPlacementResult({ key: placementKey, value }); setPlacementError(''); } })
        .catch(error => { if (!disposed) { setPlacementResult(null); setPlacementError(error instanceof Error ? error.message : '调度状态暂不可用'); } });
    }, 200);
    return () => { disposed = true; window.clearTimeout(timer); abort.abort(); };
  }, [accountName, adminView, tab, mode, gpuCount, dataDiskGiB, placementKey, state.updatedAt]);

  if (account === undefined) return <main className="rental-app"><div className="rental-auth-loading">正在连接账户服务…</div></main>;
  if (account === null) return <AuthGate mode={authMode} setMode={setAuthMode} name={authName} setName={setAuthName} password={authPassword} setPassword={setAuthPassword} submitting={authSubmitting} onSubmit={() => void submitAuth()} notice={notice} />;

  const placement = placementResult?.key === placementKey ? placementResult.value : null;
  const effectiveNodeId = nodeId === 'auto' ? placement?.recommendedNode : nodeId;
  const selectedPlacement = placement?.nodes.find(node => node.id === effectiveNodeId);
  const eightCreateBalanceCents = state.billing.gpuPlans?.find(plan => plan.gpuCount === 8)?.minCreateBalanceExclusiveCents ?? 10000;
  const canOrder = state.service !== 'maintenance' && !!selectedPlacement?.allowed && !!selectedPlacement?.createAvailable && !submitting
    && (gpuCount !== 8 || state.account.balanceCents > eightCreateBalanceCents);
  const gpuActiveCount = state.account.gpuActiveCount ?? state.instances.filter(row => row.slot > 0).length;
  const hourlyCost = state.instances.reduce((sum, row) => sum + (['running', 'stopping', 'repair_required'].includes(row.state) ? row.rateCentsPerHour ?? 0 : 0) + (row.storageCnyPerDay ?? 0) * 100 / 24, 0);
  const startReason = (instance: RentalInstance, selected: InstanceMode) => {
    const node = state.nodes?.find((item) => item.id === (instance.nodeId || 'G2-002'));
    if (state.service === 'maintenance') return state.serviceMessage;
    if (instance.nodeOnline === false || (node && (!node.online || !node.imageReady))) return '所属节点暂不可用，请稍后重试';
    if (node?.storage?.lowSpace) return '所属节点存储空间不足';
    if (!node && state.service !== 'ready') return state.serviceMessage;
    if (instance.gpuCount !== 8 && state.account.balanceCents <= 0) return '余额不足，请先充值';
    if (selected === 'headless') return (node?.headlessAvailable ?? state.headless?.available ?? 0) <= 0 ? '所属节点无头资源不足' : '';
    const quotaReason = gpuInstanceQuotaReason(state.account.gpuInstanceLimit, gpuActiveCount);
    if (quotaReason) return quotaReason;
    return gpuCapacityReason(state, instance.nodeId, instance.gpuCount ?? 1);
  };
  const estimatedHours = hourlyCost > 0 ? state.account.balanceCents / hourlyCost : null;
  const visibleInstances = filterCustomerInstances(state.instances, search, filter);
  const instancePages = Math.max(1, Math.ceil(visibleInstances.length / 10));
  const currentInstancePage = Math.min(instancePage, instancePages);
  const displayedInstances = visibleInstances.slice((currentInstancePage - 1) * 10, currentInstancePage * 10);

  const order = async () => {
    setSubmitting(true);
    setNotice(null);
    try {
      const intent = JSON.stringify({ name: instanceName.trim(), dataDiskGiB, mode, nodeId, gpuCount });
      if (orderIntent.current !== intent) { orderKey.current = null; orderIntent.current = intent; }
      orderKey.current ??= requestKey();
      const created = await rentalRequest<{ instance: { node_id: string } }>('/api/rental/order', {
        method: 'POST',
        headers: { 'X-Idempotency-Key': orderKey.current },
        body: JSON.stringify({ name: instanceName.trim() || 'gaudi-dev', mode, nodeId, gpuCount, vcpu: mode === 'headless' ? 2 : 16 * gpuCount, memoryGB: mode === 'headless' ? 4 : gpuCount === 8 ? 480 : 62.5 * gpuCount, dataDiskGiB, image: state.image.id }),
      });
      orderKey.current = null;
      selectTab('instances');
      setNotice({ tone: 'success', text: `实例 ${instanceName.trim() || 'gaudi-dev'} 已在 ${created.instance.node_id} 创建，请在实例列表中点击开机。` });
      await refresh();
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '下单失败' });
    } finally {
      setSubmitting(false);
    }
  };

  const action = async (id: string, operation: 'start' | 'stop' | 'delete', selectedMode?: InstanceMode, confirmed = false) => {
    if (operation !== 'start' && !confirmed) {
      setConfirmation({ id, operation, name: state.instances.find((row) => row.id === id)?.name ?? id });
      return;
    }
    setConfirmation(null);
    setActionId(id);
    setNotice(null);
    try {
      await rentalRequest(`/api/rental/instances/${encodeURIComponent(id)}/${operation}`, { method: 'POST', body: JSON.stringify(operation === 'start' ? { mode: selectedMode } : {}) });
      setNotice({ tone: 'success', text: operation === 'delete' ? '实例已进入释放流程。' : operation === 'start' ? '实例已进入开机流程。' : '实例已进入关机流程。' });
      await refresh();
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '操作失败' });
    } finally {
      setActionId(null);
    }
  };

  const changeGpuPlan = async (id: string, target: 1 | 4): Promise<boolean> => {
    setActionId(id);
    setNotice(null);
    try {
      await rentalRequest(`/api/rental/instances/${encodeURIComponent(id)}/gpu-plan`, {
        method: 'POST', body: JSON.stringify({ gpuCount: target }),
      });
      await refresh();
      setNotice({ tone: 'success', text: `实例 #${id} 已改为 ${target} 卡套餐。磁盘和所属节点不变；下次开机按新配置计费。` });
      return true;
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '更改配置失败' });
      return false;
    } finally {
      setActionId(null);
    }
  };

  const logout = async () => {
    try {
      await rentalRequest('/api/auth/logout', { method: 'POST' });
      resetPrivateState();
      setAccount(null);
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '退出失败' });
    }
  };

  return (
    <>
    {account.role === 'admin' && <div hidden={!adminView}><AdminWorkspace key={account.name} account={account} state={state} active={adminView} onRefresh={() => void refresh()} onLogout={() => void logout()} onCustomerView={() => selectTab('instances')}
      accounts={(props) => <AdminAccountPanel {...props} />}
      codes={(active, revision) => <AdminCodePanel active={active} customerRevision={revision} />}
      ledger={(owner, revision, active) => <LedgerPanel key={owner || 'all'} admin ownerFilter={owner} externalRevision={revision} active={active} />}
      settings={<><PlacementSettings onSaved={() => void refresh()} /><AdminRegistrationBonus /></>}
    /></div>}
    <CustomerWorkspace key={account.name} account={account} state={state} tab={tab} loading={loading} hourlyCost={hourlyCost}
      onNavigate={selectTab} onRefresh={() => void refresh()} onLogout={() => void logout()} brand={<RentalBrand />} contact={<RechargeContactBanner compact />}
      creation={{ mode, gpuCount, nodeId, name: instanceName, disk: dataDiskGiB, placement, placementError, canOrder, submitting, onMode: setMode, onCount: count => { setGpuCount(count); if (count === 8) setDataDiskGiB(0); }, onNode: setNodeId, onName: setInstanceName, onDisk: setDataDiskGiB, onOrder: () => void order() }}
      notices={<>
        {notice && <output className={`rental-notice rental-notice-${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}>{notice.tone === 'success' ? <CheckCircle2 size={18} /> : notice.tone === 'error' ? <XCircle size={18} /> : <CircleHelp size={18} />}<span>{notice.text}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示"><XCircle size={16} /></button></output>}
        {state.service !== 'ready' && !loading && <div className="rental-notice rental-notice-info"><CircleHelp size={17} />{state.serviceMessage}</div>}
        {estimatedHours !== null && estimatedHours < 2 && <div className="rental-notice rental-notice-error"><Wallet size={17} /><span>按当前用量，余额预计不足 2 小时。余额耗尽后自动关机。</span><button className="rental-small-button" onClick={() => selectTab('wallet')}>前往充值</button></div>}
      </>}
    >
      {tab === 'instances' && <section className="rental-card customer-instances" aria-label="我的实例列表">
        <div className="rental-list-tools"><label><Search size={16} /><input aria-label="搜索实例" placeholder="搜索名称、实例 ID 或节点" value={search} onChange={event => { setSearch(event.target.value); setInstancePage(1); }} /></label><select aria-label="筛选实例状态" value={filter} onChange={event => { setFilter(event.target.value as CustomerFilter); setInstancePage(1); }}><option value="all">全部状态</option><option value="running">运行中</option><option value="stopped">已关机</option><option value="pending">处理中</option><option value="attention">需要关注</option></select><span className="customer-list-count">{loading ? '读取中…' : `共 ${visibleInstances.length} 台`}</span></div>
        {loading ? <div className="customer-loading"><output>正在读取实例状态…</output><div /><div /></div> : visibleInstances.length === 0 ? <div className="customer-empty"><Server size={26} /><h2>{state.instances.length ? '没有匹配的实例' : '准备好你的第一个环境'}</h2><p>{state.instances.length ? '试试其他名称、节点或状态。' : '选择 Gaudi2 计算套餐，或先用无头模式配置环境。'}</p><button className="rental-small-button" onClick={() => { if (state.instances.length) { setSearch(''); setFilter('all'); setInstancePage(1); } else selectTab('create'); }}>{state.instances.length ? '清除筛选' : '创建第一个实例'}<ArrowRight size={15} /></button></div> : <div className="rental-instance-list">{displayedInstances.map(instance => <InstanceRow key={instance.id} instance={instance} actionId={actionId} onAction={action} onPlanChange={changeGpuPlan} planChangeBlocked={state.service === 'maintenance'} gpuStartReason={startReason(instance, 'gpu')} headlessStartReason={startReason(instance, 'headless')} gpuRate={state.billing.rateCentsPerHour ?? 0} balanceCents={state.account.balanceCents} />)}</div>}
        {!loading && visibleInstances.length > 0 && <AdminPagination page={currentInstancePage} pages={instancePages} total={visibleInstances.length} fullTotal={state.instances.length} onPage={setInstancePage} />}
      </section>}
      {tab === 'wallet' && <>{account.role === 'customer' && <div className="customer-wallet-layout"><RedeemCodePanel onRedeemed={() => { setWalletRevision(value => value + 1); void refresh(); }} /><RechargeContactBanner compact /></div>}<LedgerPanel key={walletRevision} /></>}
      {confirmation && <ConfirmDialog title={confirmation.operation === 'delete' ? '永久释放实例？' : '关闭实例？'} detail={confirmation.operation === 'delete' ? `将删除 ${confirmation.name} (#${confirmation.id}) 的系统盘和数据盘，无法恢复。请先备份重要数据。` : `${confirmation.name} (#${confirmation.id}) 中的任务将中断。关机后停止算力计费，数据盘继续计费；48 小时未开机将自动删除。系统无响应时会在等待超时后强制关机（四卡最多等待 5 分钟）。`} danger={confirmation.operation === 'delete'} onCancel={() => setConfirmation(null)} onConfirm={() => void action(confirmation.id, confirmation.operation, undefined, true)} />}
    </CustomerWorkspace>
    </>
  );
}

function RentalBrand() {
  return <div className="rental-brand"><BrandLogo /><div className="rental-brand-detail"><strong>1CatDL</strong><span>GAUDI GPU CLOUD</span></div></div>;
}

type RegistrationBonusSettings = { bonusCents: number; maxBonusCents: number };

function AdminRegistrationBonus() {
  const [settings, setSettings] = useState<RegistrationBonusSettings | null>(null);
  const [amount, setAmount] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ error: boolean; text: string } | null>(null);
  const [pending, setPending] = useState<{ cents: number; expectedCents: number } | null>(null);
  const inFlight = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setMessage(null);
    try {
      const next = await rentalRequest<RegistrationBonusSettings>('/api/admin/registration-bonus');
      setSettings(next);
      setAmount((next.bonusCents / 100).toFixed(2));
    } catch (error) {
      setMessage({ error: true, text: error instanceof Error ? error.message : '读取注册赠金设置失败' });
    } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => { void load(); }, 0);
    return () => window.clearTimeout(initial);
  }, [load]);

  const confirmSave = () => {
    if (!settings || loading || busy) return;
    if (!/^(?:0|[1-9]\d{0,4})(?:\.\d{1,2})?$/.test(amount.trim())) {
      setMessage({ error: true, text: '请输入 0–10000 元的金额，最多两位小数；0 表示关闭赠送。' });
      return;
    }
    const [yuan, fraction = ''] = amount.trim().split('.');
    const cents = Number(yuan) * 100 + Number(fraction.padEnd(2, '0'));
    if (cents > settings.maxBonusCents) {
      setMessage({ error: true, text: `注册赠金不能超过 ${formatMoney(settings.maxBonusCents)}。` });
      return;
    }
    setMessage(null);
    setPending({ cents, expectedCents: settings.bonusCents });
  };

  const save = async () => {
    if (!pending || inFlight.current) return;
    const confirmed = pending;
    inFlight.current = true;
    setPending(null);
    setBusy(true);
    try {
      const next = await rentalRequest<RegistrationBonusSettings>('/api/admin/registration-bonus', {
        method: 'POST', body: JSON.stringify(confirmed),
      });
      setSettings(next);
      setAmount((next.bonusCents / 100).toFixed(2));
      setMessage({ error: false, text: next.bonusCents === 0 ? '已关闭注册赠送。已有账户余额不变。' : `已保存：之后新注册的客户赠送 ${formatMoney(next.bonusCents)}，每个账户仅一次。已有账户余额不变。` });
    } catch (error) {
      setMessage({ error: true, text: `${error instanceof Error ? error.message : '保存失败'}。可重新加载核对当前设置。` });
    } finally { inFlight.current = false; setBusy(false); }
  };

  return <section className="rental-card rental-registration-settings" aria-labelledby="registration-bonus-title">
    <div className="rental-card-head"><div><div className="rental-kicker">ADMIN / REGISTRATION</div><h2 id="registration-bonus-title">注册赠送额度</h2></div><span className="rental-step-chip">{loading ? '读取中…' : settings ? settings.bonusCents > 0 ? `已开启 · ${formatMoney(settings.bonusCents)} / 新账户` : '已关闭' : '暂不可用'}</span></div>
    <div className="rental-admin-body">
      <p className="rental-registration-description">新客户注册成功后自动入账，每个账户仅赠送一次。仅对保存后注册的客户生效，不补发、不扣回已有余额。</p>
      <form className="rental-registration-form" onSubmit={(event) => { event.preventDefault(); confirmSave(); }}>
        <label className="rental-field"><span>每个新账户赠送金额（元）</span><input aria-describedby="registration-bonus-help" type="number" inputMode="decimal" min="0" max={settings ? settings.maxBonusCents / 100 : 10000} step="0.01" placeholder="例如 20.00" value={amount} disabled={loading || busy || !settings} onChange={(event) => setAmount(event.target.value)} /></label>
        <button className="rental-small-button" disabled={loading || busy || !settings}>{busy ? '保存中…' : '保存注册赠金'}</button>
        <button type="button" className="rental-small-button" disabled={loading || busy} onClick={() => void load()}>重新加载</button>
      </form>
      <p id="registration-bonus-help" className="rental-billing-info">设为 0 即关闭；支持 0.01 元精度，上限 ¥10,000。赠金属于活动额度，不计入实收充值；修改会记录操作审计。</p>
      {message && <div role={message.error ? 'alert' : 'status'} className={`rental-code-message ${message.error ? 'rental-text-error' : 'rental-code-success'}`}>{message.text}</div>}
    </div>
    {pending && <ConfirmDialog title={pending.cents === 0 ? '确认关闭注册赠送？' : '确认修改注册赠金？'} detail={`将从 ${formatMoney(pending.expectedCents)} 改为 ${formatMoney(pending.cents)} / 新账户。仅影响之后注册的客户，已有余额不变。`} confirmLabel="确认保存" onCancel={() => setPending(null)} onConfirm={() => void save()} />}
  </section>;
}

function RechargeContactBanner({ compact = false }: { compact?: boolean }) {
  return <aside className={`rental-recharge-contact${compact ? ' rental-recharge-contact-compact' : ''}`} aria-label="充值管理员联系方式">
    <div className="rental-recharge-contact-lead"><span className="rental-recharge-contact-icon"><MessageCircle size={19} /></span><div><strong>充值请联系管理员</strong><span>确认到账后，管理员会发放充值码，兑换即可入账。</span></div></div>
    <div className="rental-recharge-contact-list"><div><span>TCat · 微信号</span><b>MTCat03</b></div><div><span>ISI</span><b>YM_isi</b></div></div>
  </aside>;
}

function AuthGate({ mode, setMode, name, setName, password, setPassword, submitting, onSubmit, notice }: {
  mode: 'login' | 'register';
  setMode: (mode: 'login' | 'register') => void;
  name: string;
  setName: (value: string) => void;
  password: string;
  setPassword: (value: string) => void;
  submitting: boolean;
  onSubmit: () => void;
  notice: { tone: 'info' | 'success' | 'error'; text: string } | null;
}) {
  const [bonusCents, setBonusCents] = useState<number | null>(null);
  useEffect(() => {
    if (mode !== 'register') return;
    let disposed = false;
    void rentalRequest<{ bonusCents: number }>('/api/auth/registration-policy')
      .then((policy) => { if (!disposed) setBonusCents(policy.bonusCents); })
      .catch(() => { if (!disposed) setBonusCents(null); });
    return () => { disposed = true; };
  }, [mode]);
  const registrationDescription = bonusCents === null
    ? '注册后管理实例与余额，赠送额度以注册完成时的规则为准。'
    : bonusCents > 0
      ? `新客户注册赠送 ${formatMoney(bonusCents)}，每个账户一次。以注册完成时的规则为准。`
      : '当前无注册赠金。注册后可兑换充值码，余额充足后开机。';
  return <main className="rental-app customer-ui customer-auth"><div className="customer-auth-shell"><RentalBrand /><div className="customer-auth-layout">
    <section className="customer-auth-form"><h1>{mode === 'login' ? '登录工作台' : '注册客户账户'}</h1><p>{mode === 'login' ? '管理你的实例、余额与 SSH 连接。' : registrationDescription}</p>
      {notice && <div className={`rental-notice rental-notice-${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}>{notice.text}</div>}
      <form onSubmit={event => { event.preventDefault(); if (!submitting && name.trim() && password) onSubmit(); }}>
        <label className="rental-field"><span>账户名</span><input value={name} autoComplete="username" required onChange={event => setName(event.target.value)} /></label>
        <label className="rental-field"><span>密码</span><input type="password" value={password} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} required onChange={event => setPassword(event.target.value)} /></label>
        <button className="rental-primary-button" type="submit" disabled={submitting || !name.trim() || !password}>{submitting ? '处理中…' : mode === 'login' ? '登录' : '创建账户'}{!submitting && <ArrowRight size={16} />}</button>
      </form>
      <button className="rental-auth-switch" type="button" onClick={() => setMode(mode === 'login' ? 'register' : 'login')}>{mode === 'login' ? '还没有账户？立即注册' : '已有账户？返回登录'}</button>
    </section>
    <aside className="customer-auth-support"><h2>按需开启你的计算环境</h2><p>Gaudi2 单卡 / 四卡计算，或不占显卡的无头环境。</p><ol><li><strong>创建环境</strong><span>选择套餐与数据盘，系统盘已包含。</span></li><li><strong>按需开机</strong><span>余额充足后启动，开机成功开始计费。</span></li><li><strong>SSH 连接</strong><span>在实例中查看连接命令与密码。</span></li></ol><RechargeContactBanner compact /></aside>
  </div></div></main>;
}

function AdminAccountPanel({ active, revision: externalRevision, query, status, onQuery, onStatus, onReset, onChanged, onRecharge, onInstances, onLedger }: AdminAccountProps) {
  const [customers, setCustomers] = useState<AdminCustomer[]>([]);
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirmation, setConfirmation] = useState<{ customer: AdminCustomer; action: 'delete' | 'restore' } | null>(null);
  const [editingCustomer, setEditingCustomer] = useState<AdminCustomer | null>(null);
  const [quotaMode, setQuotaMode] = useState<'unlimited' | 'limited'>('unlimited');
  const [quotaValue, setQuotaValue] = useState('1');

  useEffect(() => {
    if (!active) return;
    let disposed = false;
    const abort = new AbortController();
    const timer = window.setTimeout(() => {
      setLoading(true);
      void rentalRequest<{ customers: AdminCustomer[] }>(`/api/admin/customers?status=${status}`, { signal: abort.signal })
        .then((data) => { if (!disposed) setCustomers(data.customers); })
        .catch((error) => { if (!disposed) { setCustomers([]); setMessage({ ok: false, text: error instanceof Error ? error.message : '读取账户失败' }); } })
        .finally(() => { if (!disposed) setLoading(false); });
    }, 0);
    return () => { disposed = true; clearTimeout(timer); abort.abort(); };
  }, [active, status, revision, externalRevision]);

  const perform = async () => {
    if (!confirmation || inFlight.current) return;
    const { customer, action } = confirmation;
    inFlight.current = true;
    setBusy(true); setConfirmation(null); setMessage(null);
    try {
      await rentalRequest(`/api/admin/customers/${encodeURIComponent(customer.name)}/${action}`, {
        method: 'POST', body: JSON.stringify(action === 'delete' ? { confirmName: customer.name } : {}),
      });
      setMessage({ ok: true, text: action === 'delete' ? `已删除账户 ${customer.name}，登录会话已失效；可在“已删除”中恢复。` : `已恢复账户 ${customer.name}，客户需重新登录，余额未变。` });
      onChanged();
    } catch (error) {
      setMessage({ ok: false, text: error instanceof Error ? error.message : '账户操作失败' });
    } finally {
      setRevision((value) => value + 1);
      inFlight.current = false; setBusy(false);
    }
  };
  const editQuota = (customer: AdminCustomer) => {
    setEditingCustomer(customer);
    setQuotaMode(customer.gpuInstanceLimit == null ? 'unlimited' : 'limited');
    setQuotaValue(String(customer.gpuInstanceLimit ?? 1));
    setMessage(null);
  };
  const saveQuota = async () => {
    if (!editingCustomer || inFlight.current) return;
    const limit = quotaMode === 'unlimited' ? null : /^\d+$/.test(quotaValue) ? Number(quotaValue) : NaN;
    if (limit !== null && (!Number.isInteger(limit) || limit < 0 || limit > 10000)) {
      setMessage({ ok: false, text: 'GPU 并发上限请输入 0–10000 的整数。' }); return;
    }
    inFlight.current = true; setBusy(true); setMessage(null);
    try {
      await rentalRequest(`/api/admin/customers/${encodeURIComponent(editingCustomer.name)}/gpu-limit`, {
        method: 'POST', body: JSON.stringify({ limit, expectedLimit: editingCustomer.gpuInstanceLimit }),
      });
      setMessage({ ok: true, text: `${editingCustomer.name} 的 GPU 并发配额已设为${limit === null ? '不限' : `${limit} 台`}。` });
      setEditingCustomer(null); onChanged();
    } catch (error) {
      setMessage({ ok: false, text: error instanceof Error ? error.message : '保存 GPU 配额失败' });
    } finally {
      setRevision(value => value + 1); inFlight.current = false; setBusy(false);
    }
  };
  const visible = customers.filter((customer) => customer.name.toLowerCase().includes(query.trim().toLowerCase()));
  const pageCount = Math.max(1, Math.ceil(visible.length / 15));
  const currentPage = Math.min(page, pageCount);
  const paged = visible.slice((currentPage - 1) * 15, currentPage * 15);
  return <section className="rental-card rental-account-card" aria-label="客户账户">
    <div className="rental-list-tools"><label><Search size={14} /><input aria-label="搜索客户账户" placeholder="搜索账户名" value={query} onChange={(event) => { onQuery(event.target.value); setPage(1); }} /></label><select aria-label="账户状态" value={status} disabled={busy} onChange={(event) => { onStatus(event.target.value); setPage(1); setLoading(true); setMessage(null); }}><option value="active">正常账户</option><option value="deleted">已删除</option><option value="all">全部账户</option></select><button className="admin-link" onClick={() => { onReset(); setPage(1); }}>重置筛选</button></div>
    {message && <div role={message.ok ? 'status' : 'alert'} className={`rental-account-message ${message.ok ? 'rental-code-success' : 'rental-text-error'}`}>{message.text}</div>}
    <section className="rental-table-wrap" aria-label="客户列表，可横向滚动"><table className="rental-table"><thead><tr><th>账户</th><th>余额</th><th>保留实例</th><th>GPU 并发</th><th>状态</th><th>注册 / 删除时间</th><th>操作</th></tr></thead><tbody>
      {loading ? <tr><td colSpan={7}>正在读取账户…</td></tr> : visible.length === 0 ? <tr><td colSpan={7}>{query ? '没有匹配的账户' : status === 'deleted' ? '暂无已删除账户' : '暂无客户账户'}</td></tr> : paged.map((customer) => <Fragment key={customer.name}><tr>
        <td><strong>{customer.name}</strong></td><td>{formatMoney(customer.balanceCents)}</td><td><button className="admin-link" aria-label={`查看 ${customer.name} 的实例`} onClick={() => onInstances(customer.name)}>{customer.instanceCount} 台</button></td><td><span className="admin-quota-count">{customer.gpuActiveCount} / {customer.gpuInstanceLimit == null ? '不限' : customer.gpuInstanceLimit}</span>{!customer.deletedAt && <button className="admin-link" disabled={busy} aria-label={`设置 ${customer.name} 的 GPU 并发配额`} aria-expanded={editingCustomer?.name === customer.name} onClick={() => editQuota(customer)}>设置</button>}</td><td><span className={`rental-account-status ${customer.deletedAt ? 'is-deleted' : ''}`}>{customer.deletedAt ? '已删除' : '正常'}</span></td><td>{formatDate(customer.createdAt)}{customer.deletedAt && <small className="rental-account-meta">删除于 {formatDate(customer.deletedAt)} · {customer.deletedBy}</small>}</td>
        <td><div className="admin-row-actions">{!customer.deletedAt && <button className="rental-small-button" disabled={busy} aria-label={`充值给 ${customer.name}`} onClick={() => onRecharge(customer)}>充值</button>}<button className="admin-link" onClick={() => onLedger(customer.name)} aria-label={`查看 ${customer.name} 的账单`}>账单</button>{customer.deletedAt ? <button className="rental-small-button" disabled={busy} aria-label={`恢复账户 ${customer.name}`} onClick={() => setConfirmation({ customer, action: 'restore' })}>恢复</button> : <details className="admin-more"><summary aria-label={`账户 ${customer.name} 更多操作`}>更多</summary><div><button className="admin-danger" disabled={busy || customer.instanceCount > 0} title={customer.instanceCount > 0 ? '请先释放该账户全部实例，包括已关机实例' : '可恢复删除，账单与余额保留'} aria-label={`删除账户 ${customer.name}`} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); setConfirmation({ customer, action: 'delete' }); }}>删除账户</button>{customer.instanceCount > 0 && <small>须先释放全部实例</small>}</div></details>}</div></td>
      </tr>{editingCustomer?.name === customer.name && !customer.deletedAt && <tr className="admin-quota-editor-row"><td colSpan={7}><form className="admin-quota-editor" onSubmit={event => { event.preventDefault(); void saveQuota(); }} aria-label={`${customer.name} GPU 并发配额`}>
        <div><strong>设置 {customer.name} 的 GPU 并发配额</strong><p>按同时占用 GPU 的实例台数计算，1/4/8 卡各算 1 台；无头实例不占配额。降低上限不会关闭已运行实例。</p></div>
        <div className="admin-quota-options"><label><input type="radio" name={`quota-${customer.name}`} checked={quotaMode === 'unlimited'} disabled={busy} onChange={() => setQuotaMode('unlimited')} />不限</label><label><input type="radio" name={`quota-${customer.name}`} checked={quotaMode === 'limited'} disabled={busy} onChange={() => setQuotaMode('limited')} />最多 <input type="number" min="0" max="10000" step="1" value={quotaValue} disabled={busy || quotaMode !== 'limited'} aria-label="GPU 并发台数" onChange={event => setQuotaValue(event.target.value)} /> 台</label></div>
        <div className="admin-quota-actions"><button className="rental-small-button" type="submit" disabled={busy || (quotaMode === 'limited' && (!/^\d+$/.test(quotaValue) || Number(quotaValue) > 10000))}>{busy ? '保存中…' : '保存配额'}</button><button className="admin-link" type="button" disabled={busy} onClick={() => setEditingCustomer(null)}>取消</button></div>
      </form></td></tr>}</Fragment>)}
    </tbody></table></section>
    <AdminPagination page={currentPage} pages={pageCount} total={visible.length} fullTotal={customers.length} onPage={setPage} />
    <details className="admin-help"><summary>账户删除规则</summary><p>删除会禁止登录并保留余额和历史账单，可恢复；须先释放全部实例，不会自动退款。</p></details>
    {customers.length >= 500 && <p className="rental-account-help">当前显示最近注册的 500 个符合条件的账户。</p>}
    {confirmation && <ConfirmDialog title={confirmation.action === 'delete' ? `删除账户 ${confirmation.customer.name}？` : `恢复账户 ${confirmation.customer.name}？`} detail={confirmation.action === 'delete' ? `该客户会立即退出登录，之后不能登录、下单、充值或兑换充值码。余额 ${formatMoney(confirmation.customer.balanceCents)} 和历史账目保留，不自动退款；账户名不能重新注册。可从“已删除”中恢复。` : `恢复后客户可用原密码重新登录，保留原余额，不重新发放注册赠金，也不会恢复已释放的实例。`} danger={confirmation.action === 'delete'} confirmLabel={confirmation.action === 'delete' ? '确认删除账户' : '确认恢复账户'} requiredText={confirmation.action === 'delete' ? confirmation.customer.name : undefined} onCancel={() => setConfirmation(null)} onConfirm={() => void perform()} />}
  </section>;
}


type RechargeCode = { id: number; batchId: string; hint: string; cents: number; kind: 'cash' | 'gift'; boundOwner: string | null; note: string; createdBy: string; createdAt: string; redeemedBy: string | null; redeemedAt: string | null; status: 'available' | 'redeemed' | 'revoked'; code?: string };
type CodeIssue = { batchId: string; replayed: boolean; codes: RechargeCode[] };
type CodeList = { codes: RechargeCode[]; nextCursor: number | null; summary: { total: number; availableCents: number; redeemedCashCents: number; redeemedGiftCents: number } };

function RedeemCodePanel({ onRedeemed }: { onRedeemed: () => void }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const redeem = async () => {
    if (submitting.current || !code.trim()) return;
    submitting.current = true; setBusy(true); setMessage(null);
    try {
      const { result } = await rentalRequest<{ result: { cents: number; kind: string; balanceCents: number; alreadyRedeemed: boolean } }>('/api/rental/redeem-code', { method: 'POST', body: JSON.stringify({ code: code.trim() }) });
      setMessage({ ok: true, text: result.alreadyRedeemed ? `这张码已兑换到你的账户，没有重复入账。当前余额 ${formatMoney(result.balanceCents)}。` : `${result.kind === 'gift' ? '赠送额度' : '充值'} ${formatMoney(result.cents)} 已到账，当前余额 ${formatMoney(result.balanceCents)}。` });
      setCode(''); onRedeemed();
    } catch (error) { setMessage({ ok: false, text: error instanceof Error ? error.message.replace(/^\d{3}:\s*/, '') : '兑换失败，请稍后重试' }); }
    finally { setBusy(false); submitting.current = false; }
  };
  return <section className="rental-card rental-code-card">
    <div className="rental-card-head"><h2>兑换充值码</h2><Wallet size={18} /></div>
    <div className="rental-code-body"><p className="rental-code-intro">粘贴管理员发给你的充值码，验证成功后立即入账。</p>
      <form onSubmit={(event) => { event.preventDefault(); void redeem(); }} className="rental-redeem-form"><label className="rental-field"><span>充值码</span><input aria-label="充值码" value={code} maxLength={128} autoComplete="off" autoCapitalize="characters" spellCheck={false} disabled={busy} placeholder="1CAT-XXXX-XXXX-…" onChange={(event) => setCode(event.target.value)} /></label><button className="rental-primary-button" disabled={busy || !code.trim()}>{busy ? <><LoaderCircle size={16} className="rental-spin" />正在验证</> : '兑换并入账'}</button></form>
      {message && <div role={message.ok ? 'status' : 'alert'} className={`rental-code-message ${message.ok ? 'rental-code-success' : 'rental-text-error'}`}>{message.text}</div>}
      <p className="rental-code-tip"><ShieldCheck size={14} />每张码仅可兑换一次。绑定码仅限指定账户；赠送额度不可退款。余额可用于算力与数据盘费用。</p>
    </div>
  </section>;
}

function AdminCodePanel({ active, customerRevision }: { active: boolean; customerRevision: number }) {
  const [formOpen, setFormOpen] = useState(false);
  const [amount, setAmount] = useState('100');
  const [count, setCount] = useState('1');
  const [kind, setKind] = useState<'cash' | 'gift'>('cash');
  const [bound, setBound] = useState('');
  const [note, setNote] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const [customers, setCustomers] = useState<AdminCustomer[]>([]);
  const [result, setResult] = useState<CodeIssue | null>(null);
  const [list, setList] = useState<CodeList | null>(null);
  const [status, setStatus] = useState('all');
  const [query, setQuery] = useState('');
  const [before, setBefore] = useState(0);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const issueKey = useRef<{ key: string; intent: string } | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [confirmation, setConfirmation] = useState<{ type: 'issue' | 'revoke' | 'batch' | 'hide'; id?: number; batchId?: string } | null>(null);
  const cents = /^\d+(\.\d{1,2})?$/.test(amount) ? Math.round(Number(amount) * 100) : 0;
  const quantity = /^\d+$/.test(count) ? Number(count) : 0;
  const total = cents * quantity;
  const hasPlaintext = !!result?.codes.some((row) => row.code);
  const unavailableBound = !!bound && !customers.some((customer) => customer.name === bound);
  const canIssue = cents >= 1 && cents <= 1_000_000 && quantity >= 1 && quantity <= 100 && total <= 10_000_000 && acknowledged && (kind === 'gift' || !!note.trim()) && !busy && !hasPlaintext && !unavailableBound;

  useEffect(() => {
    if (!hasPlaintext) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener('beforeunload', guard);
    return () => window.removeEventListener('beforeunload', guard);
  }, [hasPlaintext]);
  useEffect(() => {
    if (!active) return;
    let disposed = false;
    const abort = new AbortController();
    const timer = setTimeout(() => {
      setLoading(true);
      void rentalRequest<CodeList>(`/api/admin/recharge-codes?before=${before}&status=${status}&q=${encodeURIComponent(query)}`, { signal: abort.signal }).then((value) => { if (!disposed) setList(value); }).catch((e) => { if (!disposed) setError(e instanceof Error ? e.message : '读取充值码失败'); }).finally(() => { if (!disposed) setLoading(false); });
    }, 250);
    return () => { disposed = true; clearTimeout(timer); abort.abort(); };
  }, [active, before, status, query, revision]);
  useEffect(() => {
    if (!active) return;
    let disposed = false;
    void rentalRequest<{ customers: AdminCustomer[] }>('/api/admin/customers').then((data) => { if (!disposed) setCustomers(data.customers); }).catch((e) => { if (!disposed) setError(e instanceof Error ? e.message : '读取客户失败'); });
    return () => { disposed = true; };
  }, [active, customerRevision]);

  const perform = async () => {
    if (!confirmation || inFlight.current) return;
    const action = confirmation;
    setConfirmation(null); setError('');
    if (action.type === 'hide') { setResult(null); return; }
    inFlight.current = true; setBusy(true);
    try {
      if (action.type === 'issue') {
        const intent = JSON.stringify({ cents, count: quantity, kind, boundOwner: bound || null, note: note.trim() });
        if (!issueKey.current || issueKey.current.intent !== intent) issueKey.current = { key: requestKey(), intent };
        const issued = await rentalRequest<CodeIssue>('/api/admin/recharge-codes', { method: 'POST', headers: { 'X-Idempotency-Key': issueKey.current.key }, body: intent });
        setResult(issued); issueKey.current = null; setAcknowledged(false); setBefore(0);
      } else {
        await rentalRequest(`/api/admin/${action.type === 'batch' ? `recharge-code-batches/${action.batchId}` : `recharge-codes/${action.id}`}/revoke`, { method: 'POST' });
        if (result && (action.type === 'batch' && action.batchId === result.batchId || action.type === 'revoke' && result.codes.some((row) => row.id === action.id))) {
          setResult({ ...result, codes: result.codes.map((row) => action.type === 'batch' || row.id === action.id ? { ...row, code: undefined, status: row.status === 'redeemed' ? 'redeemed' : 'revoked' } : row) });
        }
      }
      setRevision((value) => value + 1);
    } catch (e) { setError(e instanceof Error ? e.message : '操作未完成，请重试同一请求'); }
    finally { inFlight.current = false; setBusy(false); }
  };
  const download = () => {
    if (!result || !hasPlaintext) return;
    const lines = ['1CAT 充值兑换码 · 请妥善保存，不要公开', `批次：${result.batchId}`, ...result.codes.filter((row) => row.code).map((row) => `#${row.id}\t${row.kind === 'gift' ? '赠送' : '已收款'}\t${formatMoney(row.cents)}\t${row.boundOwner || '不绑定账户'}\t${row.code}`)];
    const url = URL.createObjectURL(new Blob(['\uFEFF'+lines.join('\r\n')], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = `1cat-recharge-${result.batchId}.txt`; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const statusText = { available: '未兑换', redeemed: '已兑换', revoked: '已作废' };
  return <section className="rental-card rental-code-card">
    <div className="rental-card-head"><h2>充值码</h2><button className="admin-primary-button" disabled={busy || hasPlaintext} aria-expanded={formOpen || !!result} onClick={() => setFormOpen(!formOpen)}>{formOpen ? '收起表单' : <><Plus size={15} />生成充值码</>}</button></div>
    {error && !formOpen && !result && <p role="alert" className="rental-code-message rental-text-error">{error}</p>}
    <div className="rental-code-body" hidden={!formOpen && !result}><p className="rental-code-intro">核实收款或批准赠送后发码。生成时不增加余额，客户兑换后才入账。</p>
      <fieldset className="rental-code-fields" disabled={busy || hasPlaintext}>
        <label className="rental-field"><span>单张金额（元）</span><input aria-label="单张金额" type="number" min="0.01" max="10000" step="0.01" value={amount} onChange={(e) => { setAmount(e.target.value); setAcknowledged(false); }} /></label>
        <label className="rental-field"><span>生成数量</span><input aria-label="生成数量" type="number" min="1" max="100" step="1" value={count} onChange={(e) => { setCount(e.target.value); setAcknowledged(false); }} /></label>
        <label className="rental-field"><span>额度类型</span><select value={kind} onChange={(e) => { setKind(e.target.value as 'cash' | 'gift'); setAcknowledged(false); }}><option value="cash">已收款充值</option><option value="gift">赠送额度（不可退款）</option></select></label>
        <label className="rental-field"><span>绑定客户（可选）</span><select value={bound} onChange={(e) => { setBound(e.target.value); setAcknowledged(false); }}><option value="">不绑定 · 持码客户可兑换</option>{unavailableBound && <option value={bound} disabled>{bound} · 已删除或不可用，请重新选择</option>}{customers.map((row) => <option key={row.name} value={row.name}>{row.name}</option>)}</select></label>
        <label className="rental-field rental-code-note"><span>{kind === 'cash' ? '收款编号 / 核验备注（必填，仅管理员可见）' : '发放备注（仅管理员可见）'}</span><input maxLength={120} value={note} onChange={(e) => { setNote(e.target.value); setAcknowledged(false); }} placeholder={kind === 'cash' ? '例如：已核实到账的商户流水编号，不填密码或密钥' : '例如：活动赠送'} /></label>
      </fieldset>
      <div className="rental-code-checkout"><div><span>本批额度</span><strong>{formatMoney(Number.isFinite(total) ? total : 0)}</strong><small>最多 100 张 / 批；每批不超过 ¥100,000</small></div><label><input type="checkbox" checked={acknowledged} disabled={busy || hasPlaintext} onChange={(e) => setAcknowledged(e.target.checked)} />{kind === 'cash' ? '我已核实实际到账，且与本批额度一致' : '我确认批准发放这批赠送额度'}</label><button className="rental-primary-button" disabled={!canIssue} onClick={() => setConfirmation({ type: 'issue' })}>{busy ? '处理中…' : '生成充值码'}</button></div>
      {error && <p role="alert" className="rental-code-message rental-text-error">{error}</p>}
      {result && <div className="rental-code-result"><h3>{result.replayed ? '该批已生成，没有重复发码' : '充值码已生成'}</h3><p>{hasPlaintext ? '明文仅在本次生成时返回，请立即复制或下载。切换工作台页签会保留，刷新或退出登录后无法再次查看。' : result.replayed ? '为保护额度，无法重新取回明文。若此前未保存，请作废本批未兑换码后重新生成。' : '明文已隐藏或码已作废，可在下方查看状态。'}</p><small>批次 {result.batchId}</small>
        {hasPlaintext && <><textarea aria-label="本次生成的充值码" readOnly spellCheck={false} value={result.codes.filter((row) => row.code).map((row) => row.code).join('\n')} /><div className="rental-code-buttons"><CopyButton value={result.codes.filter((row) => row.code).map((row) => row.code).join('\n')} label="复制全部充值码" /><button className="rental-small-button" onClick={download}>下载发放清单</button><button className="rental-small-button" onClick={() => setConfirmation({ type: 'hide' })}>已保存，隐藏明文</button></div></>}
        <button disabled={busy} className="rental-small-button rental-small-danger" onClick={() => setConfirmation({ type: 'batch', batchId: result.batchId })}>作废本批未兑换码</button>
      </div>}
      <p className="rental-code-tip">充值码是余额凭证，请通过私密渠道发放。后台只保存校验摘要，列表不能找回完整码；已兑换的码不能作废或再次入账。</p>
    </div>
    <div className="rental-code-summary"><span>生成总数 <b>{list?.summary.total ?? 0}</b></span><span>未兑换面额 <b>{formatMoney(list?.summary.availableCents ?? 0)}</b></span><span>收款码已入账 <b>{formatMoney(list?.summary.redeemedCashCents ?? 0)}</b></span><span>赠送码已入账 <b>{formatMoney(list?.summary.redeemedGiftCents ?? 0)}</b></span></div>
    <div className="rental-list-tools"><label><Search size={15} /><input aria-label="搜索充值码" placeholder="搜索客户、编号、备注或批次" value={query} onChange={(e) => { setQuery(e.target.value); setBefore(0); }} /></label><select aria-label="充值码状态" value={status} onChange={(e) => { setStatus(e.target.value); setBefore(0); }}><option value="all">全部状态</option><option value="available">未兑换</option><option value="redeemed">已兑换</option><option value="revoked">已作废</option></select><button className="rental-small-button" onClick={() => setRevision((value) => value + 1)} disabled={loading}><RefreshCw size={14} />刷新</button></div>
    <div className="rental-table-wrap"><table className="rental-table"><thead><tr><th>编号 / 尾号</th><th>面额 / 类型</th><th>绑定 / 兑换客户</th><th>状态</th><th>创建 / 兑换时间</th><th>备注</th><th>操作</th></tr></thead><tbody>{list?.codes.map((row) => <tr key={row.id}><td>#{row.id}<small className="rental-code-sub">{row.hint}</small></td><td>{formatMoney(row.cents)}<small className="rental-code-sub">{row.kind === 'cash' ? '已收款充值' : '赠送额度'}</small></td><td>{row.boundOwner || '不限账户'}<small className="rental-code-sub">{row.redeemedBy ? `兑换：${row.redeemedBy}` : '尚未兑换'}</small></td><td><span className={`rental-code-status rental-code-status-${row.status}`}>{statusText[row.status]}</span></td><td>{formatDate(row.createdAt)}{row.redeemedAt && <small className="rental-code-sub">{formatDate(row.redeemedAt)}</small>}</td><td className="rental-code-note-cell">{row.note || '—'}<small className="rental-code-sub">由 {row.createdBy} 创建</small></td><td>{row.status === 'available' ? <button className="rental-small-button rental-small-danger" disabled={busy} onClick={() => setConfirmation({ type: 'revoke', id: row.id })}>作废</button> : '—'}</td></tr>)}</tbody></table></div>
    {!list?.codes.length && <div className="rental-empty">{loading ? '正在读取充值码…' : '暂无匹配的充值码'}</div>}
    <div className="rental-code-pagination"><span>每页 50 张 · 汇总为全平台充值码累计，不等同于实收对账</span>{before > 0 && <button className="rental-small-button" onClick={() => setBefore(0)}>返回第一页</button>}{list?.nextCursor && <button className="rental-small-button" disabled={loading} onClick={() => setBefore(list.nextCursor!)}>下一页</button>}</div>
    {confirmation && <ConfirmDialog title={confirmation.type === 'issue' ? '确认生成充值码' : confirmation.type === 'hide' ? '已妥善保存完整充值码？' : '确认作废未兑换码'} detail={confirmation.type === 'issue' ? `生成 ${quantity} 张 ${formatMoney(cents)} 的${kind === 'cash' ? '已收款充值码' : '赠送码'}，合计 ${formatMoney(total)}；${bound ? `仅限客户 ${bound} 兑换` : '任何持码客户均可兑换'}。仅首次返回明文，请及时保存。` : confirmation.type === 'hide' ? '隐藏后无法重新查看完整码，已生成的码仍然有效。如未保存，请取消并先下载清单。' : confirmation.type === 'batch' ? '本批所有未兑换码将失效，已兑换的余额不受影响。此操作不能撤销。' : `未兑换码 #${confirmation.id} 将永久失效。如果已被客户兑换，不会撤回客户余额。`} danger={confirmation.type === 'revoke' || confirmation.type === 'batch'} confirmLabel={confirmation.type === 'issue' ? '确认生成' : confirmation.type === 'hide' ? '已保存，确认隐藏' : '确认作废'} onCancel={() => setConfirmation(null)} onConfirm={() => void perform()} />}
  </section>;
}

function InstanceRow({ instance, actionId, onAction, onPlanChange, planChangeBlocked = false, gpuStartReason = '', headlessStartReason = '', gpuRate, balanceCents }: { instance: RentalInstance; actionId: string | null; planChangeBlocked?: boolean; gpuStartReason?: string; headlessStartReason?: string; gpuRate?: number; balanceCents?: number; onAction: (id: string, operation: 'start' | 'stop' | 'delete', mode?: InstanceMode) => Promise<void>; onPlanChange: (id: string, target: 1 | 4) => Promise<boolean> }) {
  const [showPassword, setShowPassword] = useState(false);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [planOpen, setPlanOpen] = useState(false);
  const count = instance.gpuCount ?? 1;
  const [targetCount, setTargetCount] = useState<1 | 4 | 8>(count);
  const busy = actionId !== null;
  const running = instance.state === 'running';
  const stopped = instance.state === 'stopped' || instance.state === 'error';
  const hours = instance.releaseAt && instance.observedAt ? Math.max(0, (Date.parse(instance.releaseAt) - Date.parse(instance.observedAt)) / 3600000) : null;
  const ssh = instance.publicHost && instance.publicPort ? `ssh -p ${instance.publicPort} ${instance.username || 'gpu'}@${instance.publicHost}` : '';
  const connectionId = `customer-connection-${instance.id}`;
  return <article className="customer-instance" aria-labelledby={`customer-instance-${instance.id}`}>
    <div className="customer-instance-heading">
      <div className="customer-instance-identity"><span className="customer-instance-symbol">{instance.mode === 'headless' ? <Terminal size={20} /> : <Cpu size={20} />}</span><div><h2 id={`customer-instance-${instance.id}`}>{instance.name}</h2><p>#{instance.id}{instance.owner ? ` · ${instance.owner}` : ''} · {instance.mode === 'headless' ? '无头模式' : `${count} 卡 Gaudi2`}</p></div></div>
      <span className={stateClass(instance.state)}><i />{stateLabel(instance.state)}</span>
      <div className="customer-instance-actions">
        {running && !instance.owner && <button className="rental-small-button customer-action-primary" aria-expanded={connectionOpen} aria-controls={connectionId} onClick={() => { setConnectionOpen(!connectionOpen); setShowPassword(false); }}><Terminal size={15} />{connectionOpen ? '收起连接' : 'SSH 连接'}</button>}
        {stopped && <><button className="rental-small-button customer-action-primary" disabled={busy || !!gpuStartReason} title={gpuStartReason || `${16 * count} vCPU / ${count === 8 ? 480 : 62.5 * count} GB / ${count} × Gaudi2${gpuRate === undefined ? '' : ` · ${formatMoney(gpuRate * count)}/h`}`} onClick={() => void onAction(instance.id, 'start', 'gpu')}><Power size={14} />{count} 卡开机</button><button className="rental-small-button" disabled={busy || !!headlessStartReason} title={headlessStartReason || '2 vCPU / 4 GB / 无显卡 · ¥0.08/h'} onClick={() => void onAction(instance.id, 'start', 'headless')}><Terminal size={14} />无头开机</button></>}
        {instance.state === 'stopped' && !instance.owner && <button className="rental-small-button" disabled={busy || planChangeBlocked} aria-expanded={planOpen} aria-controls={`customer-plan-${instance.id}`} onClick={() => { setTargetCount(count); setPlanOpen(!planOpen); }}>{planOpen ? '收起配置' : '更改配置'}</button>}
        {['running', 'creating', 'starting', 'repair_required'].includes(instance.state) && <button className="rental-small-button" disabled={busy} onClick={() => void onAction(instance.id, 'stop')}><Power size={14} />{running ? '关机' : instance.state === 'repair_required' ? '尝试回收' : '取消开机'}</button>}
        {actionId === instance.id && <output className="customer-working">正在处理…</output>}
      </div>
    </div>
    <dl className="customer-instance-specs">
      <div><dt>所属节点</dt><dd>{instance.nodeId || 'G2-002'}{instance.nodeOnline === false && <small className="rental-text-error">暂离线</small>}</dd></div>
      <div><dt>计算配置</dt><dd>{instance.vcpu} vCPU · {instance.memoryGB} GB</dd></div>
      <div><dt>存储配置</dt><dd>{instance.systemDiskGiB} GiB 系统盘{instance.dataDiskGiB ? <small>+ {instance.dataDiskGiB} GiB 数据盘{instance.giftDataDiskGiB ? `（赠送 ${instance.giftDataDiskGiB} GiB）` : ''}</small> : <small>无数据盘</small>}</dd></div>
      <div><dt>{stopped ? 'GPU 套餐费率' : '本次算力费率'}</dt><dd>{formatMoney(stopped ? (gpuRate ?? 0) * count : instance.rateCentsPerHour ?? 0)}<small> / 小时</small></dd></div>
    </dl>
    {instance.state === 'stopped' && planOpen && !instance.owner && <section className="customer-plan-change" id={`customer-plan-${instance.id}`} aria-label={`实例 #${instance.id} 更改 GPU 配置`}>
      <div className="customer-plan-heading"><h3>更改 GPU 配置</h3><p>仅关机并释放显卡后可更改；系统盘、数据盘和所属节点保持不变。下次开机重新检查资源。</p></div>
      <fieldset disabled={busy || planChangeBlocked}><legend className="customer-sr-only">选择 GPU 套餐</legend><div className="customer-plan-options">{([1, 4] as const).map(option => <label key={option} className={targetCount === option ? 'is-selected' : ''}><input type="radio" name={`gpu-plan-${instance.id}`} checked={targetCount === option} onChange={() => setTargetCount(option)} /><strong>{option} 卡 Gaudi2</strong><span>{16 * option} vCPU · {62.5 * option} GB · {formatMoney((gpuRate ?? 0) * option)}/小时</span></label>)}</div></fieldset>
      <p className="customer-plan-note">{instance.eightCardGiftDisk ? '原八卡赠送的 600 GiB 数据盘原容量保留，继续免费。' : '已有实例不能升级到 8 卡；如需八卡，请另行创建实例。'}</p>
      <div className="customer-plan-footer"><span>{count === 8 ? '8 卡只能降级，降级后不能再改回 8 卡。' : `${count} 卡当前配置`}</span><button className="rental-small-button customer-action-primary" disabled={busy || planChangeBlocked || targetCount === count || targetCount === 8} onClick={() => { if (targetCount !== 8) void onPlanChange(instance.id, targetCount).then(ok => { if (ok) setPlanOpen(false); }); }}>确认更改为 {targetCount} 卡</button></div>
    </section>}
    {instance.message && <p className={`customer-instance-message ${instance.state === 'error' || instance.state === 'repair_required' ? 'is-error' : ''}`}>{instance.message}</p>}
    {stopped && count === 8 && balanceCents !== undefined && balanceCents <= 0 && <p className="customer-instance-message">可以开机，但余额为零时会按现有计费规则自动关机；请先充值以持续运行。</p>}
    {stopped && (gpuStartReason || headlessStartReason) && <div className="customer-start-reasons">{gpuStartReason && <p>GPU：{gpuStartReason}</p>}{headlessStartReason && <p>无头：{headlessStartReason}</p>}</div>}
    {hours !== null && Number.isFinite(hours) && <div className={`customer-expiry ${hours < 6 ? 'is-urgent' : ''}`}><Clock3 size={15} /><span><strong>{hours > 0 ? `约 ${hours.toFixed(1)} 小时后自动释放` : '已到释放时间，等待清理'}</strong><span>{formatDate(instance.releaseAt ?? null)} · 请提前备份，开机后取消倒计时</span></span></div>}
    {running && connectionOpen && !instance.owner && <section className="customer-connection" id={connectionId} aria-label={`实例 #${instance.id} SSH 连接`}>
      <div className="customer-connection-heading"><h3><Terminal size={16} />SSH 连接</h3><span>{instance.connectivity === 'ready' ? '宿主 SSH 转发已就绪' : 'SSH 入口正在检查，请稍后重试'}</span></div>
      <div className="customer-connection-row"><span>连接命令</span><code>{ssh || '公网映射准备中'}</code>{ssh && <CopyButton value={ssh} label="复制命令" />}</div>
      <div className="customer-connection-row"><span>登录密码</span><code>{showPassword ? instance.password || '准备中' : '••••••••••••'}</code><div className="customer-password-actions"><button className="rental-small-button" aria-label={showPassword ? '隐藏密码' : '显示密码'} aria-pressed={showPassword} onClick={() => setShowPassword(!showPassword)}>{showPassword ? <EyeOff size={14} /> : <Eye size={14} />}</button>{instance.password && <CopyButton value={instance.password} label="复制密码" />}</div></div>
      <p>公网连接仍受隧道和网络影响。长任务建议使用 tmux / screen；SSH 断开后计算可继续，关闭实例则会中断任务。</p>
    </section>}
    <details className="customer-instance-details"><summary>费用与管理</summary><div>
      <dl><div><dt>累计算力</dt><dd>{formatMoney(instance.computeCents ?? 0)}</dd></div><div><dt>累计存储</dt><dd>{formatMoney(instance.storageCents ?? 0)}</dd></div><div><dt>数据盘费用</dt><dd>¥{(instance.storageCnyPerDay ?? 0).toFixed(3)} / 天{(instance.billableDataDiskGiB ?? instance.dataDiskGiB) > 0 ? <small>关机仍计费</small> : instance.giftDataDiskGiB ? <small>赠送部分不收费</small> : null}</dd></div><div><dt>创建时间</dt><dd>{formatDate(instance.createdAt)}</dd></div></dl>
      {instance.mode !== 'headless' && <p>{instance.slots?.length ? `GPU 分配：${instance.slots.join(' / ')}` : instance.slot ? `GPU 分配：${instance.slot}` : '尚未分配 GPU'}</p>}
      {running && instance.billableAt && <p>本次计费开始于 {formatDate(instance.billableAt)} · 关机后重开按届时价格</p>}
      {stopped && <p>关机后可切换 GPU / 无头模式，磁盘与环境保留。无头模式 ¥0.08/h。</p>}
      {(stopped || instance.state === 'repair_required') && <div className="customer-danger-row"><span>释放会永久删除全部磁盘，请先备份。</span><button className="rental-small-button rental-small-danger" disabled={busy} onClick={() => void onAction(instance.id, 'delete')}>永久释放</button></div>}
    </div></details>
  </article>;
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState('');
  useEffect(() => { if (!copied) return; const timer = setTimeout(() => setCopied(''), 2500); return () => clearTimeout(timer); }, [copied]);
  const copy = async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(value);
      else {
        // The explicitly approved HTTP test platform needs a non-secure fallback.
        const input = document.createElement('textarea'); input.value = value;
        input.style.position = 'fixed'; input.style.opacity = '0'; document.body.appendChild(input);
        // eslint-disable-next-line typescript/no-deprecated -- required fallback for the user-approved HTTP test platform
        try { input.select(); if (!document.execCommand('copy')) throw new Error('copy unavailable'); }
        finally { input.remove(); }
      }
      setCopied('已复制');
    } catch { setCopied('请手动复制'); }
  };
  return <><button className="rental-small-button" onClick={() => void copy()} aria-label={label}><Copy size={13} />{copied || label}</button><output className="customer-sr-only">{copied}</output></>;
}

function ConfirmDialog({ title, detail, danger, confirmLabel, requiredText, onCancel, onConfirm }: { title: string; detail: string; danger?: boolean; confirmLabel?: string; requiredText?: string; onCancel: () => void; onConfirm: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [typed, setTyped] = useState('');
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog className="rental-dialog" ref={ref} onCancel={onCancel} aria-labelledby="rental-confirm-title"><h2 id="rental-confirm-title">{title}</h2><p>{detail}</p>{requiredText && <label className="rental-field rental-confirm-input"><span>输入账户名 {requiredText} 确认</span><input aria-label="删除确认账户名" autoComplete="off" spellCheck={false} value={typed} onChange={(event) => setTyped(event.target.value)} /></label>}<div><button className="rental-small-button" autoFocus onClick={onCancel}>取消</button><button className={`rental-small-button ${danger ? 'rental-small-danger' : ''}`} disabled={requiredText !== undefined && typed !== requiredText} onClick={onConfirm}>{confirmLabel ?? (danger ? '确认永久释放' : '确认操作')}</button></div></dialog>;
}

type LedgerEntry = { id: number; owner: string; instance_id: number | null; cents: number; reason: string; created_at: string; actor: string | null };
function LedgerPanel({ admin = false, ownerFilter = '', externalRevision = 0, active = true }: { admin?: boolean; ownerFilter?: string; externalRevision?: number; active?: boolean }) {
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [scopeVerified, setScopeVerified] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('all');
  const [page, setPage] = useState(1);
  useEffect(() => {
    if (!active) return;
    let disposed = false, fetching = false;
    const load = async () => {
      if (fetching || document.hidden) return;
      fetching = true;
      try { const result = await rentalRequest<{ entries: LedgerEntry[]; scopeOwner?: string }>(admin ? `/api/admin/ledger${ownerFilter ? `?owner=${encodeURIComponent(ownerFilter)}` : ''}` : '/api/rental/ledger'); if (!disposed) { setEntries(result.entries); setScopeVerified(!ownerFilter || result.scopeOwner === ownerFilter); setError(''); } }
      catch (e) { if (!disposed) setError(e instanceof Error ? e.message : '账单加载失败'); }
      finally { fetching = false; if (!disposed) setLoading(false); }
    };
    void load(); const timer = setInterval(() => void load(), 30000);
    return () => { disposed = true; clearInterval(timer); };
  }, [admin, ownerFilter, revision, externalRevision, active]);
  const reason = (text: string) => text === 'usage' ? '算力费用' : text.startsWith('storage:') ? '数据盘费用' : text === 'registration_bonus' ? '注册赠金' : text.startsWith('redeem_cash:') ? `充值码入账 #${text.split(':')[1]}` : text.startsWith('redeem_gift:') ? `赠送码入账 #${text.split(':')[1]}` : text.startsWith('recharge') ? '管理员充值' : text;
  const scoped = entries.filter(row => !ownerFilter || row.owner === ownerFilter);
  const visible = scoped.filter(row => `${row.instance_id ?? ''} ${admin ? row.owner : ''}`.toLowerCase().includes(query.trim().toLowerCase()) && (kind === 'all' || kind === 'compute' && row.reason === 'usage' || kind === 'storage' && row.reason.startsWith('storage:') || kind === 'recharge' && (row.reason.startsWith('recharge') || row.reason.startsWith('redeem_cash:')) || kind === 'gift' && (row.reason === 'registration_bonus' || row.reason.startsWith('redeem_gift:'))));
  const pages = Math.max(1, Math.ceil(visible.length / 20)), current = Math.min(page, pages);
  const displayed = visible.slice((current - 1) * 20, current * 20);
  return <section className="rental-card rental-instances-card"><div className="rental-card-head"><div><h2>{admin ? '账户流水' : '费用明细'}</h2></div><button className="rental-small-button" onClick={() => setRevision(revision + 1)}><RefreshCw size={14} />刷新</button></div>
    <div className="rental-billing-info">{admin ? '金额为客户余额增减，不等同于平台现金收支，赠金不计实收。' : '算力开机成功后计费，关机停止；数据盘保留期间计费。'} 算力与存储按 UTC 日汇总，充值和赠金逐笔展示；仅最近 200 组记录。</div>
    {admin && ownerFilter && !loading && !scopeVerified && <div className="admin-notice" role="alert">当前服务尚未启用按客户独立查询。以下仅为平台最近 200 组中的匹配记录，可能不完整；请勿据此认定该客户没有历史账单。</div>}
    <div className="rental-list-tools"><label><Search size={15} /><input aria-label="筛选账单" value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} placeholder={admin ? '搜索账户或实例 ID' : '搜索实例 ID'} /></label>{<><select aria-label="账单项目" value={kind} onChange={e => { setKind(e.target.value); setPage(1); }}><option value="all">全部项目</option><option value="recharge">充值</option><option value="gift">赠金</option><option value="compute">算力费用</option><option value="storage">数据盘费用</option></select><button className="admin-link" onClick={() => { setQuery(''); setKind('all'); setPage(1); }}>重置</button></>}</div>
    {error && <p className="rental-inline-error" role="alert">{error}</p>}
    <div className="rental-table-wrap"><table className="rental-table"><thead><tr><th>时间</th>{admin && <th>账户</th>}<th>项目</th><th>实例</th><th>金额</th></tr></thead><tbody>{displayed.map((row) => <tr key={row.id}><td>{formatDate(row.created_at)}</td>{admin && <td>{row.owner}</td>}<td>{reason(row.reason)}{admin && row.reason.startsWith('recharge:') && <small className="admin-subtext">{row.reason.slice('recharge:'.length)}</small>}</td><td>{row.instance_id ? `#${row.instance_id}` : '—'}</td><td className={row.cents > 0 ? 'rental-text-credit' : ''}>{row.cents > 0 ? '+' : '−'}{formatMoney(Math.abs(row.cents))}</td></tr>)}</tbody></table></div>
    {visible.length === 0 && <div className="rental-empty">{loading ? '正在读取账单…' : '当前范围内没有匹配记录'}</div>}
    {<AdminPagination page={current} pages={pages} total={visible.length} fullTotal={scoped.length} onPage={setPage} />}
  </section>;
}
