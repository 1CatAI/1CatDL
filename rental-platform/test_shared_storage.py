import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from backend import LibvirtBackend
from shared_storage import TAG, guest_install_script, validate_export


class SharedExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        mount = root / 'raid'; mount.mkdir()
        source = mount / 'public'; source.mkdir()
        export = root / 'export'; export.mkdir()
        binary = root / 'virtiofsd'; binary.write_text('test')
        self.config = {'enabled': True, 'mount': str(mount), 'source': str(source),
                       'export': str(export), 'uuid': 'test-raid-uuid', 'binary': str(binary)}
        self.rows = [dict(uuid='test-raid-uuid', fstype='ext4', target=str(mount), options='rw'),
                     dict(uuid='test-raid-uuid', fstype='ext4', target=str(export),
                          fsroot='/public', options='ro,nodev,nosuid')]

    def test_valid_host_readonly_bind(self):
        with patch('shared_storage.find_mount', side_effect=self.rows):
            self.assertEqual(validate_export(self.config), self.config['export'])

    def test_wrong_uuid_rejected(self):
        self.rows[1]['uuid'] = 'root-disk'
        with patch('shared_storage.find_mount', side_effect=self.rows), self.assertRaisesRegex(RuntimeError, 'identity'):
            validate_export(self.config)

    def test_rw_bind_rejected_even_with_ro_guest_mount(self):
        self.rows[1]['options'] = 'rw,nodev,nosuid'
        with patch('shared_storage.find_mount', side_effect=self.rows), self.assertRaisesRegex(RuntimeError, 'read-only'):
            validate_export(self.config)

    def test_wrong_subdirectory_rejected(self):
        self.rows[1]['fsroot'] = '/'
        with patch('shared_storage.find_mount', side_effect=self.rows), self.assertRaises(RuntimeError):
            validate_export(self.config)

    def test_disabled_has_no_host_probes(self):
        with patch('shared_storage.find_mount') as probe:
            self.assertIsNone(validate_export({}))
            probe.assert_not_called()

    def test_guest_installer_never_initializes_disks(self):
        script = guest_install_script()
        compile(script, '<guest-installer>', 'exec')
        self.assertNotIn('cloud-init', script)
        self.assertNotIn('mkfs', script)
        self.assertIn("temp.open('x')", script)
        self.assertIn('ONECAT_SHARED_READY', script)


class SharedBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        runtime = root / 'runtime'; runtime.mkdir()
        data = root / 'data'; data.mkdir()
        image = root / 'image.qcow2'; image.write_bytes(b'test')
        image.with_suffix('.manifest.json').write_text('{"validated":true}')
        self.backend = LibvirtBackend(runtime, {'enabled': True, 'image': str(image), 'data_root': str(data),
            'network': '1cat-rental', 'bdfs': ['0000:19:00.0'], 'enforce_separate_data_device': False,
            'shared_storage': {'enabled': True, 'binary': '/usr/libexec/virtiofsd'}})
        self.instance = {'id': '1', 'slot': 1, 'vcpu': 16, 'memory_mb': 62500, 'data_disk': 0}
        directory = self.backend.path(self.instance); directory.mkdir()
        for name in ('seed.iso', 'VARS.fd', 'system.qcow2'):
            (directory / name).write_bytes(b'test')

    def test_xml_adds_shared_memory_without_changing_gaudi_mapping(self):
        with patch.object(self.backend, 'state', return_value='off'), patch.object(self.backend, 'preflight'), \
             patch('backend.validate_export', return_value='/srv/1cat-public-ro'), \
             patch.object(self.backend, 'virsh', return_value='known-uuid'):
            self.backend.start(self.instance)
        xml = ET.parse(self.backend.path(self.instance) / 'domain.xml').getroot()
        self.assertEqual(xml.find('./memoryBacking/source').get('type'), 'memfd')
        self.assertEqual(xml.find('./memoryBacking/access').get('mode'), 'shared')
        self.assertEqual(xml.find('./devices/filesystem/target').get('dir'), TAG)
        self.assertIsNone(xml.find('./devices/filesystem/readonly'))  # not supported by installed versions
        self.assertEqual(xml.find('./cpu/maxphysaddr').get('bits'), '42')
        self.assertEqual(xml.find('./devices/hostdev/address').get('bus'), '0x05')
        self.assertEqual(xml.find('./memory').text, '62500')

    def test_unsafe_export_blocks_before_gpu_detach(self):
        with patch.object(self.backend, 'state', return_value='off'), \
             patch('backend.validate_export', side_effect=RuntimeError('unsafe')), \
             patch.object(self.backend, 'preflight') as preflight, \
             patch.object(self.backend, 'virsh') as virsh:
            with self.assertRaises(RuntimeError): self.backend.start(self.instance)
            preflight.assert_not_called(); virsh.assert_not_called()

    def test_old_running_vm_not_modified_or_failed(self):
        with patch.object(self.backend, 'virsh', return_value='<domain><devices/></domain>') as virsh:
            self.assertTrue(self.backend.ensure_shared_guest(self.instance))
            self.assertEqual(virsh.call_count, 1)
            self.assertEqual(virsh.call_args.args[0], 'dumpxml')
        self.assertEqual(self.backend.shared_status(self.instance)['state'], 'restart_required')

    def test_guest_setup_result_is_required(self):
        xml = f'<domain><devices><filesystem><target dir="{TAG}"/></filesystem></devices></domain>'
        responses = [xml, json.dumps({'return': {'pid': 1}}), json.dumps({'return': {'exited': True, 'exitcode': 0,
                      'out-data': base64.b64encode(b'ONECAT_SHARED_READY').decode()}})]
        with patch.object(self.backend, 'virsh', side_effect=responses):
            self.assertTrue(self.backend.ensure_shared_guest(self.instance))
        self.assertEqual(self.backend.shared_status(self.instance)['state'], 'mounted')

    def test_failed_mount_never_reported_ready(self):
        xml = f'<domain><devices><filesystem><target dir="{TAG}"/></filesystem></devices></domain>'
        with patch.object(self.backend, 'virsh', side_effect=[xml, '{"return":{"pid":1}}',
                         '{"return":{"exited":true,"exitcode":1}}']):
            self.assertFalse(self.backend.ensure_shared_guest(self.instance))
        self.assertEqual(self.backend.shared_status(self.instance)['state'], 'mount_failed')


if __name__ == '__main__':
    unittest.main()
