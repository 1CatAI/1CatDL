"""Pure new-instance placement policy. No allocation, I/O, or billing writes."""
from math import isfinite
from gpu_plans import gpu_slots


def placement_decision(policy, nodes, totals, held_slots, observations, *, mode, count, system_gib, data_gib):
    preferred = policy['preferredNode']
    need = system_gib + data_gib
    checks = {}
    for ident, budget in nodes.items():
        observed = observations.get(ident, {})
        storage = observed.get('storage', {})
        used = totals.get(ident, {'system': 0, 'data': 0})
        pool = budget['storage_pool_budget']
        logical = (used['system'] + used['data'] + need <= pool if pool else
                   used['system'] + system_gib <= budget['system_disk_budget'] and
                   used['data'] + data_gib <= budget['data_disk_budget'])
        free, reserve = storage.get('freeGiB'), storage.get('safetyGiB')
        known = all(type(v) in (int, float) and isfinite(v) and v >= 0 for v in (free, reserve))
        space = bool(known and free - reserve >= need and not storage.get('lowSpace', True) and logical)
        starts = (1, 5) if count == 4 else (1,) if count == 8 else range(1, budget['slot_count'] + 1)
        gpu = mode == 'headless' or any(start + count - 1 <= budget['slot_count'] and
                not held_slots.get(ident, set()).intersection(gpu_slots(start, count)) for start in starts)
        online = observed.get('online') is True
        image = observed.get('imageReady') is True
        checks[ident] = {'id': ident, 'online': online, 'imageReady': image,
                         'storageAvailable': space, 'gpuAvailable': gpu,
                         'allGpusAllocated': all(slot in held_slots.get(ident, set()) for slot in range(1, budget['slot_count'] + 1)),
                         'createAvailable': online and image and space}
    primary = checks.get(preferred, {})
    reasons = []
    if not primary.get('online'):
        reasons.append('primary_offline')
    else:
        if not primary.get('storageAvailable'):
            reasons.append('primary_storage_insufficient')
        if primary.get('allGpusAllocated') or not primary.get('gpuAvailable'):
            reasons.append('primary_gpu_insufficient')
    fallback = not policy['enabled'] or bool(reasons)
    labels = {'primary_offline': f'{preferred}离线',
              'primary_storage_insufficient': f'{preferred}存储空间不足',
              'primary_gpu_insufficient': f'{preferred}没有可分配的完整四卡组' if count == 4 else f'{preferred}没有可分配的完整八卡组' if count == 8 else f'{preferred}GPU已占满'}
    for ident, item in checks.items():
        item['allowed'] = ident == preferred or fallback
        item['reason'] = (f'优先使用{preferred}；仅主节点GPU不足、存储不足或离线时开放其他节点' if not item['allowed'] else
                          '节点离线' if not item['online'] else '镜像尚未就绪' if not item['imageReady'] else
                          '存储空间不足（含系统盘、数据盘与安全余量）' if not item['storageAvailable'] else
                          '创建不占GPU，开机时可能需要等待资源' if not item['gpuAvailable'] else '')
    eligible = [ident for ident, item in checks.items() if item['allowed'] and item['createAvailable']]
    # Prefer an immediately usable fallback if the primary lacks GPUs; still
    # allow stopped-instance creation when all GPUs are occupied, as before.
    ranked = sorted(eligible, key=lambda ident: (not checks[ident]['gpuAvailable'], ident != preferred, ident))
    return {'policy': dict(policy), 'fallbackAllowed': fallback, 'reasonCodes': reasons,
            'reason': '；'.join(labels[reason] for reason in reasons) if policy['enabled'] else '管理员已关闭优先节点限制',
            'recommendedNode': ranked[0] if ranked else None, 'nodes': list(checks.values())}
