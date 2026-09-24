import type { RentalInstance } from './rental-panel';
import { withRequestTimeout } from '../../lib/request-timeout.mjs';

export type AdminSection = 'instances' | 'customers' | 'finance' | 'nodes' | 'settings';
export type AdminRoute = {
  section: AdminSection; q: string; node: string; status: string; mode: string;
  owner: string; customer: string; accounts: string; finance: string;
};
export const adminDefaults: AdminRoute = { section: 'instances', q: '', node: 'all', status: 'all', mode: 'all', owner: '', customer: '', accounts: 'active', finance: 'ledger' };
export function readAdminRoute(search: string): AdminRoute {
  const params = new URLSearchParams(search);
  const route = { ...adminDefaults };
  for (const key of Object.keys(route) as Array<keyof AdminRoute>) {
    const value = params.get(key);
    if (value !== null) (route as Record<string, string>)[key] = value;
  }
  if (!['instances', 'customers', 'finance', 'nodes', 'settings'].includes(route.section)) route.section = 'instances';
  if (!['all', 'running', 'stopped', 'pending', 'attention'].includes(route.status)) route.status = 'all';
  if (!['all', 'gpu', 'headless'].includes(route.mode)) route.mode = 'all';
  if (!['active', 'deleted', 'all'].includes(route.accounts)) route.accounts = 'active';
  if (!['ledger', 'codes', 'audit'].includes(route.finance)) route.finance = 'ledger';
  return route;
}
export function adminSearch(route: AdminRoute, current: string): string {
  const params = new URLSearchParams(current);
  params.set('view', 'admin');
  for (const key of Object.keys(route) as Array<keyof AdminRoute>) {
    if (route[key] === adminDefaults[key] && key !== 'section') params.delete(key);
    else params.set(key, route[key]);
  }
  return `?${params.toString()}`;
}
export function isAttention(row: RentalInstance): boolean {
  return ['error', 'repair_required'].includes(row.state) || row.nodeOnline === false;
}
export function filterAdminInstances(rows: RentalInstance[], route: AdminRoute): RentalInstance[] {
  const query = route.q.trim().toLowerCase();
  return rows.filter(row =>
    (!route.owner || row.owner === route.owner) &&
    (route.node === 'all' || (row.nodeId || 'G2-002') === route.node) &&
    (route.mode === 'all' || (row.mode || 'gpu') === route.mode) &&
    (route.status === 'all' || (route.status === 'attention' ? isAttention(row) : route.status === 'pending' ? ['creating', 'starting', 'stopping'].includes(row.state) : row.state === route.status)) &&
    `${row.owner ?? ''} ${row.name} ${row.id} #${row.id} ${row.nodeId || 'G2-002'}`.toLowerCase().includes(query)
  ).sort((a, b) => Number(isAttention(b)) - Number(isAttention(a)) || Number(b.id) - Number(a.id));
}
export function moneyToCents(value: string, max = 1_000_000): number | null {
  if (!/^(?:0|[1-9]\d{0,6})(?:\.\d{1,2})?$/.test(value.trim())) return null;
  const [whole, decimal = ''] = value.trim().split('.');
  const cents = Number(whole) * 100 + Number(decimal.padEnd(2, '0'));
  return cents >= 1 && cents <= max ? cents : null;
}

export function formatMoney(cents: number) { return `¥${(cents / 100).toFixed(2)}`; }
export function formatDate(value: string | null | undefined) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}
export function stateLabel(value: string) {
  return ({ available: '可用', creating: '创建中', starting: '开机中', running: '运行中', stopping: '关机中', stopped: '已关机', error: '启动失败', repair_required: '待修复' } as Record<string, string>)[value] ?? value;
}
let requestIdentity = 0;
export function currentRequestIdentity() { return requestIdentity; }
export function resetRequestIdentity() { requestIdentity += 1; }
export function notifyUnauthorized(status: number, generation: number, path: string) {
  if (status === 401 && generation === requestIdentity && !['/api/auth/login', '/api/auth/register'].includes(path)) window.dispatchEvent(new Event('rental:auth-expired'));
}
export async function adminRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const generation = currentRequestIdentity();
  const headers = new Headers(init?.headers);
  headers.set('Content-Type', 'application/json');
  return withRequestTimeout(async signal => {
    const response = await fetch(path, { ...init, headers, cache: 'no-store', signal });
    const result = await response.json().catch(() => ({})) as T & { message?: string; error?: string };
    notifyUnauthorized(response.status, generation, path);
    if (!response.ok) throw new Error(`${response.status}: ${result.message || result.error || '请求失败'}`);
    return result;
  }, 15000, init?.signal);
}
export function adminRequestKey() { return Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join(''); }
