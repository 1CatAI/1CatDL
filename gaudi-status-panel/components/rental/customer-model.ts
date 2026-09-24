import type { RentalInstance } from './rental-panel';

export type CustomerTab = 'instances' | 'create' | 'wallet' | 'admin';
export type CustomerFilter = 'all' | 'running' | 'stopped' | 'pending' | 'attention';

export function readCustomerTab(search: string, role: string): CustomerTab {
  const params = new URLSearchParams(search);
  if (role === 'admin' && params.get('view') !== 'workspace') return 'admin';
  const tab = params.get('tab');
  return tab === 'create' || tab === 'wallet' ? tab : 'instances';
}

export function customerSearch(tab: CustomerTab, current: string): string {
  const params = new URLSearchParams(current);
  params.set('view', tab === 'admin' ? 'admin' : 'workspace');
  if (tab !== 'admin') params.set('tab', tab);
  return `?${params}`;
}

export function filterCustomerInstances(instances: RentalInstance[], query: string, filter: CustomerFilter): RentalInstance[] {
  const term = query.trim().toLowerCase();
  return instances.filter(row => {
    const matches = !term || `${row.name} #${row.id} ${row.nodeId || 'G2-002'} ${row.mode === 'headless' ? '无头' : 'GPU'}`.toLowerCase().includes(term);
    const status = filter === 'all' || filter === 'running' && row.state === 'running' || filter === 'stopped' && row.state === 'stopped'
      || filter === 'pending' && ['creating', 'starting', 'stopping'].includes(row.state)
      || filter === 'attention' && (['error', 'repair_required'].includes(row.state) || row.nodeOnline === false);
    return matches && status;
  });
}

export function remainingTime(balanceCents: number, centsPerHour: number): string {
  if (!Number.isFinite(balanceCents) || !Number.isFinite(centsPerHour) || centsPerHour <= 0) return '暂无持续扣费';
  const hours = Math.max(0, balanceCents / centsPerHour);
  if (hours < 1) return `约 ${Math.floor(hours * 60)} 分钟`;
  if (hours >= 48) return `约 ${(hours / 24).toFixed(1)} 天`;
  return `约 ${hours.toFixed(1)} 小时`;
}
