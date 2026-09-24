import type { RentalState } from './rental-panel';

export type GpuCount = 1 | 4 | 8;
export const gpuMemoryGB = (count: GpuCount) => count === 8 ? 480 : 62.5 * count;
export const gpuMemoryMB = (count: GpuCount) => count === 8 ? 480000 : 62500 * count;

// Same fixed module groups as the transactional scheduler, never across nodes.
export function gpuCapacityReason(state: RentalState, nodeId = 'G2-002', count = 1): string {
  const node = state.nodes?.find(node => node.id === nodeId);
  if ((node?.availableCpu ?? Infinity) < 16 * count || (node?.availableMemoryMB ?? Infinity) < gpuMemoryMB(count as GpuCount)) return '所属节点 CPU 或内存不足';
  const free = new Set(state.slots.filter(slot => (slot.nodeId || 'G2-002') === nodeId && slot.state === 'available').map(slot => slot.slot));
  if (count === 4) return [1, 5].some(start => [0, 1, 2, 3].every(offset => free.has(start + offset))) ? '' : '需要同一节点完整空闲的四卡组';
  if (count === 8) return [1, 2, 3, 4, 5, 6, 7, 8].every(slot => free.has(slot)) ? '' : '需要同一节点全部八卡空闲';
  return free.size > 0 ? '' : '所属节点 GPU 资源不足';
}
