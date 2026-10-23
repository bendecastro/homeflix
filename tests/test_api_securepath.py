from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.homeflix_setup.api.seerr import read_settings_api_key
from scripts.homeflix_setup.api.securepath import read_config_file
from scripts.homeflix_setup.core import prepare_seerr_config


class SeerrOwnershipTests(unittest.TestCase):
    def test_extra_uid_is_confined_to_seerr_not_config_root_or_other_services(self):
        key = "FIXTURE_API_KEY_1234567890ABCDE"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = root / "seerr"
            service.mkdir()
            settings = service / "settings.json"
            settings.write_text(json.dumps({"main": {"apiKey": key}}))
            actual_fstat = os.fstat
            inodes = [path.stat().st_ino for path in (root, service, settings)]
            for puid, owners, accepted in (
                (1000, (1000, 1000, 1000), True),
                (1001, (1001, 1000, 1000), True),
                (1001, (1001, 1002, 1000), False),
                (1001, (1001, 1000, 1002), False),
                (1001, (1000, 1000, 1000), False),
            ):
                with self.subTest(puid=puid, owners=owners):
                    def metadata(fd):
                        result = actual_fstat(fd)
                        fields = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
                        if result.st_ino in inodes:
                            fields["st_uid"] = owners[inodes.index(result.st_ino)]
                        return SimpleNamespace(**fields)
                    with patch("scripts.homeflix_setup.api.securepath.os.fstat", side_effect=metadata):
                        if accepted:
                            self.assertEqual(read_settings_api_key(root, puid), key)
                        else:
                            with self.assertRaises(ValueError):
                                read_settings_api_key(root, puid)
                        if puid == 1001:
                            with self.assertRaises(ValueError):
                                read_config_file(root, ("seerr", "settings.json"), puid)

    def test_prepare_creates_directory_and_sets_only_its_fixed_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("scripts.homeflix_setup.core.os.fchown") as chown:
                prepare_seerr_config(directory)
                self.assertTrue((Path(directory) / "seerr").is_dir())
                if (os.getuid(), os.getgid()) != (1000, 1000):
                    self.assertEqual(chown.call_args.args[1:], (1000, 1000))
                else:
                    chown.assert_not_called()

    def test_prepare_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "seerr").symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                prepare_seerr_config(root)
