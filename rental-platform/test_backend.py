import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from backend import LibvirtBackend


class BackendStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        image = root / "gaudi.qcow2"
        image.write_bytes(b"qa")
        image.with_suffix(".manifest.json").write_text(json.dumps({"validated": True}))
        self.data_tmp = tempfile.TemporaryDirectory()
        data_root = Path(self.data_tmp.name)
        self.data_root = data_root
        self.backend = LibvirtBackend(root, {"enabled": True, "image": str(image), "network": "1cat-rental", "bdfs": ["0000:19:00.0"], "data_root": str(data_root), "enforce_separate_data_device": False})

    def tearDown(self):
        self.tmp.cleanup()
        self.data_tmp.cleanup()

    def test_existing_data_disk_instance_must_have_data_overlay(self):
        directory = self.backend.disks / "1"
        directory.mkdir()
        for name in ("system.qcow2", "seed.iso", "VARS.fd"):
            (directory / name).write_bytes(b"qa")
        instance = {"id": "1", "data_disk": 100}
        with self.assertRaisesRegex(RuntimeError, "data.qcow2@data_root"):
            self.backend.prepare(instance, "unused-password")
        data_directory = self.backend.data_disks / "1"
        data_directory.mkdir()
        (data_directory / "data.qcow2").write_bytes(b"qa")
        self.backend.prepare(instance, "unused-password")

    def test_system_only_instance_does_not_require_data_overlay(self):
        directory = self.backend.disks / "2"
        directory.mkdir()
        for name in ("system.qcow2", "seed.iso", "VARS.fd"):
            (directory / name).write_bytes(b"qa")
        self.backend.prepare({"id": "2", "data_disk": 0}, "unused-password")

    def test_gift_enlargement_is_not_recreated_or_shrunk_on_prepare(self):
        instance = {'id': '57', 'data_disk': 200}
        directory = self.backend.disks / '57'
        directory.mkdir()
        for name in ('system.qcow2', 'seed.iso', 'VARS.fd'):
            (directory / name).write_bytes(b'existing')
        data = self.backend.data_path(instance)
        data.mkdir()
        image = data / 'data.qcow2'
        original = b'existing enlarged image with customer data'
        image.write_bytes(original)
        with patch('backend.command') as commands:
            self.backend.prepare(instance, 'unused-test-password')
        self.assertEqual(image.read_bytes(), original)
        self.assertFalse(any(call.args[0][0] == 'qemu-img' for call in commands.call_args_list))

    def test_release_removes_exact_instance_storage(self):
        directory = self.backend.disks / "3"
        directory.mkdir()
        (directory / "system.qcow2").write_bytes(b"qa")
        data_directory = self.backend.data_disks / "3"
        data_directory.mkdir()
        (data_directory / "data.qcow2").write_bytes(b"qa")
        with patch.object(self.backend, "state", return_value="absent"):
            self.backend.release({"id": "3", "slot": None})
        self.assertFalse(directory.exists())
        self.assertFalse(data_directory.exists())
        self.assertFalse((self.backend.root / "retired" / "3").exists())
        self.assertFalse((self.backend.data_root / "retired" / "3").exists())

    def test_new_guest_has_gaudi_device_groups(self):
        with patch("backend.command"), patch("backend.shutil.copy2"), \
             patch("backend.subprocess.run", return_value=SimpleNamespace(stdout="$6$test-hash\n")):
            self.backend.prepare({"id": "4", "data_disk": 0}, "test-only-password")
        payload = (self.backend.disks / "4" / "user-data").read_text()
        user_data = json.loads(payload.removeprefix("#cloud-config\n"))
        self.assertFalse(self.backend.data_path({"id": "4"}).exists())
        self.assertEqual(user_data["users"][0]["groups"], ["render", "video"])
        self.assertEqual(user_data["users"][0]["hashed_passwd"], "$6$test-hash")
        self.assertNotIn("passwd", user_data["users"][0])
        self.assertEqual(user_data["chpasswd"], {"expire": False})
        self.assertNotIn("test-only-password", payload)
        self.assertIn("usermod -aG render,video gpu", str(user_data["runcmd"]))

    def test_domain_uses_exact_decimal_memory_and_intel_data_overlay(self):
        directory = self.backend.disks / "5"
        directory.mkdir()
        for name in ("system.qcow2", "seed.iso", "VARS.fd"):
            (directory / name).write_bytes(b"qa")
        data_directory = self.backend.data_disks / "5"
        data_directory.mkdir()
        data_overlay = data_directory / "data.qcow2"
        data_overlay.write_bytes(b"qa")
        instance = {"id": "5", "slot": 1, "vcpu": 16, "memory_mb": 62500, "data_disk": 10}
        calls = []

        def virsh(*args, **kwargs):
            calls.append(args)
            if args[0] == "domuuid":
                raise RuntimeError("not defined")
            return ""

        with patch.object(self.backend, "state", return_value="off"), \
             patch.object(self.backend, "preflight"), \
             patch.object(self.backend, "virsh", side_effect=virsh):
            self.backend.start(instance)
        xml = (directory / "domain.xml").read_text()
        self.assertIn('<memory unit="MB">62500</memory>', xml)
        self.assertIn(f'<source file="{data_overlay}"', xml)
        self.assertNotIn(str(directory / "data.qcow2"), xml)
        self.assertTrue(any(call[0] == "define" for call in calls))

    def test_ready_gate_rejects_data_root_on_system_filesystem(self):
        config = dict(self.backend.config)
        config["enforce_separate_data_device"] = True
        same_device = LibvirtBackend(self.backend.root, config)
        self.assertFalse(same_device.ready())


if __name__ == "__main__":
    unittest.main()
