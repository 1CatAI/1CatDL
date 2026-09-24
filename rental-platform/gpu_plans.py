"""One physical Gaudi module group is allocated atomically, never split across VMs."""


def gpu_count(value=1):
    if type(value) is not int or value not in (1, 4, 8):
        raise ValueError('GPU count must be 1, 4 or 8')
    return value


def gpu_slots(slot, count=1):
    count = gpu_count(count)
    if slot is None:
        return []
    if type(slot) is not int or not 1 <= slot <= 8:
        raise ValueError('invalid GPU slot')
    if count == 4 and slot not in (1, 5):
        raise ValueError('four GPUs require a complete module group')
    if count == 8 and slot != 1:
        raise ValueError('eight GPUs require the entire node')
    return list(range(slot, slot + count))


def instance_slots(instance):
    return gpu_slots(instance.get('slot'), instance.get('gpu_count', 1))
