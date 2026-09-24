'use client';

import type { ReactNode } from 'react';
import { ArrowRight, Check, CheckCircle2, CircleHelp, Cpu, HardDrive, LayoutDashboard, LogOut, Plus, ReceiptText, RefreshCw, Server, ShieldCheck, Terminal, UserRound, Wallet } from 'lucide-react';
import type { RentalAccount, RentalState } from './rental-panel';
import type { PlacementQuote } from './placement-settings';
import { gpuCapacityReason, gpuMemoryGB, type GpuCount } from './gpu-plans';
import { remainingTime, type CustomerTab } from './customer-model';

type Creation = {
  mode: 'gpu' | 'headless'; gpuCount: GpuCount; nodeId: string; name: string; disk: number;
  placement: PlacementQuote | null; placementError: string; canOrder: boolean; submitting: boolean;
  onMode: (mode: 'gpu' | 'headless') => void; onCount: (count: GpuCount) => void;
  onNode: (node: string) => void; onName: (name: string) => void; onDisk: (disk: number) => void;
  onOrder: () => void;
};

type Props = {
  account: RentalAccount; state: RentalState; tab: CustomerTab; loading: boolean; hourlyCost: number;
  onNavigate: (tab: CustomerTab) => void; onRefresh: () => void; onLogout: () => void;
  brand: ReactNode; contact: ReactNode; notices: ReactNode; children: ReactNode; creation: Creation;
};

const money = (cents: number) => `¥${(cents / 100).toFixed(2)}`;
const pageInfo = {
  instances: ['我的实例', '连接正在运行的环境，或管理已保留的实例。'],
  create: ['创建实例', '先创建环境，再按需开机。算力从开机成功后开始计费。'],
  wallet: ['充值与账单', '兑换充值码，查看账户余额与费用明细。'],
  admin: ['我的实例', '连接正在运行的环境，或管理已保留的实例。'],
};

export function CustomerWorkspace({ account, state, tab, loading, hourlyCost, onNavigate, onRefresh, onLogout, brand, contact, notices, children, creation }: Props) {
  const free = state.slots.filter(slot => slot.state === 'available' && state.nodes?.find(node => node.id === (slot.nodeId || 'G2-002'))?.online !== false).length;
  const running = state.instances.filter(instance => instance.state === 'running').length;
  return <main className="rental-app customer-ui customer-workspace" hidden={tab === 'admin'}>
    <div className="customer-shell">
      <header className="customer-header">
        {brand}
        <nav className="customer-nav" aria-label="工作台导航">
          <button aria-current={tab === 'instances' ? 'page' : undefined} onClick={() => onNavigate('instances')}><LayoutDashboard size={16} />我的实例</button>
          <button aria-current={tab === 'create' ? 'page' : undefined} onClick={() => onNavigate('create')}><Plus size={17} />创建实例</button>
          <button aria-current={tab === 'wallet' ? 'page' : undefined} onClick={() => onNavigate('wallet')}><ReceiptText size={16} />充值与账单</button>
        </nav>
        <div className="customer-user"><span title={account.name}><UserRound size={15} />{account.name}</span>{account.role === 'admin' && <button className="customer-text-button" onClick={() => onNavigate('admin')}>管理后台</button>}<button className="customer-text-button" onClick={onLogout}><LogOut size={15} /><span>退出登录</span></button></div>
      </header>

      <div className="customer-page-heading"><div><h1>{pageInfo[tab][0]}</h1><p>{pageInfo[tab][1]}</p></div><div className="customer-heading-actions"><button className="rental-small-button" onClick={onRefresh} aria-label="刷新工作台"><RefreshCw size={15} />刷新</button>{tab === 'instances' && <button className="rental-primary-button customer-create-button" onClick={() => onNavigate('create')}><Plus size={16} />创建实例</button>}</div></div>
      <section className="customer-account-strip" aria-label="账户与资源概览">
        <div className="customer-balance"><Wallet size={17} /><div><span>账户余额</span><strong>{loading ? '—' : money(account.balanceCents)}</strong></div>{tab !== 'wallet' && <button className="customer-text-button" onClick={() => onNavigate('wallet')}>去充值<ArrowRight size={14} /></button>}</div>
        <div><span>当前费用</span><strong>{loading ? '—' : `${money(hourlyCost)} / 小时`}</strong><small>含数据盘折算 · {loading ? '正在读取' : `${remainingTime(account.balanceCents, hourlyCost)}${hourlyCost > 0 ? '，仅供参考' : ''}`}</small></div>
        <div><span>实例与资源</span><strong>{loading ? '—' : `${running} 台运行中`}<small> / {loading ? '—' : state.instances.length} 台</small></strong><small>{loading ? '正在读取资源' : `GPU ${free} / ${state.slots.length} 可用 · 开机时分配`}</small></div>
      </section>
      {notices}
      {account.role === 'customer' && tab !== 'wallet' && <div className="customer-contact-strip">{contact}</div>}
      {tab === 'create' && <CreateInstance state={state} loading={loading} options={creation} onRecharge={() => onNavigate('wallet')} />}
      {children}
      <footer className="customer-footer"><span><ShieldCheck size={14} />GPU 每人同时 1 台 · 无头可同时多台</span><span>关机 48 小时后自动释放全部磁盘</span><span>{state.updatedAt ? `同步于 ${new Date(state.updatedAt).toLocaleTimeString('zh-CN', { hour12: false })} · 5 秒自动同步` : '正在连接资源调度器'} · 后台暂停刷新</span></footer>
    </div>
  </main>;
}

function CreateInstance({ state, loading, options: o, onRecharge }: { state: RentalState; loading: boolean; options: Creation; onRecharge: () => void }) {
  const headlessRate = state.billing.headlessRateCentsPerHour ?? state.headless?.rateCentsPerHour ?? 8;
  const gpuRate = state.billing.rateCentsPerHour ?? 0;
  const rate = o.mode === 'headless' ? headlessRate : gpuRate * o.gpuCount;
  const nodeId = o.nodeId === 'auto' ? o.placement?.recommendedNode : o.nodeId;
  const selected = o.placement?.nodes.find(node => node.id === nodeId);
  const gpuReason = o.mode === 'gpu' && nodeId ? gpuCapacityReason(state, nodeId, o.gpuCount) : '';
  const maxDisk = state.limits.dataDiskGiB[1];
  const diskPrice = (o.gpuCount === 8 ? 0 : o.disk) * (state.billing.extraDataDiskCnyPerGiBDay ?? 0);
  const eightCreateBalanceCents = state.billing.gpuPlans?.find(plan => plan.gpuCount === 8)?.minCreateBalanceExclusiveCents ?? 10000;
  const disabledReason = loading ? '正在读取配置…' : state.service === 'maintenance' ? state.serviceMessage : o.placementError ? '调度核对失败，请刷新后重试。' : !o.placement ? '正在核对所属节点…' : !selected?.allowed || !selected?.createAvailable ? selected?.reason || o.placement.reason || '暂无可创建节点，请稍后重试。' : '';
  return <div className="customer-create-layout">
    <form id="customer-order-form" className="customer-configuration" onSubmit={event => { event.preventDefault(); if (o.canOrder && !loading) o.onOrder(); }}>
      <section className="customer-config-section" aria-labelledby="customer-plan-title">
        <div className="customer-section-heading"><h2 id="customer-plan-title">选择计算套餐</h2><span>GPU 独占 · 无头不占显卡</span></div>
        <fieldset className="customer-plans" aria-label="计算套餐">
          {([1, 4, 8] as const).map(count => <button type="button" className="customer-plan" key={count} aria-pressed={o.mode === 'gpu' && o.gpuCount === count} onClick={() => { o.onMode('gpu'); o.onCount(count); }}>
            <span className="customer-plan-title"><Cpu size={19} /><strong>{count} 卡 Gaudi2</strong>{o.mode === 'gpu' && o.gpuCount === count && <Check size={16} aria-label="已选中" />}</span>
            <span>{16 * count} vCPU · {gpuMemoryGB(count)} GB 内存{count === 8 ? ' · 赠 600 GiB 数据盘' : ''}</span><strong className="customer-plan-price">{loading ? '—' : money(gpuRate * count)}<small> / 小时</small></strong>
          </button>)}
          <button type="button" className="customer-plan" aria-pressed={o.mode === 'headless'} onClick={() => o.onMode('headless')}><span className="customer-plan-title"><Terminal size={19} /><strong>无头模式</strong>{o.mode === 'headless' && <Check size={16} aria-label="已选中" />}</span><span>2 vCPU · 4 GB 内存 · 无 GPU</span><strong className="customer-plan-price">{money(headlessRate)}<small> / 小时</small></strong></button>
        </fieldset>
        <p className="customer-help">无头模式适合安装依赖、下载模型和配置环境。关机后可以切换模式，磁盘与环境保留。</p>
        {o.mode === 'headless' && <label className="customer-future-plan"><span>后续 GPU 套餐</span><select aria-label="后续 GPU 套餐" value={o.gpuCount} onChange={event => o.onCount(Number(event.target.value) as GpuCount)}><option value={1}>1 卡 · 16 vCPU / 62.5 GB</option><option value={4}>4 卡 · 64 vCPU / 250 GB</option><option value={8}>8 卡 · 128 vCPU / 480 GB</option></select><small>当前不占 GPU；套餐创建后固定，切回 GPU 时重新检查资源。</small></label>}
      </section>

      <section className="customer-config-section" aria-labelledby="customer-environment-title">
        <div className="customer-section-heading"><h2 id="customer-environment-title">实例环境</h2></div>
        <label className="rental-field"><span>实例名称</span><input value={o.name} maxLength={32} onChange={event => o.onName(event.target.value)} placeholder="例如 gaudi-dev" autoComplete="off" /></label>
        <div className="customer-image"><Server size={20} /><div><span>系统镜像</span><strong>{state.image.name}</strong><small>{state.image.version} · {state.image.ready ? '镜像就绪' : '镜像未就绪'}</small><details><summary>查看环境说明</summary><p>{state.image.detail}</p></details></div>{state.image.ready && <CheckCircle2 size={17} aria-label="镜像就绪" />}</div>
        <div className="customer-disk-heading"><h3><HardDrive size={16} />存储配置</h3><span>{state.limits.systemDiskGiB} GiB 系统盘已包含</span></div>
        {o.gpuCount === 8 ? <p className="customer-included-disk"><HardDrive size={16} />固定赠送 600 GiB 数据盘，挂载到 /data，不可调整，赠送部分不收存储费。</p> : <><div className="customer-disk-control"><label className="rental-field" htmlFor="customer-data-disk"><span>附加数据盘 <small>可选</small></span><div className="customer-input-unit"><input id="customer-data-disk" type="number" min={0} max={maxDisk} step={1} value={o.disk} onChange={event => { const value = Number(event.target.value); if (Number.isFinite(value)) o.onDisk(Math.min(maxDisk, Math.max(0, Math.round(value)))); }} /><span>GiB</span></div></label><fieldset className="customer-disk-presets" aria-label="数据盘快捷容量">{[0, 50, 100, 200].filter(value => value <= maxDisk).map(value => <button key={value} type="button" aria-pressed={o.disk === value} onClick={() => o.onDisk(value)}>{value === 0 ? '不添加' : `${value} GiB`}</button>)}</fieldset></div><p className="customer-help">{o.disk > 0 ? `挂载到 /data，¥${diskPrice.toFixed(3)} / 天。` : '默认不带数据盘，无额外存储费。'} 数据盘从创建起计费，关机后仍收费。</p></>}
      </section>

      <section className="customer-config-section" aria-labelledby="customer-placement-title">
        <div className="customer-section-heading"><h2 id="customer-placement-title">所属节点</h2><span>创建后固定，不自动迁移</span></div>
        <label className="rental-field"><span>节点选择</span><select value={o.nodeId} onChange={event => o.onNode(event.target.value)}><option value="auto">自动选择{o.placement?.recommendedNode ? ` · 推荐 ${o.placement.recommendedNode}` : ''}</option>{(state.nodes ?? []).map(node => { const check = o.placement?.nodes.find(item => item.id === node.id); return <option key={node.id} value={node.id} disabled={!check?.allowed || !check.createAvailable}>{node.id}{!check ? ' · 核对中' : !check.allowed ? ' · 暂不开放' : !check.createAvailable ? ' · 暂不可创建' : ''}</option>; })}</select></label>
        <p className="customer-help">{o.placement?.policy.enabled ? `优先使用 ${o.placement.policy.preferredNode}；GPU 不足、存储不足或离线时才开放其他节点。` : o.placement ? '管理员已允许自由选择节点。' : '正在核对调度规则。'}</p>
        <div className="customer-node-availability">{(state.nodes ?? []).map(node => { const slots = state.slots.filter(slot => (slot.nodeId || 'G2-002') === node.id); return <div key={node.id}><span><i className={node.online ? 'is-online' : 'is-offline'} />{node.id}</span><strong>{node.online ? `${slots.filter(slot => slot.state === 'available').length} / ${slots.length} GPU 可用` : '离线'}</strong><small>{node.online ? `无头可启动 ${node.headlessAvailable} 台` : '暂不可用'}</small></div>; })}</div>
        {o.placementError && <p className="customer-warning" role="alert">{o.placementError}</p>}
        {selected?.reason && <p className="customer-help">{selected.reason}</p>}
      </section>
    </form>

    <aside className="customer-checkout" aria-labelledby="customer-checkout-title">
      <h2 id="customer-checkout-title">费用预估</h2><p className="customer-checkout-plan">{o.mode === 'headless' ? '无头模式' : `${o.gpuCount} 卡 Gaudi2`}<span>{nodeId || '正在选择节点'}</span></p>
      <dl className="customer-checkout-spec"><div><dt>计算配置</dt><dd>{o.mode === 'headless' ? '2 vCPU / 4 GB' : `${16 * o.gpuCount} vCPU / ${gpuMemoryGB(o.gpuCount)} GB`}</dd></div><div><dt>系统盘</dt><dd>{state.limits.systemDiskGiB} GiB · 已包含</dd></div><div><dt>数据盘</dt><dd>{o.gpuCount === 8 ? '600 GiB · 已赠送' : o.disk ? `${o.disk} GiB` : '不添加'}</dd></div></dl>
      <div className="customer-price-line"><span>算力费用</span><strong>{loading ? '—' : money(rate)}<small> / 小时</small></strong><p>开机成功后计费，关机停止</p></div>
      <div className="customer-price-line"><span>数据盘费用</span><strong>¥{diskPrice.toFixed(3)}<small> / 天</small></strong><p>{o.gpuCount === 8 ? '600 GiB 固定赠送，关机保留期间也不收费' : o.disk ? '创建后持续计费，关机仍收费' : '没有额外数据盘费用'}</p></div>
      {gpuReason && <p className="customer-warning"><CircleHelp size={15} />{gpuReason}。可先创建，开机时再检查资源。</p>}
      {disabledReason && <p className="customer-warning" role={o.placementError ? 'alert' : undefined}>{disabledReason}</p>}
      {!loading && (o.gpuCount === 8 ? state.account.balanceCents <= eightCreateBalanceCents : state.account.balanceCents <= 0) && <p className="customer-warning">{o.gpuCount === 8 ? `创建 8 卡实例需余额大于 ${money(eightCreateBalanceCents)}；创建后开机不再设最低余额。余额耗尽仍会自动关机。` : '开机前需保证余额充足。'}<button type="button" className="customer-text-button" onClick={onRecharge}>前往充值</button></p>}
      <button className="rental-primary-button" form="customer-order-form" type="submit" disabled={!o.canOrder || loading}>{o.submitting ? '正在创建…' : '创建实例'}{!o.submitting && <ArrowRight size={16} />}</button>
      <p className="customer-checkout-note">创建不自动开机、不预占 GPU。{o.gpuCount === 8 ? `创建时余额须大于 ${money(eightCreateBalanceCents)}；之后开机不重复检查余额门槛，余额耗尽仍会自动关机。` : '创建成功后，在实例列表选择开机。'}</p>
      <div className="customer-retention"><ShieldCheck size={16} /><p>关机后保留 48 小时，到期自动删除系统盘和数据盘。请提前备份。</p></div>
    </aside>
  </div>;
}
