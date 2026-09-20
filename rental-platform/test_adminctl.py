import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adminctl


class AdminCtlTests(unittest.TestCase):
    def test_environment_root_resolves_to_service_state_directory(self):
        with patch.dict(os.environ, {"RENTAL_ROOT": "/var/lib/1cat-rental", "RENTAL_STATE_ROOT": ""}, clear=False):
            parser = adminctl.build_parser()
            args = parser.parse_args(["create-admin", "--name", "opsadmin"])
        self.assertEqual(Path(args.root).name, "state")
        self.assertTrue(args.root.replace("\\", "/").endswith("/var/lib/1cat-rental/state"))

    def test_create_admin_bootstraps_empty_state_and_refuses_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(tmp) / "state")
            argv = ["adminctl.py", "--root", root, "create-admin", "--name", "opsadmin"]
            with patch.object(sys, "argv", argv), patch.object(
                adminctl.getpass, "getpass", side_effect=["strong-admin-password", "strong-admin-password"]
            ):
                self.assertEqual(adminctl.main(), 0)
            with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                adminctl.main()


if __name__ == "__main__":
    unittest.main()
