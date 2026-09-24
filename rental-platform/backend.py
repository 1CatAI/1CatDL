"""Host adapter. All arguments are server-owned IDs/config; no shell interpolation."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET
from shared_storage import TAG as SHARED_TAG, load_config as load_shared_config, validate_export, guest_install_script
from gpu_plans import gpu_count, instance_slots


def command(args, timeout=30):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        # Do not return subprocess input, guest output or secrets to API clients.
        raise RuntimeError(f'{Path(args[0]).name} failed (exit {proc.returncode})')
    return proc.stdout.strip()


class LibvirtBackend:
    def __init__(self, root, config):
        self.root = Path(root).resolve()
        self.config = config
        self.shared_config = load_shared_config(config)
        self.shared_states = {}
        self.disks = self.root / 'instances'
        self.disks.mkdir(parents=True, exist_ok=True)
        self.system_root = Path(config.get('system_root') or self.root).resolve()
        if not self.system_root.is_dir() or self.system_root == Path('/'):
            raise RuntimeError('configured system storage root is missing')
        self.system_disks = self.system_root / 'instances'
        self.system_disks.mkdir(parents=True, exist_ok=True)
        configured_data_root = config.get('data_root')
        if not configured_data_root:
            raise ValueError('data_root is required')
        self.data_root = Path(configured_data_root).resolve()
        if self.data_root in (Path('/'), self.root) or self.root in self.data_root.parents:
            raise ValueError('data_root must be outside rental root')
        if not self.data_root.is_dir():
            raise RuntimeError('configured data root is missing')
        self.data_disks = self.data_root / 'instances'
        self.data_disks.mkdir(parents=True, exist_ok=True)

    def name(self, instance):
        ident = str(instance['id'])
        if ident.isdigit():
            return 'cat-rental-' + ident
        if not re.fullmatch(r'[a-zA-Z0-9_-]{8,64}', ident):
            raise ValueError('Invalid internal instance ID')
        return 'cat-' + ident

    def path(self, instance):
        self.name(instance)
        result = (self.disks / instance['id']).resolve()
        if result.parent != self.disks.resolve():
            raise ValueError('Unsafe instance path')
        return result

    def data_path(self, instance):
        self.name(instance)
        result = (self.data_disks / instance['id']).resolve()
        if result.parent != self.data_disks.resolve():
            raise ValueError('Unsafe data instance path')
        return result

    def system_path(self, instance):
        self.name(instance)
        result = (self.system_disks / instance['id']).resolve()
        if result.parent != self.system_disks.resolve():
            raise ValueError('Unsafe system instance path')
        return result / 'system.qcow2'

    def virsh(self, *args, timeout=30):
        return command(['virsh', '-c', 'qemu:///system', *map(str, args)], timeout)

    def online(self):
        try:
            self.virsh('list', '--all', '--name', timeout=3)
            return True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return False

    def ready(self):
        try:
            validate_export(self.shared_config)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
            return False
        image = Path(self.config['image'])
        manifest = image.with_suffix('.manifest.json')
        storage_ready = self.data_root.is_dir() and self.data_disks.is_dir()
        if storage_ready and self.config.get('enforce_separate_data_device', True):
            storage_ready = os.stat(self.data_root).st_dev != os.stat(self.root).st_dev
            if self.system_root != self.root:
                storage_ready = storage_ready and os.stat(self.system_root).st_dev == os.stat(self.data_root).st_dev
        return (self.config.get('enabled') is True and image.is_file() and manifest.is_file()
                and json.loads(manifest.read_text()).get('validated') is True and storage_ready)

    def state(self, instance):
        try:
            names = self.virsh('list', '--all', '--name').splitlines()
            if self.name(instance) not in names:
                return 'absent'
            value = self.virsh('domstate', self.name(instance)).strip()
            return {'running': 'running', 'in shutdown': 'stopping', 'shut off': 'off'}.get(value, 'unknown')
        except (RuntimeError, subprocess.TimeoutExpired):
            return 'unknown'

    def preflight(self, slot):
        bdf = self.config['bdfs'][int(slot)-1]
        device = Path('/sys/bus/pci/devices') / bdf
        if device.joinpath('driver').resolve().name != 'habanalabs':
            raise RuntimeError('GPU is not available on host')
        status = command(['hl-smi', '-i', bdf, '-q'], 15)
        if 'Operational' not in status:
            raise RuntimeError('GPU health check failed')
        if any(p.name.startswith('accel') for p in device.glob('accel/*')):
            # Existing compute FDs must not be detached under a customer's workload.
            nodes = [str(Path('/dev/accel') / p.name) for p in device.glob('accel/*')]
            if nodes:
                result = subprocess.run(['fuser', *nodes], capture_output=True, timeout=10)
                if result.returncode == 0:
                    raise RuntimeError('GPU is in use on host')

    def prepare(self, instance, password):
        if not self.ready():
            raise RuntimeError('Validated guest image is not enabled')
        directory = self.path(instance)
        system = self.system_path(instance)
        data_directory = self.data_path(instance)
        if directory.exists():
            required = ['seed.iso', 'VARS.fd']
            missing = [name for name in required if not (directory / name).is_file()]
            if not system.is_file():
                missing.append('system.qcow2@system_root')
            if int(instance['data_disk']) > 0 and not (data_directory / 'data.qcow2').is_file():
                missing.append('data.qcow2@data_root')
            if missing:
                raise RuntimeError('instance storage is incomplete: ' + ','.join(missing))
            return  # Existing disks are never overwritten on restart/recovery.
        directory.mkdir(mode=0o750)
        system.parent.mkdir(mode=0o750, exist_ok=True)
        image = str(Path(self.config['image']).resolve())
        command(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', image,
                 str(system), '50G'])
        size = int(instance['data_disk'])
        if size:
            data_directory.mkdir(mode=0o750)
            command(['qemu-img', 'create', '-f', 'qcow2', str(data_directory/'data.qcow2'), f'{size}G'])
        # Hash via stdin; plaintext password never appears in process arguments/files.
        result = subprocess.run(['openssl','passwd','-6','-stdin'], input=password+'\n',
                                text=True,capture_output=True,check=True)
        user = {'users': [{'name':'gpu','shell':'/bin/bash','sudo':'ALL=(ALL) NOPASSWD:ALL',
                           # The base image already contains gpu. cloud-init's
                           # `passwd` only applies while creating a new user;
                           # `hashed_passwd` also updates an existing user.
                           'lock_passwd':False,'hashed_passwd':result.stdout.strip(),
                           # Gaudi device nodes are protected by these groups.
                           # Keep this in cloud-init so every fresh instance is
                           # correct even when a candidate image is reused.
                           'groups':['render','video']}],
                'ssh_pwauth':True,'disable_root':True,'ssh_deletekeys':True,
                'chpasswd':{'expire':False},
                'growpart':{'mode':'auto','devices':['/']},'resize_rootfs':True,
                'runcmd':[
                    # The Ubuntu image ships this unit without a WantedBy entry;
                    # explicitly install the boot-time symlink for every guest.
                    ['mkdir','-p','/etc/systemd/system/multi-user.target.wants'],
                    ['ln','-sf','/lib/systemd/system/qemu-guest-agent.service',
                     '/etc/systemd/system/multi-user.target.wants/qemu-guest-agent.service'],
                    # Defensive fallback for images with incomplete group data.
                    ['sh','-c',"getent group render >/dev/null || groupadd --system render; getent group video >/dev/null || groupadd --system video; usermod -aG render,video gpu"],
                    ['systemctl','start','qemu-guest-agent']]}
        if size:
            user.update({'disk_setup':{'/dev/vdb':{'table_type':'gpt','layout':True,'overwrite':False}},
                         'fs_setup':[{'label':'data','filesystem':'ext4','device':'/dev/vdb1'}],
                         'mounts':[['LABEL=data','/data','ext4','defaults,nofail','0','2']]})
            user['runcmd'].append(['chown','gpu:gpu','/data'])
        (directory/'user-data').write_text('#cloud-config\n'+json.dumps(user),encoding='utf-8')
        (directory/'meta-data').write_text(json.dumps({'instance-id':instance['id'],
                                                     'local-hostname':self.name(instance)}))
        network = {'version':2,'ethernets':{'nic':{'match':{'name':'en*'},'dhcp4':True}}}
        (directory/'network-data').write_text(json.dumps(network))
        command(['cloud-localds','--network-config',str(directory/'network-data'),
                 str(directory/'seed.iso'),str(directory/'user-data'),str(directory/'meta-data')])
        shutil.copy2('/usr/share/OVMF/OVMF_VARS_4M.fd',directory/'VARS.fd')
        command(['chown','-R','libvirt-qemu:kvm',str(directory)])
        if system.parent != directory:
            command(['chown','-R','libvirt-qemu:kvm',str(system.parent)])
        if size:
            command(['chown','-R','libvirt-qemu:kvm',str(data_directory)])

    def start(self, instance):
        if self.state(instance) == 'running':
            return
        headless = instance.get('mode') == 'headless'
        slot = None if headless else int(instance['slot'])
        count = gpu_count(instance.get('gpu_count', 1))
        if not headless and count == 4 and (instance['vcpu'], instance['memory_mb']) != (64, 250000):
            raise ValueError('four GPUs require 64 vCPU and 250000 MB')
        if not headless and count == 8 and (slot, instance['vcpu'], instance['memory_mb'], instance['data_disk']) != (1, 128, 480000, 600):
            raise ValueError('eight GPUs require slot 1, 128 vCPU, 480000 MB and the included 600 GiB data disk')
        if headless and (instance['vcpu'] != 2 or instance['memory_mb'] != 4000 or instance.get('slot') is not None):
            raise ValueError('headless must have 2 vCPU, 4000 MB and no GPU slot')
        # Fail closed before detaching a GPU if the host export lost read-only
        # protection or the expected RAID filesystem is not mounted.
        shared_export = validate_export(self.shared_config)
        if (self.path(instance) / 'shared-storage-disabled').exists():
            shared_export = None
        self.shared_states.pop(str(instance['id']), None)
        if not headless and count == 8:
            self.eight_card_numa(instance)
            self.eight_card_memory_guard(instance)
        if not headless:
            for allocated_slot in instance_slots(instance):
                self.preflight(allocated_slot)
        numa = self.four_card_numa(instance) if not headless and count == 4 else None
        directory = self.path(instance)
        domain = ET.Element('domain',{'type':'kvm','xmlns:qemu':'http://libvirt.org/schemas/domain/qemu/1.0'})
        def add(parent, tag, text=None, **attrs):
            item=ET.SubElement(parent,tag,{k:str(v) for k,v in attrs.items()})
            item.text=None if text is None else str(text)
            return item
        add(domain,'name',self.name(instance))
        # Retain libvirt's UUID when redefining a stopped domain.
        try:
            existing_uuid = self.virsh('domuuid', self.name(instance))
        except RuntimeError:
            existing_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, '1cat-rental-' + str(instance['id'])))
        add(domain,'uuid',existing_uuid)
        add(domain,'memory',instance['memory_mb'],unit='MB')
        add(domain,'vcpu',instance['vcpu'], **({'cpuset': numa[1]} if numa else {'placement':'static'} if count == 8 and not headless else {}))
        if count == 8 and not headless:
            tune = add(domain, 'cputune')
            for vcpu in range(128):
                add(tune, 'vcpupin', vcpu=vcpu,
                    cpuset='4-31,68-95' if vcpu < 64 else '36-63,100-127')
            add(tune, 'emulatorpin', cpuset='0-3,32-35,64-67,96-99')
        if numa:
            add(add(domain,'numatune'),'memory',mode='preferred',nodeset=numa[0])
        if count == 8 and not headless:
            numatune = add(domain, 'numatune')
            add(numatune, 'memnode', cellid='0', mode='preferred', nodeset='0')
            add(numatune, 'memnode', cellid='1', mode='preferred', nodeset='1')
        if shared_export or count == 8 and not headless:
            memory_backing = add(domain, 'memoryBacking')
            add(memory_backing, 'source', type='memfd')
            add(memory_backing, 'access', mode='shared')
        osxml=add(domain,'os')
        add(osxml,'type','hvm',arch='x86_64',machine='pc-q35-noble')
        add(osxml,'loader','/usr/share/OVMF/OVMF_CODE_4M.fd',readonly='yes',type='pflash')
        add(osxml,'nvram',str(directory/'VARS.fd'), template='/usr/share/OVMF/OVMF_VARS_4M.fd')
        add(osxml,'boot',dev='hd')
        features=add(domain,'features'); add(features,'acpi'); add(features,'apic')
        cpu=add(domain,'cpu',mode='host-passthrough',check='none')
        add(cpu,'topology',sockets='2' if count == 8 and not headless else '1',
            cores='64' if count == 8 and not headless else instance['vcpu'],threads='1')
        if not headless:
            add(cpu,'maxphysaddr',mode='emulate',bits='42')
        if count == 8 and not headless:
            guest_numa = add(cpu, 'numa')
            add(guest_numa, 'cell', id='0', cpus='0-63', memory='240000', unit='MB')
            add(guest_numa, 'cell', id='1', cpus='64-127', memory='240000', unit='MB')
        add(domain,'on_poweroff','destroy'); add(domain,'on_reboot','restart'); add(domain,'on_crash','destroy')
        devices=add(domain,'devices')
        add(devices,'emulator','/usr/bin/qemu-system-x86_64')
        if shared_export:
            filesystem = add(devices, 'filesystem', type='mount', accessmode='passthrough')
            add(filesystem, 'driver', type='virtiofs', queue='1024')
            binary = add(filesystem, 'binary', path=self.shared_config.get('binary', '/usr/libexec/virtiofsd'))
            add(binary, 'sandbox', mode='namespace')
            add(filesystem, 'source', dir=shared_export)
            add(filesystem, 'target', dir=SHARED_TAG)
        root=add(devices,'controller',type='pci',index='0',model='pcie-root')
        if not headless:
            add(root,'pcihole64',2147483648,unit='KiB')
        for index in range(1,17 if count == 8 and not headless else 13 if count == 4 and not headless else 9):
            port=add(devices,'controller',type='pci',index=index,model='pcie-root-port')
            add(port,'target',chassis=index,port=hex(7+index))
        disk_files = [(self.system_path(instance), 'vda')]
        data_overlay = self.data_path(instance) / 'data.qcow2'
        if data_overlay.exists():
            disk_files.append((data_overlay, 'vdb'))
        for file,target in disk_files:
            if not file.exists(): continue
            disk=add(devices,'disk',type='file',device='disk')
            add(disk,'driver',name='qemu',type='qcow2',cache='none',discard='unmap')
            add(disk,'source',file=str(file));add(disk,'target',dev=target,bus='virtio')
        disk=add(devices,'disk',type='file',device='cdrom')
        add(disk,'driver',name='qemu',type='raw');add(disk,'source',file=str(directory/'seed.iso'))
        add(disk,'target',dev='sda',bus='sata');add(disk,'readonly')
        iface=add(devices,'interface',type='network')
        add(iface,'mac',address=self.mac_address(instance))
        add(iface,'source',network=self.config['network']);add(iface,'model',type='virtio')
        console=add(devices,'console',type='pty');add(console,'target',type='serial',port='0')
        channel=add(devices,'channel',type='unix');add(channel,'target',type='virtio',name='org.qemu.guest_agent.0')
        if not headless:
            for guest_bus, allocated_slot in enumerate(instance_slots(instance), 5):
                hostdev=add(devices,'hostdev',mode='subsystem',type='pci',managed='yes')
                source=add(hostdev,'source')
                m=re.fullmatch(r'([0-9a-f]{4}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])',self.config['bdfs'][allocated_slot-1])
                if not m: raise ValueError('Invalid configured PCI address')
                add(source,'address',**dict(zip(['domain','bus','slot','function'],['0x'+s for s in m.groups()])))
                add(hostdev,'address',type='pci',domain='0x0000',bus=f'0x{guest_bus:02x}',slot='0x00',function='0x0')
            qemu=add(domain,'qemu:commandline');add(qemu,'qemu:arg',value='-fw_cfg')
            add(qemu,'qemu:arg',value='name=opt/ovmf/X-PciMmio64Mb,string=1179648')
        xml=directory/'domain.xml';xml.write_text(ET.tostring(domain,encoding='unicode'))
        self.virsh('define',xml,'--validate')
        if count == 8 and not headless:
            self.eight_card_memory_guard(instance)
        self.virsh('start',self.name(instance),timeout=900 if count == 8 and not headless else 600 if count == 4 and not headless else 90)

    def eight_card_numa(self, instance):
        """Fail closed unless the exact two-socket mapping proven in QA is present."""
        first = self.four_card_numa({**instance, 'slot': 1, 'gpu_count': 4})
        second = self.four_card_numa({**instance, 'slot': 5, 'gpu_count': 4})
        if first != (0, '0-31,64-95') or second != (1, '32-63,96-127'):
            raise RuntimeError('eight-card CPU/GPU NUMA topology differs from acceptance')

    @staticmethod
    def eight_card_memory_guard(instance):
        line = next((line for line in Path('/proc/meminfo').read_text().splitlines()
                     if line.startswith('MemAvailable:')), None)
        if line is None:
            raise RuntimeError('host memory availability is unknown')
        available_bytes = int(line.split()[1]) * 1024
        required_bytes = instance['memory_mb'] * 1_000_000 + 32 * 1024**3
        if available_bytes < required_bytes:
            raise RuntimeError('host memory safety reserve would be breached by eight-card VM')

    def four_card_numa(self, instance):
        nodes = set()
        for slot in instance_slots(instance):
            bdf = self.config['bdfs'][slot-1]
            device = Path('/sys/bus/pci/devices') / bdf
            if sorted(p.name for p in (device/'iommu_group/devices').iterdir()) != [bdf]:
                raise RuntimeError('four-card plan requires independent IOMMU groups')
            nodes.add(int((device/'numa_node').read_text()))
        if len(nodes) != 1 or min(nodes) < 0:
            raise RuntimeError('four-card plan requires one verified NUMA group')
        node = nodes.pop()
        cpuset = (Path('/sys/devices/system/node')/f'node{node}'/'cpulist').read_text().strip()
        if not re.fullmatch(r'[0-9,-]+', cpuset):
            raise RuntimeError('invalid NUMA CPU set')
        return node, cpuset

    @staticmethod
    def mac_address(instance):
        if instance.get('mode') != 'headless':
            return f"52:54:00:ca:01:{int(instance['slot']):02x}"
        # Separate prefix from the legacy GPU pool; stable across restarts.
        ident = str(instance['id'])
        suffix = int(ident) if ident.isdigit() else int(hashlib.sha256(ident.encode()).hexdigest()[:8], 16)
        if not 0 <= suffix <= 0xffffffff:
            raise ValueError('instance ID exceeds network identity range')
        return '02:ca:' + ':'.join(f'{b:02x}' for b in suffix.to_bytes(4, 'big'))

    def healthy(self, instance):
        try:
            raw=self.virsh('qemu-agent-command',self.name(instance),json.dumps({'execute':'guest-network-get-interfaces'}))
            ip=None
            for nic in json.loads(raw).get('return',[]):
                if nic.get('hardware-address','').lower()==self.mac_address(instance):
                    for addr in nic.get('ip-addresses',[]):
                        candidate=addr.get('ip-address','')
                        if candidate.count('.') == 3 and not candidate.startswith('127.'):
                            ip = candidate
            if not ip: return None
            if instance.get('mode') == 'headless':
                with socket.create_connection((ip,22),timeout=3) as sock:
                    if not sock.recv(128).startswith(b'SSH-'): return None
                return ip if self.ensure_shared_guest(instance) else None
            raw=self.virsh('qemu-agent-command',self.name(instance),json.dumps({'execute':'guest-exec','arguments':{'path':'/usr/bin/hl-smi','arg':['-L'],'capture-output':True}}))
            pid=json.loads(raw)['return']['pid']
            for _ in range(10):
                raw=self.virsh('qemu-agent-command',self.name(instance),json.dumps({'execute':'guest-exec-status','arguments':{'pid':pid}}))
                status=json.loads(raw)['return']
                if status.get('exited'):
                    if status.get('exitcode') != 0: return None
                    output=base64.b64decode(status.get('out-data','')).decode(errors='replace')
                    count = gpu_count(instance.get('gpu_count', 1))
                    if not re.search(rf'Attached AIPs\s*:\s*{count}\b',output): return None
                    if len(re.findall(r'Module status\s*:\s*Operational',output)) != count: return None
                    if len(re.findall(r'Memory Usage[\s\S]*?Total\s*:\s*98304\s*MB',output)) != count: return None
                    with socket.create_connection((ip,22),timeout=3) as sock:
                        if not sock.recv(128).startswith(b'SSH-'): return None
                    if not self.ensure_shared_guest(instance): return None
                    return ip
                time.sleep(.5)
            return None
        except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired, OSError, ET.ParseError):
            # QEMU may be running while the guest agent/network is still booting.
            # Let the worker retry within its health deadline instead of quarantining.
            return None

    def ensure_shared_guest(self, instance):
        """Install only managed mount files; never rerun guest disk/cloud-init setup.

        Existing running VMs without a filesystem device remain healthy and
        get an explicit restart-required state. They are never hot-reconfigured.
        """
        if self.shared_config.get('enabled') is not True:
            return True
        ident = str(instance['id'])
        xml = ET.fromstring(self.virsh('dumpxml', self.name(instance)))
        attached = any(item.find('target') is not None
                       and item.find('target').get('dir') == SHARED_TAG
                       for item in xml.findall('./devices/filesystem'))
        if not attached:
            self.shared_states[ident] = 'restart_required'
            return True
        payload = {'execute': 'guest-exec', 'arguments': {
            'path': '/usr/bin/python3', 'arg': ['-c', guest_install_script()], 'capture-output': True}}
        raw = self.virsh('qemu-agent-command', self.name(instance), json.dumps(payload))
        pid = json.loads(raw)['return']['pid']
        for _ in range(60):
            raw = self.virsh('qemu-agent-command', self.name(instance), json.dumps({
                'execute': 'guest-exec-status', 'arguments': {'pid': pid}}))
            status = json.loads(raw)['return']
            if status.get('exited'):
                output = base64.b64decode(status.get('out-data', '')).decode(errors='replace')
                okay = status.get('exitcode') == 0 and 'ONECAT_SHARED_READY' in output
                self.shared_states[ident] = 'mounted' if okay else 'mount_failed'
                return okay
            time.sleep(.5)
        self.shared_states[ident] = 'mount_failed'
        return False

    def shared_status(self, instance):
        enabled = self.shared_config.get('enabled') is True
        return {'enabled': enabled, 'path': '/shared', 'readOnly': True,
                'state': self.shared_states.get(str(instance['id']), 'pending' if enabled else 'disabled')}

    def stop(self, instance):
        if self.state(instance)=='running': self.virsh('shutdown',self.name(instance))

    def force_off(self, instance):
        if self.state(instance) in ('running', 'stopping'):
            self.virsh('destroy', self.name(instance), timeout=600 if instance.get('gpu_count',1) in (4,8) else 30)

    def recovered(self, instance):
        if self.state(instance) not in ('off','absent'): return False
        if instance.get('slot') is None: return True
        try:
            for slot in instance_slots(instance):
                self.preflight(slot)
            return True
        except (RuntimeError,subprocess.TimeoutExpired): return False

    def release(self, instance):
        if self.state(instance) not in ('off','absent'): raise RuntimeError('VM is not off')
        self.shared_states.pop(str(instance['id']), None)
        if self.state(instance)=='off': self.virsh('undefine',self.name(instance),'--nvram')
        trees = [
            (self.path(instance), self.root/'retired'),
            (self.data_path(instance), self.data_root/'retired'),
        ]
        if self.system_root != self.root:
            trees.append((self.system_path(instance).parent, self.system_root/'retired'))
        for directory, trash in trees:
            if not directory.exists():
                continue
            trash.mkdir(exist_ok=True)
            target = trash / directory.name
            if target.exists():
                target = trash / f'{directory.name}-{time.time_ns()}'
            if directory.parent.resolve() not in (self.disks.resolve(), self.data_disks.resolve(), self.system_disks.resolve()):
                raise RuntimeError('unsafe instance storage path')
            directory.rename(target)
            # A released instance is promised to be deleted, not merely
            # hidden from the scheduler. The path was validated above and
            # the VM is already off, so remove only this exact retired tree.
            if target.parent.resolve() != trash.resolve() or target == trash:
                raise RuntimeError('unsafe retired storage path')
            shutil.rmtree(target)


class SimulationBackend:
    """Local QA only; never represents a real VM or public endpoint."""
    def __init__(self): self.states={}
    def ready(self): return True
    def prepare(self,instance,password): pass
    def start(self,instance): self.states[instance['id']]='running'
    def healthy(self,instance): return '127.0.0.1'
    def state(self,instance): return self.states.get(instance['id'],'off')
    def stop(self,instance): self.states[instance['id']]='off'
    def force_off(self,instance): self.stop(instance)
    def recovered(self,instance): return self.state(instance)=='off'
    def release(self,instance): self.states.pop(instance['id'],None)
