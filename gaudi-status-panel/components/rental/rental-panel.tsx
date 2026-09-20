'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { BrandLogo } from '@/components/brand-logo';
import {
  ArrowRight,
  CheckCircle2,
  CircleHelp,
  Cpu,
  Database,
  HardDrive,
  LoaderCircle,
  MemoryStick,
  MessageCircle,
  Plus,
  Power,
  RefreshCw,
  Server,
  ShieldCheck,
  Sparkles,
  Terminal,
  UserRound,
  Wallet,
  XCircle,
  Copy, Eye, EyeOff, Clock3, ReceiptText, LayoutDashboard, Search,
} from 'lucide-react';

type InstanceState = 'creating' | 'starting' | 'running' | 'stopping' | 'stopped' | 'error' | 'repair_required';
type SlotState = 'available' | 'creating' | 'running' | 'stopping' | 'stopped' | 'error' | 'repair_required';
type InstanceMode = 'gpu' | 'headless';

type RentalInstance = {
  id: string;
  name: string;
  slot: number;
  mode?: InstanceMode;
  state: InstanceState;
  vcpu: number;
  memoryGB: number;
  systemDiskGiB: number;
  dataDiskGiB: number;
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
  observedAt?: string;
};

type RentalAccount = {
  name: string;
  role: 'customer' | 'admin';
  balanceCents: number;
  createdAt: string;
  lastActivityAt: string | null;
};

type RentalState = {
  service: 'ready' | 'degraded' | 'blocked' | 'maintenance';
  serviceMessage: string;
  account: RentalAccount;
  slots: Array<{
    slot: number;
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
    includedCpu?: number;
    includedMemoryGB?: number;
    freeDataDiskGiB?: number;
    extraDataDiskCnyPerGiBDay?: number;
  };
  updatedAt: string;
  storage?: { totalGiB: number; usedGiB: number; freeGiB: number; safetyGiB: number; reservedGiB: number; budgetGiB: number; lowSpace: boolean; mode: string };
};

type AdminCustomer = { name: string; balanceCents: number; createdAt: string; deletedAt: string | null; deletedBy: string | null; instanceCount: number };

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
  const headers = new Headers(init?.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, {
    ...init,
    headers,
    cache: 'no-store',
    signal: init?.signal ?? AbortSignal.timeout(15000),
  });
  const body = (await response.json().catch(() => ({}))) as { error?: string; message?: string } & T;
  if (!response.ok) throw new Error(`${response.status}: ${body.message || body.error || '请求失败'}`);
  return body as T;
}

function requestKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, '0')).join('');
}

export function RentalPanel() {
  const [account, setAccount] = useState<RentalAccount | null | undefined>(undefined);
  const [state, setState] = useState<RentalState>(initialState);
  const [dataDiskGiB, setDataDiskGiB] = useState(0);
  const [mode, setMode] = useState<InstanceMode>('gpu');
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
  const [filter, setFilter] = useState('all');
  const [walletRevision, setWalletRevision] = useState(0);
  const [customerRevision, setCustomerRevision] = useState(0);
  const [confirmation, setConfirmation] = useState<{ id: string; operation: 'stop' | 'delete'; name: string } | null>(null);

  useEffect(() => {
    void rentalRequest<{ account: RentalAccount }>('/api/auth/me')
      .then((result) => { setAccount(result.account); if (result.account.role === 'admin' && new URLSearchParams(location.search).get('view') === 'admin') setTab('admin'); })
      .catch(() => setAccount(null));
  }, []);

  const accountName = account?.name;
  const refresh = useCallback(async () => {
    if (!accountName || refreshing.current) return;
    refreshing.current = true;
    const epoch = authEpoch.current;
    try {
      const next = await rentalRequest<RentalState>('/api/rental/state');
      if (epoch !== authEpoch.current) return;
      setState(next);
      setAccount(next.account);
    } catch (error) {
      if (epoch !== authEpoch.current) return;
      if (error instanceof Error && /登录|unauthorized|401/i.test(error.message)) setAccount(null);
      else setNotice({ tone: 'error', text: error instanceof Error ? error.message : '资源调度器暂时不可用' });
    } finally {
      refreshing.current = false;
      setLoading(false);
    }
  }, [accountName]);

  useEffect(() => {
    if (!accountName) return;
    const initial = window.setTimeout(() => { void refresh(); }, 0);
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 5000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [accountName, refresh]);

  const submitAuth = async () => {
    setAuthSubmitting(true);
    setNotice(null);
    try {
      const result = await rentalRequest<{ account: RentalAccount }>(`/api/auth/${authMode}`, {
        method: 'POST',
        body: JSON.stringify({ name: authName.trim(), password: authPassword }),
      });
      authEpoch.current += 1;
      setAccount(result.account);
      setAuthPassword('');
      setNotice({ tone: 'success', text: authMode === 'register' ? '账户已创建并登录。' : '登录成功。' });
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '认证失败' });
    } finally {
      setAuthSubmitting(false);
    }
  };

  if (account === undefined) return <main className="rental-app"><div className="rental-auth-loading">正在连接账户服务…</div></main>;
  if (account === null) return <AuthGate mode={authMode} setMode={setAuthMode} name={authName} setName={setAuthName} password={authPassword} setPassword={setAuthPassword} submitting={authSubmitting} onSubmit={() => void submitAuth()} notice={notice} />;

  const availableSlots = state.slots.filter((slot) => slot.state === 'available').length;
  const busySlots = state.slots.length - availableSlots;
  const canOrder = state.service === 'ready' && state.image.ready && !submitting;
  const activeInstance = state.instances.find((row) => row.mode !== 'headless' && ['creating', 'starting', 'running', 'stopping', 'repair_required'].includes(row.state));
  const hourlyCost = state.instances.reduce((sum, row) => sum + (['running', 'stopping', 'repair_required'].includes(row.state) ? row.rateCentsPerHour ?? 0 : 0) + (row.storageCnyPerDay ?? 0) * 100 / 24, 0);
  const startReason = (instance: RentalInstance, selected: InstanceMode) => state.service !== 'ready' ? state.serviceMessage : state.account.balanceCents <= 0 ? '余额不足，请先充值' : selected === 'headless' ? (state.headless?.available ?? 0) <= 0 ? '无头资源不足，暂时无法开机' : '' : activeInstance && activeInstance.id !== instance.id ? '请先关闭当前 GPU 实例' : availableSlots === 0 && !instance.slot ? 'GPU 资源不足，暂时无法开机' : '';
  const estimatedHours = hourlyCost > 0 ? state.account.balanceCents / hourlyCost : null;
  const visibleInstances = state.instances.filter((row) => (filter === 'all' || row.state === filter) && `${row.name} ${row.id}`.toLowerCase().includes(search.toLowerCase()));

  const updateNumber = (value: string, setter: (value: number) => void, min: number, max: number, step: number) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return;
    setter(Math.min(max, Math.max(min, Math.round(parsed / step) * step)));
  };

  const order = async () => {
    setSubmitting(true);
    setNotice(null);
    try {
      const intent = JSON.stringify({ name: instanceName.trim(), dataDiskGiB, mode });
      if (orderIntent.current !== intent) { orderKey.current = null; orderIntent.current = intent; }
      orderKey.current ??= requestKey();
      await rentalRequest('/api/rental/order', {
        method: 'POST',
        headers: { 'X-Idempotency-Key': orderKey.current },
        body: JSON.stringify({ name: instanceName.trim() || 'gaudi-dev', mode, vcpu: mode === 'headless' ? 2 : 16, memoryGB: mode === 'headless' ? 4 : 62.5, dataDiskGiB, image: state.image.id }),
      });
      orderKey.current = null;
      setTab('instances');
      setNotice({ tone: 'success', text: `实例 ${instanceName.trim() || 'gaudi-dev'} 已创建，请在实例列表中点击开机。` });
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

  const logout = async () => {
    try {
      await rentalRequest('/api/auth/logout', { method: 'POST' });
      authEpoch.current += 1;
      orderKey.current = null;
      setAccount(null);
      setState(initialState);
      setTab('instances');
      setNotice(null);
    } catch (error) {
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : '退出失败' });
    }
  };

  return (
    <main className="rental-app">
      <div className="rental-shell">
        <header className="rental-header">
          <RentalBrand />
          <div className="rental-header-right"><span className={state.service === 'ready' ? 'rental-live-dot' : 'rental-warn-dot'} />资源池 {availableSlots}/{state.slots.length} 可用 · {state.account.name} · 余额 {formatMoney(state.account.balanceCents)} <button type="button" className="rental-icon-button" onClick={() => void refresh()} aria-label="刷新资源池"><RefreshCw size={16} /></button><button type="button" className="rental-small-button" onClick={() => void logout()}>退出登录</button></div>
        </header>

        <section className="rental-hero">
          <div><div className="rental-eyebrow">YOUR COMPUTE, READY WHEN YOU ARE</div><h1>算力工作台<span className="rental-beta">G2-002</span></h1><p>独占 Gaudi2 · 16 核 CPU · 62.5 GB 内存 · 50 GiB 系统盘</p></div>
          <div className="rental-hero-badge"><ShieldCheck size={18} /><span>GPU 计算 · 无头配置环境<br /><small>GPU 每人同时 1 台 · 无头可同时多台</small></span></div>
        </section>

        <nav className="rental-nav" aria-label="工作台导航">
          <button aria-current={tab === 'instances' ? 'page' : undefined} onClick={() => setTab('instances')}><LayoutDashboard size={16} />我的实例 <b>{state.instances.length}</b></button>
          <button aria-current={tab === 'create' ? 'page' : undefined} onClick={() => setTab('create')}><Plus size={17} />创建实例</button>
          <button aria-current={tab === 'wallet' ? 'page' : undefined} onClick={() => setTab('wallet')}><ReceiptText size={16} />充值与账单</button>
          {account.role === 'admin' && <button aria-current={tab === 'admin' ? 'page' : undefined} onClick={() => setTab('admin')}><ShieldCheck size={16} />管理中心</button>}
        </nav>
        {notice && <output className={`rental-notice rental-notice-${notice.tone}`}>{notice.tone === 'success' ? <CheckCircle2 size={18} /> : notice.tone === 'error' ? <XCircle size={18} /> : <CircleHelp size={18} />}<span>{notice.text}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示">×</button></output>}
        {state.service !== 'ready' && !loading && <div className="rental-notice rental-notice-info"><CircleHelp size={17} />{state.serviceMessage}</div>}
        {estimatedHours !== null && estimatedHours < 2 && <div className="rental-notice rental-notice-error"><Wallet size={17} />按当前用量，余额预计不足 2 小时。余额耗尽后自动关机。<button className="rental-small-button" onClick={() => setTab('wallet')}>兑换充值码</button></div>}
        {account.role === 'customer' && <RechargeContactBanner />}
        <div className="rental-overview">
          <div><span><Wallet size={16} />账户余额</span><strong>{formatMoney(state.account.balanceCents)}</strong><small>{estimatedHours === null ? '按实际运行和磁盘保留时间计费' : `按当前用量约可用 ${estimatedHours.toFixed(1)} 小时，仅供参考`}</small></div>
          <div><span><Cpu size={16} />GPU 资源</span><strong>{availableSlots}<em> / 8 可用</em></strong><small>{activeInstance ? `你的实例 #${activeInstance.id} 正在占用资源` : '创建不占卡，开机时自动分配'}</small></div>
          <div><span><Clock3 size={16} />保留规则</span><strong>48<em> 小时</em></strong><small>关机后保留两天，届时自动删除全部磁盘</small></div>
        </div>

        {tab === 'instances' && <section className="rental-card rental-instances-card">
          <div className="rental-card-head"><div><div className="rental-kicker">WORKSPACE</div><h2>我的实例</h2></div><button className="rental-small-button" onClick={() => setTab('create')}><Plus size={15} />创建实例</button></div>
          <div className="rental-list-tools"><label><Search size={15} /><input aria-label="搜索实例" placeholder="搜索名称或实例 ID" value={search} onChange={(event) => setSearch(event.target.value)} /></label><select aria-label="筛选实例状态" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">全部状态</option><option value="running">运行中</option><option value="stopped">已关机</option><option value="error">启动失败</option></select></div>
          {loading ? <div className="rental-empty"><LoaderCircle className="rental-spin" size={22} />读取实例状态…</div> : visibleInstances.length === 0 ? <div className="rental-empty"><Server size={24} /><span>{state.instances.length ? '没有匹配的实例' : '还没有实例，创建你的第一个算力环境。'}</span></div> : <div className="rental-instance-list">{visibleInstances.map((instance) => <InstanceRow key={instance.id} instance={instance} actionId={actionId} onAction={action} gpuStartReason={startReason(instance, 'gpu')} headlessStartReason={startReason(instance, 'headless')} gpuRate={state.billing.rateCentsPerHour ?? 0} />)}</div>}
        </section>}
        {tab === 'wallet' && <>{account.role === 'customer' && <RedeemCodePanel onRedeemed={() => { setWalletRevision((value) => value + 1); void refresh(); }} />}<LedgerPanel key={walletRevision} /></>}
        {tab === 'admin' && account.role === 'admin' && <AdminRegistrationBonus />}
        {tab === 'admin' && account.role === 'admin' && <AdminAccountPanel onChanged={() => setCustomerRevision((value) => value + 1)} />}
        {account.role === 'admin' && <div hidden={tab !== 'admin'}><AdminCodePanel active={tab === 'admin'} customerRevision={customerRevision} /></div>}
        {tab === 'admin' && account.role === 'admin' && <><AdminPanel currentRateCents={state.billing.rateCentsPerHour ?? 0} customerRevision={customerRevision} onNotice={setNotice} /><AdminFleet storage={state.storage} /><LedgerPanel admin /></>}

        {tab === 'create' && <div className="rental-grid">
          <section className="rental-card rental-order-card">
            <div className="rental-card-head"><div><div className="rental-kicker">01 / LAUNCH</div><h2>创建实例</h2></div><span className="rental-step-chip">一步下单</span></div>
            <div className="rental-form">
              <fieldset className="rental-mode-picker"><legend>启动模式</legend><div>
                <button type="button" aria-pressed={mode === 'gpu'} onClick={() => setMode('gpu')}><Cpu size={19} /><strong>GPU 模式</strong><span>16 vCPU · 62.5 GB · Gaudi2</span><b>{formatMoney(state.billing.rateCentsPerHour ?? 0)} / 小时</b></button>
                <button type="button" aria-pressed={mode === 'headless'} onClick={() => setMode('headless')}><Terminal size={19} /><strong>无头模式</strong><span>2 vCPU · 4 GB · 无显卡</span><b>¥0.08 / 小时</b></button>
              </div><small>无头适合安装依赖、下载模型和配置环境，可同时开多台。关机后切换模式，磁盘与环境保留。</small></fieldset>
              <label className="rental-field"><span>实例名称</span><input value={instanceName} maxLength={32} onChange={(event) => setInstanceName(event.target.value)} placeholder="例如 gaudi-dev" /></label>
              <div className="rental-field"><span>镜像</span><div className={`rental-image-option ${state.image.ready ? 'rental-image-ready' : 'rental-image-pending'}`}><div className="rental-image-icon"><Sparkles size={17} /></div><div><strong>{state.image.name}</strong><small>{state.image.version} · {state.image.detail}</small></div><CheckCircle2 size={17} /></div></div>

              <div className="rental-resource-title"><span>固定计算规格</span><small>{mode === 'headless' ? '不占 GPU · 按剩余 CPU 和内存分配' : '每张 Gaudi2 对应一台独占虚拟机'}</small></div>
              <div className="rental-control-grid">
                <div className="rental-disk-fixed"><Cpu size={15} /><div><span>vCPU</span><strong>{mode === 'headless' ? 2 : 16} 核</strong></div><small>固定</small></div>
                <div className="rental-disk-fixed"><MemoryStick size={15} /><div><span>内存</span><strong>{mode === 'headless' ? 4 : 62.5} GB</strong></div><small>固定</small></div>
              </div>
              <div className="rental-disk-row"><div className="rental-disk-fixed"><HardDrive size={15} /><div><span>系统盘</span><strong>50 GiB</strong></div><small>固定</small></div><label className="rental-control rental-data-disk"><span><Database size={15} />数据盘</span><div className="rental-control-value"><input type="number" min={0} max={200} step={1} value={dataDiskGiB} onChange={(event) => updateNumber(event.target.value, setDataDiskGiB, 0, 200, 1)} /><b>GiB</b></div><input type="range" min={0} max={200} step={1} value={dataDiskGiB} onChange={(event) => setDataDiskGiB(Number(event.target.value))} /></label></div>

              <div className="rental-order-summary"><div><span>算力 · 开机成功后计费</span><strong>{formatMoney(mode === 'headless' ? 8 : state.billing.rateCentsPerHour ?? 0)} / 小时</strong><small>已含 {mode === 'headless' ? '2 核 / 4 GB / 无 GPU' : '16 核 / 62.5 GB / Gaudi2'} / 50 GiB 系统盘</small></div><div><span>数据盘 · 创建后持续计费</span><strong>¥{(dataDiskGiB * (state.billing.extraDataDiskCnyPerGiBDay ?? 0)).toFixed(3)} / 天</strong><small>{dataDiskGiB === 0 ? '不附加数据盘，无额外存储费' : `${dataDiskGiB} GiB · 挂载到 /data · 关机仍计费`}</small></div></div>
              <button className="rental-primary-button" type="button" disabled={!canOrder} onClick={() => void order()}>{submitting ? <><LoaderCircle size={18} className="rental-spin" /> 正在创建</> : !state.image.ready ? <>镜像尚未就绪</> : <>创建实例 <ArrowRight size={18} /></>}</button>
              <p className="rental-form-footnote"><ShieldCheck size={14} />先创建，再开机。开机成功后才收算力费；数据盘从创建起收费。</p>
            </div>
          </section>

          <section className="rental-card rental-pool-card">
            <div className="rental-card-head"><div><div className="rental-kicker">02 / RESOURCE POOL</div><h2>GPU 资源池</h2></div><span className="rental-pool-count"><b>{busySlots}</b> / {state.slots.length} 使用中</span></div>
            <div className="rental-slot-grid">{state.slots.map((slot) => <div className={`rental-slot ${slot.state === 'available' ? 'rental-slot-free' : 'rental-slot-busy'}`} key={slot.slot}><div className="rental-slot-top"><span className="rental-slot-number">GPU {String(slot.slot).padStart(2, '0')}</span><span className={stateClass(slot.state)}><i />{stateLabel(slot.state)}</span></div><div className="rental-slot-gpu"><Server size={18} /><span>{slot.gpu}</span></div>{slot.instanceId && <small>实例 {slot.instanceId}</small>}</div>)}</div>
            <div className="rental-pool-foot"><div><i className="rental-legend-free" />可分配</div><div><i className="rental-legend-busy" />已占用或处理中</div><span>最多同时运行 8 台 GPU 实例</span></div>
            <div className="rental-headless-capacity"><Terminal size={19} /><div><strong>无头模式 · ¥0.08/h</strong><span>当前可启动 {state.headless?.available ?? 0} 台 · 2 vCPU / 4 GB · 同客户可多开</span></div></div>
          </section>
        </div>}

        <footer className="rental-footer"><span><ShieldCheck size={14} />1CatTunnel 公网连接</span><span>5 秒自动同步 · 切到后台暂停刷新</span><span>最近同步 {formatDate(state.updatedAt)}</span></footer>
        {confirmation && <ConfirmDialog title={confirmation.operation === 'delete' ? '永久释放实例？' : '关闭实例？'} detail={confirmation.operation === 'delete' ? `将删除 ${confirmation.name} (#${confirmation.id}) 的系统盘和数据盘，无法恢复。请先备份重要数据。` : `${confirmation.name} (#${confirmation.id}) 中的任务将中断。关机后停止算力计费，数据盘继续计费；48 小时未开机将自动删除。系统无响应时会在 2 分钟后强制关机。`} danger={confirmation.operation === 'delete'} onCancel={() => setConfirmation(null)} onConfirm={() => void action(confirmation.id, confirmation.operation, undefined, true)} />}
      </div>
    </main>
  );
}

function RentalBrand() {
  return <div className="rental-brand"><BrandLogo /><div className="rental-brand-detail"><strong>1CatDL</strong><span>GAUDI GPU CLOUD / G2-002</span></div></div>;
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
    <div className="rental-recharge-contact-lead"><span className="rental-recharge-contact-icon"><MessageCircle size={19} /></span><div><small>TOP UP SUPPORT</small><strong>充值请联系管理员</strong><span>确认到账后，管理员会发放充值码，兑换即可入账。</span></div></div>
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
  return <main className="rental-app"><div className="rental-auth-shell"><RentalBrand /><section className="rental-card rental-auth-card"><div className="rental-kicker">SELF-SERVICE ACCESS</div><h1>{mode === 'login' ? '登录 GPU 云实例' : '注册客户账户'}</h1><p>{mode === 'login' ? '登录后管理实例、余额与 SSH 入口。' : registrationDescription}</p><RechargeContactBanner compact />{notice && <div className={`rental-notice rental-notice-${notice.tone}`}>{notice.text}</div>}<label className="rental-field"><span>账户名</span><input value={name} autoComplete="username" onChange={(event) => setName(event.target.value)} /></label><label className="rental-field"><span>密码</span><input type="password" value={password} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} onChange={(event) => setPassword(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') onSubmit(); }} /></label><button className="rental-primary-button" type="button" disabled={submitting || !name.trim() || !password} onClick={onSubmit}>{submitting ? '处理中…' : mode === 'login' ? '登录' : '创建账户'}</button><button className="rental-auth-switch" type="button" onClick={() => setMode(mode === 'login' ? 'register' : 'login')}>{mode === 'login' ? '还没有账户？立即注册' : '已有账户？返回登录'}</button></section></div></main>;
}

function AdminAccountPanel({ onChanged }: { onChanged: () => void }) {
  const [customers, setCustomers] = useState<AdminCustomer[]>([]);
  const [status, setStatus] = useState('active');
  const [query, setQuery] = useState('');
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [confirmation, setConfirmation] = useState<{ customer: AdminCustomer; action: 'delete' | 'restore' } | null>(null);

  useEffect(() => {
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
  }, [status, revision]);

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
  const visible = customers.filter((customer) => customer.name.toLowerCase().includes(query.trim().toLowerCase()));
  return <section className="rental-card rental-account-card" aria-labelledby="account-management-title">
    <div className="rental-card-head"><div><div className="rental-kicker">ADMIN / ACCOUNTS</div><h2 id="account-management-title">客户账户管理</h2></div><button className="rental-small-button" disabled={loading || busy} onClick={() => { setMessage(null); setRevision((value) => value + 1); }}><RefreshCw size={14} />刷新账户</button></div>
    <p className="rental-account-help">删除后立即禁止登录，保留余额、历史账单和账户名，可随时恢复。账户下的全部实例（含已关机实例）须先在下方实例管理中释放；这里不会删除磁盘或自动退款。</p>
    <div className="rental-list-tools"><label><Search size={14} /><input aria-label="搜索客户账户" placeholder="搜索账户名" value={query} onChange={(event) => setQuery(event.target.value)} /></label><select aria-label="账户状态" value={status} disabled={busy} onChange={(event) => { setStatus(event.target.value); setLoading(true); setMessage(null); }}><option value="active">正常账户</option><option value="deleted">已删除</option><option value="all">全部账户</option></select><span className="rental-account-count">{loading ? '加载中…' : `${visible.length} 个账户`}</span></div>
    {message && <div role={message.ok ? 'status' : 'alert'} className={`rental-account-message ${message.ok ? 'rental-code-success' : 'rental-text-error'}`}>{message.text}</div>}
    <div className="rental-table-wrap"><table className="rental-table"><thead><tr><th>账户</th><th>余额</th><th>保留实例</th><th>状态</th><th>注册 / 删除时间</th><th>操作</th></tr></thead><tbody>
      {loading ? <tr><td colSpan={6}>正在读取账户…</td></tr> : visible.length === 0 ? <tr><td colSpan={6}>{query ? '没有匹配的账户' : status === 'deleted' ? '暂无已删除账户' : '暂无客户账户'}</td></tr> : visible.map((customer) => <tr key={customer.name}>
        <td><strong>{customer.name}</strong></td><td>{formatMoney(customer.balanceCents)}</td><td>{customer.instanceCount}</td><td><span className={`rental-account-status ${customer.deletedAt ? 'is-deleted' : ''}`}>{customer.deletedAt ? '已删除' : '正常'}</span></td><td>{formatDate(customer.createdAt)}{customer.deletedAt && <small className="rental-account-meta">删除于 {formatDate(customer.deletedAt)} · {customer.deletedBy}</small>}</td>
        <td>{customer.deletedAt ? <button className="rental-small-button" disabled={busy} aria-label={`恢复账户 ${customer.name}`} onClick={() => setConfirmation({ customer, action: 'restore' })}>恢复账户</button> : <><button className="rental-small-button rental-small-danger" disabled={busy || customer.instanceCount > 0} aria-label={`删除账户 ${customer.name}`} onClick={() => setConfirmation({ customer, action: 'delete' })}>删除账户</button>{customer.instanceCount > 0 && <small className="rental-account-meta">请先释放全部实例</small>}</>}</td>
      </tr>)}
    </tbody></table></div>
    {customers.length >= 500 && <p className="rental-account-help">当前显示最近注册的 500 个符合条件的账户。</p>}
    {confirmation && <ConfirmDialog title={confirmation.action === 'delete' ? `删除账户 ${confirmation.customer.name}？` : `恢复账户 ${confirmation.customer.name}？`} detail={confirmation.action === 'delete' ? `该客户会立即退出登录，之后不能登录、下单、充值或兑换充值码。余额 ${formatMoney(confirmation.customer.balanceCents)} 和历史账目保留，不自动退款；账户名不能重新注册。可从“已删除”中恢复。` : `恢复后客户可用原密码重新登录，保留原余额，不重新发放注册赠金，也不会恢复已释放的实例。`} danger={confirmation.action === 'delete'} confirmLabel={confirmation.action === 'delete' ? '确认删除账户' : '确认恢复账户'} requiredText={confirmation.action === 'delete' ? confirmation.customer.name : undefined} onCancel={() => setConfirmation(null)} onConfirm={() => void perform()} />}
  </section>;
}

function AdminPanel({ currentRateCents, customerRevision, onNotice }: { currentRateCents: number; customerRevision: number; onNotice: (notice: { tone: 'info' | 'success' | 'error'; text: string } | null) => void }) {
  const [customers, setCustomers] = useState<AdminCustomer[]>([]);
  const [selected, setSelected] = useState('');
  const [amount, setAmount] = useState('100');
  const [price, setPrice] = useState(currentRateCents ? (currentRateCents / 100).toFixed(2) : '');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const rechargeKey = useRef<{ intent: string; key: string } | null>(null);
  const priceDirty = useRef(false);
  const [confirm, setConfirm] = useState<'recharge' | 'price' | null>(null);

  useEffect(() => {
    const sync = window.setTimeout(() => { if (!priceDirty.current) setPrice(currentRateCents ? (currentRateCents / 100).toFixed(2) : ''); }, 0);
    return () => window.clearTimeout(sync);
  }, [currentRateCents]);

  const load = useCallback(async () => {
    try {
      const result = await rentalRequest<{ customers: AdminCustomer[] }>('/api/admin/customers');
      setCustomers(result.customers);
      setSelected((previous) => result.customers.some((customer) => customer.name === previous) ? previous : result.customers[0]?.name || '');
    } catch (error) {
      onNotice({ tone: 'error', text: error instanceof Error ? error.message : '读取客户列表失败' });
    } finally {
      setLoading(false);
    }
  }, [onNotice]);

  useEffect(() => {
    const initial = window.setTimeout(() => { void load(); }, 0);
    return () => window.clearTimeout(initial);
  }, [load, customerRevision]);

  const recharge = async (confirmed = false) => {
    const yuan = Number(amount);
    const cents = Math.round(yuan * 100);
    if (!selected || !Number.isFinite(yuan) || cents < 1) {
      onNotice({ tone: 'error', text: '请选择客户并填写有效充值金额。' });
      return;
    }
    if (!confirmed) { setConfirm('recharge'); return; }
    setConfirm(null);
    setBusy(true);
    try {
      const intent = `${selected}:${cents}`;
      if (!rechargeKey.current || rechargeKey.current.intent !== intent) {
        rechargeKey.current = { intent, key: requestKey() };
      }
      await rentalRequest('/api/admin/recharge', {
        method: 'POST',
        headers: { 'X-Idempotency-Key': rechargeKey.current.key },
        body: JSON.stringify({ user: selected, cents, note: '管理员面板充值' }),
      });
      rechargeKey.current = null;
      onNotice({ tone: 'success', text: `已为 ${selected} 充值 ¥${yuan.toFixed(2)}。` });
      await load();
    } catch (error) {
      onNotice({ tone: 'error', text: error instanceof Error ? error.message : '充值失败' });
    } finally {
      setBusy(false);
    }
  };

  const savePrice = async (confirmed = false) => {
    const yuan = Number(price);
    const cents = Math.round(yuan * 100);
    if (!Number.isFinite(yuan) || cents < 1) {
      onNotice({ tone: 'error', text: '小时价格必须大于 0。' });
      return;
    }
    if (!confirmed) { setConfirm('price'); return; }
    setConfirm(null);
    setBusy(true);
    try {
      await rentalRequest('/api/admin/price', { method: 'POST', body: JSON.stringify({ cents }) });
      priceDirty.current = false;
      onNotice({ tone: 'success', text: `单卡价格已设置为 ¥${yuan.toFixed(2)}/小时。` });
    } catch (error) {
      onNotice({ tone: 'error', text: error instanceof Error ? error.message : '价格设置失败' });
    } finally {
      setBusy(false);
    }
  };

  return <section className="rental-card rental-admin-card">
    <div className="rental-card-head"><div><div className="rental-kicker">ADMIN / BILLING</div><h2>管理员控制台</h2></div><span className="rental-card-head-hint"><UserRound size={15} />仅管理员可见</span></div>
    <div className="rental-admin-body">
      <div className="rental-admin-controls">
        <label className="rental-field"><span>GPU 新开机价格（元/小时）</span><input type="number" min="0.01" step="0.01" value={price} onChange={(event) => { priceDirty.current = true; setPrice(event.target.value); }} placeholder="例如 4.00" /></label>
        <button type="button" className="rental-small-button" disabled={busy} onClick={() => void savePrice()}>保存价格</button>
        <label className="rental-field"><span>客户账户</span><select value={selected} onChange={(event) => setSelected(event.target.value)} disabled={loading || customers.length === 0}><option value="">暂无客户</option>{customers.map((customer) => <option value={customer.name} key={customer.name}>{customer.name} · {formatMoney(customer.balanceCents)}</option>)}</select></label>
        <label className="rental-field"><span>充值金额（元）</span><input type="number" min="0.01" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} /></label>
        <button type="button" className="rental-small-button rental-admin-recharge" disabled={busy || !selected} onClick={() => void recharge()}>充值</button>
      </div>
      <div className="rental-admin-customer-list">{customers.length === 0 ? <span>暂无客户账户</span> : customers.map((customer) => <div key={customer.name}><span>{customer.name}</span><strong>{formatMoney(customer.balanceCents)}</strong></div>)}</div>
      <p className="rental-billing-info">GPU 改价仅对下次开机生效，运行实例保持本次锁定费率。无头模式固定 ¥0.08/小时。新账户按注册时的赠送设置入账，赠金和充值均可在收支记录中核对。</p>
    </div>
    {confirm && <ConfirmDialog title={confirm === 'recharge' ? '确认给客户充值' : '确认修改卡时价格'} detail={confirm === 'recharge' ? `给 ${selected} 增加余额 ¥${Number(amount).toFixed(2)}，请确认账户和金额无误。` : `新开机价格将从 ${formatMoney(currentRateCents)}/h 改为 ¥${Number(price).toFixed(2)}/h。已运行实例价格不变，改价将记录审计。`} onCancel={() => setConfirm(null)} onConfirm={() => void (confirm === 'recharge' ? recharge(true) : savePrice(true))} />}
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
    <div className="rental-card-head"><div><div className="rental-kicker">REDEEM / BALANCE</div><h2>充值码自助入账</h2></div><Wallet size={22} /></div>
    <div className="rental-code-body"><p className="rental-code-intro">将管理员发给你的充值 SDK（兑换码）粘贴到下方，验证成功后立即入账。</p>
      <form onSubmit={(event) => { event.preventDefault(); void redeem(); }} className="rental-redeem-form"><label className="rental-field"><span>充值码</span><input aria-label="充值码" value={code} maxLength={128} autoComplete="off" autoCapitalize="characters" spellCheck={false} disabled={busy} placeholder="1CAT-XXXX-XXXX-…" onChange={(event) => setCode(event.target.value)} /></label><button className="rental-primary-button" disabled={busy || !code.trim()}>{busy ? <><LoaderCircle size={16} className="rental-spin" />正在验证</> : '兑换并入账'}</button></form>
      {message && <div role={message.ok ? 'status' : 'alert'} className={`rental-code-message ${message.ok ? 'rental-code-success' : 'rental-text-error'}`}>{message.text}</div>}
      <p className="rental-code-tip"><ShieldCheck size={14} />每张码仅可兑换一次。绑定码仅限指定账户；赠送额度不可退款。余额可用于算力与数据盘费用。</p>
    </div>
  </section>;
}

function AdminCodePanel({ active, customerRevision }: { active: boolean; customerRevision: number }) {
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
    <div className="rental-card-head"><div><div className="rental-kicker">PREPAID CODES / ADMIN</div><h2>充值 SDK 生成器</h2></div><span className="rental-step-chip">一次性兑换码</span></div>
    <div className="rental-code-body"><p className="rental-code-intro">管理员核实收款或批准赠送后发码，客户在「充值与账单」自助兑换。生成时不增加任何客户余额。</p>
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

function InstanceRow({ instance, actionId, onAction, gpuStartReason = '', headlessStartReason = '', gpuRate }: { instance: RentalInstance; actionId: string | null; gpuStartReason?: string; headlessStartReason?: string; gpuRate?: number; onAction: (id: string, operation: 'start' | 'stop' | 'delete', mode?: InstanceMode) => Promise<void> }) {
  const [showPassword, setShowPassword] = useState(false);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const busy = actionId !== null;
  const running = instance.state === 'running';
  const stopped = instance.state === 'stopped' || instance.state === 'error';
  const hours = instance.releaseAt && instance.observedAt ? Math.max(0, (Date.parse(instance.releaseAt) - Date.parse(instance.observedAt)) / 3600000) : null;
  const ssh = instance.publicHost && instance.publicPort ? `ssh -p ${instance.publicPort} ${instance.username || 'gpu'}@${instance.publicHost}` : '';
  return <article className="rental-instance-v2">
    <div className="rental-instance-main">
      <div className="rental-instance-title"><div className={`rental-instance-avatar ${instance.mode === 'headless' ? 'rental-avatar-headless' : ''}`}>{instance.mode === 'headless' ? <Terminal size={21} /> : 'G'}</div><div><strong>{instance.name}</strong><small>#{instance.id}{instance.owner ? ` · ${instance.owner}` : ''} · {instance.mode === 'headless' ? '无头模式 · 无显卡' : instance.slot ? `GPU ${String(instance.slot).padStart(2, '0')}` : 'GPU 模式'}</small></div></div>
      <span className={stateClass(instance.state)}><i />{stateLabel(instance.state)}</span>
      <div className="rental-instance-actions">
        {running && !instance.owner && <button className="rental-small-button" onClick={() => { setConnectionOpen(!connectionOpen); setShowPassword(false); }}><Terminal size={14} />{connectionOpen ? '收起连接' : 'SSH 连接'}</button>}
        {stopped && <><button className="rental-small-button" disabled={busy || !!gpuStartReason} title={gpuStartReason || `16 vCPU / 62.5 GB / Gaudi2${gpuRate === undefined ? '' : ` · ${formatMoney(gpuRate)}/h`}`} onClick={() => void onAction(instance.id, 'start', 'gpu')}><Cpu size={14} />GPU 开机</button><button className="rental-small-button rental-headless-button" disabled={busy || !!headlessStartReason} title={headlessStartReason || '2 vCPU / 4 GB / 无显卡 · ¥0.08/h'} onClick={() => void onAction(instance.id, 'start', 'headless')}><Terminal size={14} />无头开机 · ¥0.08/h</button></>}
        {['running', 'creating', 'starting', 'repair_required'].includes(instance.state) && <button className="rental-small-button" disabled={busy} onClick={() => void onAction(instance.id, 'stop')}><Power size={14} />{running ? '关机' : instance.state === 'repair_required' ? '尝试回收' : '取消开机'}</button>}
        {(stopped || instance.state === 'repair_required') && <button className="rental-small-button rental-small-danger" disabled={busy} onClick={() => void onAction(instance.id, 'delete')}>释放</button>}
        {actionId === instance.id && <LoaderCircle size={16} className="rental-spin" />}
      </div>
    </div>
    <div className="rental-instance-meta">
      <span><Cpu size={14} />{instance.vcpu} vCPU</span><span><MemoryStick size={14} />{instance.memoryGB} GB</span><span><HardDrive size={14} />50 GiB 系统盘{instance.dataDiskGiB ? ` + ${instance.dataDiskGiB} GiB 数据盘` : ''}</span>
      <span>{stopped ? '上次选择' : '本次运行'} {formatMoney(instance.rateCentsPerHour ?? 0)}/h</span>
    </div>
    <div className="rental-instance-detail"><span>累计算力 {formatMoney(instance.computeCents ?? 0)} · 存储 {formatMoney(instance.storageCents ?? 0)}</span><span>数据盘 ¥{(instance.storageCnyPerDay ?? 0).toFixed(3)}/天{instance.dataDiskGiB > 0 ? '（关机仍计费）' : ''}</span></div>
    <p className={`rental-instance-message ${instance.state === 'error' || instance.state === 'repair_required' ? 'rental-text-error' : ''}`}>{instance.message}{stopped ? ` · 关机后可切换模式，磁盘与环境保留${gpuStartReason ? ` · GPU：${gpuStartReason}` : ''}${headlessStartReason ? ` · 无头：${headlessStartReason}` : ''}` : ''}</p>
    {hours !== null && <div className={`rental-expiry ${hours < 6 ? 'rental-expiry-urgent' : ''}`}><Clock3 size={14} />{hours > 0 ? `约 ${hours.toFixed(1)} 小时后自动释放` : '已到释放时间，等待清理'} · {formatDate(instance.releaseAt ?? null)} · 请提前备份，开机后取消倒计时</div>}
    {running && instance.billableAt && <small className="rental-running-since">本次计费开始于 {formatDate(instance.billableAt)} · 关机后重开按届时价格</small>}
    {running && connectionOpen && !instance.owner && <div className="rental-connection">
      <p><Terminal size={15} />{instance.connectivity === 'ready' ? '宿主 SSH 转发已就绪' : 'SSH 入口正在检查，请稍后重试'}<small>公网连接仍受隧道和网络影响</small></p>
      <div><code>{ssh || '公网映射准备中'}</code>{ssh && <CopyButton value={ssh} label="复制命令" />}</div>
      <div><code>{showPassword ? instance.password || '准备中' : '••••••••••••'}</code><button className="rental-small-button" aria-label={showPassword ? '隐藏密码' : '显示密码'} onClick={() => setShowPassword(!showPassword)}>{showPassword ? <EyeOff size={14} /> : <Eye size={14} />}</button>{instance.password && <CopyButton value={instance.password} label="复制密码" />}</div>
      <small>长任务建议使用 tmux / screen，SSH 断开后计算可继续；关闭实例则会中断任务。</small>
    </div>}
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
  return <button className="rental-small-button" onClick={() => void copy()} aria-label={label}><Copy size={13} />{copied || label}</button>;
}

function ConfirmDialog({ title, detail, danger, confirmLabel, requiredText, onCancel, onConfirm }: { title: string; detail: string; danger?: boolean; confirmLabel?: string; requiredText?: string; onCancel: () => void; onConfirm: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [typed, setTyped] = useState('');
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog className="rental-dialog" ref={ref} onCancel={onCancel} aria-labelledby="rental-confirm-title"><h2 id="rental-confirm-title">{title}</h2><p>{detail}</p>{requiredText && <label className="rental-field rental-confirm-input"><span>输入账户名 {requiredText} 确认</span><input aria-label="删除确认账户名" autoComplete="off" spellCheck={false} value={typed} onChange={(event) => setTyped(event.target.value)} /></label>}<div><button className="rental-small-button" autoFocus onClick={onCancel}>取消</button><button className={`rental-small-button ${danger ? 'rental-small-danger' : ''}`} disabled={requiredText !== undefined && typed !== requiredText} onClick={onConfirm}>{confirmLabel ?? (danger ? '确认永久释放' : '确认操作')}</button></div></dialog>;
}

type LedgerEntry = { id: number; owner: string; instance_id: number | null; cents: number; reason: string; created_at: string; actor: string | null };
function LedgerPanel({ admin = false }: { admin?: boolean }) {
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [query, setQuery] = useState('');
  useEffect(() => {
    let disposed = false, fetching = false;
    const load = async () => {
      if (fetching || document.hidden) return;
      fetching = true;
      try { const result = await rentalRequest<{ entries: LedgerEntry[] }>(admin ? '/api/admin/ledger' : '/api/rental/ledger'); if (!disposed) { setEntries(result.entries); setError(''); } }
      catch (e) { if (!disposed) setError(e instanceof Error ? e.message : '账单加载失败'); }
      finally { fetching = false; if (!disposed) setLoading(false); }
    };
    void load(); const timer = setInterval(() => void load(), 30000);
    return () => { disposed = true; clearInterval(timer); };
  }, [admin, revision]);
  const reason = (text: string) => text === 'usage' ? '算力费用' : text.startsWith('storage:') ? '数据盘费用' : text === 'registration_bonus' ? '注册赠金' : text.startsWith('redeem_cash:') ? `充值码入账 #${text.split(':')[1]}` : text.startsWith('redeem_gift:') ? `赠送码入账 #${text.split(':')[1]}` : text.startsWith('recharge') ? '管理员充值' : text;
  return <section className="rental-card rental-instances-card"><div className="rental-card-head"><div><div className="rental-kicker">BILLING HISTORY</div><h2>{admin ? '全平台收支记录' : '费用明细'}</h2></div><button className="rental-small-button" onClick={() => setRevision(revision + 1)}><RefreshCw size={14} />刷新</button></div>
    <div className="rental-billing-info">算力开机成功后计费，关机停止；数据盘按容量和保留时间计费。算力与存储按 UTC 日汇总展示，充值和赠金逐笔展示。保留最近 200 组记录。</div>
    <div className="rental-list-tools"><label><Search size={15} /><input aria-label="筛选账单" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={admin ? '搜索账户或实例 ID' : '搜索实例 ID'} /></label></div>
    {error && <p className="rental-inline-error" role="alert">{error}</p>}
    <div className="rental-table-wrap"><table className="rental-table"><thead><tr><th>时间</th>{admin && <th>账户</th>}<th>项目</th><th>实例</th><th>金额</th></tr></thead><tbody>{entries.filter((row) => `${row.instance_id ?? ''} ${admin ? row.owner : ''}`.includes(query)).map((row) => <tr key={row.id}><td>{formatDate(row.created_at)}</td>{admin && <td>{row.owner}</td>}<td>{reason(row.reason)}</td><td>{row.instance_id ? `#${row.instance_id}` : '—'}</td><td className={row.cents > 0 ? 'rental-text-credit' : ''}>{row.cents > 0 ? '+' : '−'}{formatMoney(Math.abs(row.cents))}</td></tr>)}</tbody></table></div>
    {entries.length === 0 && <div className="rental-empty">{loading ? '正在读取账单…' : '暂无收支记录'}</div>}
  </section>;
}

function AdminFleet({ storage }: { storage?: RentalState['storage'] }) {
  const [instances, setInstances] = useState<RentalInstance[]>([]);
  const [events, setEvents] = useState<Array<{ id: number; actor: string; event: string; target: string; detail: string; created_at: string }>>([]);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [actionId, setActionId] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [confirmation, setConfirmation] = useState<{ id: string; operation: 'start' | 'stop' | 'delete'; mode?: InstanceMode } | null>(null);
  useEffect(() => {
    let disposed = false, fetching = false;
    const load = async () => {
      if (fetching || document.hidden) return;
      fetching = true;
      try { const [fleet, audit] = await Promise.all([rentalRequest<RentalState>('/api/admin/instances'), rentalRequest<{ events: typeof events }>('/api/admin/audit')]); if (!disposed) { setInstances(fleet.instances); setEvents(audit.events); setError(''); } }
      catch (e) { if (!disposed) setError(e instanceof Error ? e.message : '管理信息读取失败'); }
      finally { fetching = false; }
    };
    void load(); const timer = setInterval(() => void load(), 10000);
    return () => { disposed = true; clearInterval(timer); };
  }, [revision]);
  const action = async (id: string, operation: 'start' | 'stop' | 'delete', mode?: InstanceMode, confirmed = false) => {
    if (!confirmed) { setConfirmation({ id, operation, mode }); return; }
    setConfirmation(null); setActionId(id);
    try { await rentalRequest(`/api/admin/instances/${id}/${operation}`, { method: 'POST', body: JSON.stringify(operation === 'start' ? { mode } : {}) }); setRevision((value) => value + 1); }
    catch (e) { setError(e instanceof Error ? e.message : '操作失败'); }
    finally { setActionId(null); }
  };
  const labels: Record<string, string> = { customer_deleted: '删除客户账户', customer_restored: '恢复客户账户', registration_bonus_changed: '修改注册赠金', registration_bonus_granted: '发放注册赠金', price_changed: '修改卡时价', instance_start: '启动实例', instance_stop: '关闭实例', instance_delete: '释放实例', guest_poweroff_detected: '检测到关机', recharge_codes_issued: '生成充值码', recharge_codes_revoked: '作废充值码', recharge_code_redeemed: '兑换入账' };
  return <>
    {storage && <section className="rental-card rental-storage"><div><span className="rental-kicker">INTEL NVME / SHARED STORAGE</span><h2>存储池</h2><p>{storage.mode} · 实际已用 {storage.usedGiB.toFixed(1)} GiB / {storage.totalGiB.toFixed(1)} GiB</p><progress aria-label="磁盘实际使用率" value={storage.usedGiB} max={storage.totalGiB} /><small>规格预留 {storage.reservedGiB} / {storage.budgetGiB} GiB · 物理剩余 {storage.freeGiB.toFixed(1)} GiB · 安全余量 {storage.safetyGiB} GiB</small>{storage.lowSpace && <p className="rental-text-error">物理空间不足，新建及开机已受限，请检查磁盘。</p>}</div></section>}
    <section className="rental-card rental-instances-card"><div className="rental-card-head"><div><div className="rental-kicker">PLATFORM INSTANCES</div><h2>全部客户实例</h2></div><span>{instances.length} 台保留</span></div><div className="rental-list-tools"><label><Search size={15} /><input aria-label="搜索客户实例" placeholder="搜索账户、实例名或 ID" value={query} onChange={(event) => setQuery(event.target.value)} /></label></div>{error && <p className="rental-inline-error">{error}</p>}{instances.filter((row) => `${row.owner} ${row.name} ${row.id}`.toLowerCase().includes(query.toLowerCase())).map((row) => <InstanceRow key={row.id} instance={row} actionId={actionId} onAction={action} />)}</section>
    <details className="rental-card rental-audit"><summary>操作审计 · 最近 {events.length} 条</summary><div className="rental-table-wrap"><table className="rental-table"><thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>目标 / 详情</th></tr></thead><tbody>{events.map((row) => <tr key={row.id}><td>{formatDate(row.created_at)}</td><td>{row.actor}</td><td>{labels[row.event] ?? row.event}</td><td>{row.target} · {row.detail}</td></tr>)}</tbody></table></div></details>
    {confirmation && <ConfirmDialog title="确认管理员操作" danger={confirmation.operation === 'delete'} detail={`将对客户实例 #${confirmation.id} 执行${confirmation.operation === 'delete' ? '永久释放，系统盘和数据盘将删除' : confirmation.operation === 'stop' ? '关机，运行任务将中断' : `${confirmation.mode === 'headless' ? '无头开机，¥0.08/h' : 'GPU 开机'}，成功后从客户余额计费`}。此操作将记录到审计日志。`} onCancel={() => setConfirmation(null)} onConfirm={() => void action(confirmation.id, confirmation.operation, confirmation.mode, true)} />}
  </>;
}
