from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.homeflix_setup.envfile import EnvDocument
from scripts.homeflix_setup.cli import build_parser, main
from scripts.homeflix_setup.preflight import run_preflight
from scripts.homeflix_setup.secrets import set_vpn_secrets
from tests.test_preflight import MountRunner, configured


def run_main(*args: str, repository_root: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        return_code = main(args, repository_root=repository_root)
    return return_code, stdout.getvalue(), stderr.getvalue()


class VpnSecretHandoffTests(unittest.TestCase):
    def test_secrets_vpn_refuses_json_and_redirected_streams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "must-not-appear-as-argv-or-json"
            (root / ".env").write_text(
                "VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=openvpn\nVPN_USER=\nVPN_PASSWORD=\n",
                encoding="utf-8",
            )
            json_code, json_stdout, json_stderr = run_main(
                "--json", "secrets", "vpn", repository_root=root
            )
            pipe_code, pipe_stdout, pipe_stderr = run_main(
                "secrets", "vpn", repository_root=root
            )
            document = (root / ".env").read_text(encoding="utf-8")

        self.assertEqual(json_code, 2)
        self.assertEqual(pipe_code, 2)
        combined = json_stdout + json_stderr + pipe_stdout + pipe_stderr
        self.assertNotIn(secret, combined)
        self.assertNotIn("VPN_PASSWORD=", combined)
        self.assertNotIn(secret, document)

    def test_secrets_vpn_refuses_secret_tokens_on_argv(self) -> None:
        secret = "must-not-appear-as-argv-or-json"
        parser = build_parser()
        argv_shapes = (
            ("secrets", "vpn", secret),
            ("secrets", "vpn", "--password", secret),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            env_path.write_text(
                "VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=openvpn\nVPN_USER=\nVPN_PASSWORD=\n",
                encoding="utf-8",
            )
            for argv in argv_shapes:
                with self.subTest(argv=argv):
                    stderr = io.StringIO()
                    with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                        parser.parse_args(list(argv))
                    self.assertEqual(raised.exception.code, 2)

                    stdout = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                        main(argv, repository_root=root)
                    self.assertEqual(raised.exception.code, 2)
                    self.assertNotIn(secret, env_path.read_text(encoding="utf-8"))

            document = env_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, document)
        self.assertIn("VPN_PASSWORD=\n", document)

    def test_supported_openvpn_secrets_confirm_and_update_env_without_leaking_values(self) -> None:
        username = "proton-openvpn-user+pmp"
        password = "proton-openvpn-secret"
        prompts: list[tuple[str, bool]] = []

        def reader(prompt: str, *, confirm: bool = False) -> str:
            prompts.append((prompt, confirm))
            if "user" in prompt.casefold():
                return username
            if "password" in prompt.casefold():
                return password
            raise AssertionError(f"unexpected prompt {prompt!r}")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=openvpn\nVPN_USER=\nVPN_PASSWORD=\nOTHER=keep\n",
                encoding="utf-8",
            )
            result = set_vpn_secrets(path, reader=reader)
            document = EnvDocument.load(path)
            mode = path.stat().st_mode & 0o777

        self.assertEqual(mode, 0o600)
        self.assertEqual(document.get("VPN_USER"), username)
        self.assertEqual(document.get("VPN_PASSWORD"), password)
        self.assertEqual(document.get("OTHER"), "keep")
        self.assertEqual([confirm for _prompt, confirm in prompts], [True, True])
        rendered = repr(result)
        self.assertNotIn(username, rendered)
        self.assertNotIn(password, rendered)
        names = [item["name"] for item in result["keys"]]
        self.assertEqual(names, ["VPN_USER", "VPN_PASSWORD"])
        self.assertTrue(all(item["status"] == "updated" and item["secret"] for item in result["keys"]))

    def test_unsupported_provider_refuses_guessed_keys_and_points_at_gluetun_docs(self) -> None:
        guessed = "guessed-nord-token"

        def reader(prompt: str, *, confirm: bool = False) -> str:
            raise AssertionError("unsupported providers must not prompt for guessed keys")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "VPN_SERVICE_PROVIDER=nordvpn\nVPN_TYPE=openvpn\nVPN_USER=\nVPN_PASSWORD=\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "gluetun-wiki"):
                set_vpn_secrets(path, reader=reader)
            with self.assertRaisesRegex(ValueError, "gluetun-wiki"):
                set_vpn_secrets(path, provider="custom", vpn_type="wireguard", reader=reader)
            document = path.read_text(encoding="utf-8")
        self.assertNotIn(guessed, document)
        self.assertIn("VPN_USER=\n", document)

    def test_read_from_tty_refuses_missing_or_non_tty_and_does_not_take_stdin(self) -> None:
        from scripts.homeflix_setup.secrets import read_from_tty

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent-tty"
            with self.assertRaisesRegex(RuntimeError, "controlling terminal"):
                read_from_tty("VPN password: ", tty_path=str(missing))
            regular = Path(directory) / "regular"
            regular.write_text("stdin-secret\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "controlling terminal"):
                read_from_tty("VPN password: ", tty_path=str(regular))
            self.assertEqual(regular.read_text(encoding="utf-8"), "stdin-secret\n")

    def test_confirmation_mismatch_does_not_write_secrets(self) -> None:
        from scripts.homeflix_setup.secrets import read_from_tty

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("VPN_SERVICE_PROVIDER=protonvpn\nVPN_TYPE=openvpn\nVPN_USER=\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "did not match"):
                set_vpn_secrets(
                    path,
                    reader=lambda prompt, confirm=False: (_ for _ in ()).throw(ValueError("entered values did not match")),
                )
            self.assertIn("VPN_USER=\n", path.read_text(encoding="utf-8"))
            with patch("scripts.homeflix_setup.secrets.getpass.getpass", side_effect=["one", "two"]), patch(
                "scripts.homeflix_setup.secrets.os.open", return_value=41
            ), patch("scripts.homeflix_setup.secrets.os.isatty", return_value=True), patch(
                "scripts.homeflix_setup.secrets.os.fdopen"
            ) as fdopen, patch("scripts.homeflix_setup.secrets.os.close"):
                terminal = MagicMock()
                terminal.__enter__.return_value = terminal
                fdopen.return_value = terminal
                with self.assertRaisesRegex(ValueError, "did not match"):
                    read_from_tty("VPN password: ", confirm=True)


class AcquisitionCredentialIsolationTests(unittest.TestCase):
    def test_unsupported_or_invalid_credentials_fail_acquisition_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = configured(root)
            config["VPN_SERVICE_PROVIDER"] = "nordvpn"
            config["VPN_TYPE"] = "openvpn"
            config["VPN_USER"] = "set"
            config["VPN_PASSWORD"] = "set"
            runner = MountRunner(Path(config["DATA_ROOT"]))
            core = run_preflight(config, "core", runner)
            acquisition = run_preflight(config, "acquisition", runner)
            status_code, status_stdout, status_stderr = run_main(
                "--json", "status", repository_root=root
            )

        self.assertTrue(core.passed)
        self.assertFalse(acquisition.passed)
        provider = next(result for result in acquisition.results if result.name == "vpn_provider")
        self.assertEqual(provider.status, "fail")
        self.assertIn("gluetun-wiki", provider.message)
        self.assertNotIn("set", repr(acquisition.results))
        self.assertEqual(status_code, 0, status_stderr)
        self.assertNotIn("nordvpn", status_stdout + status_stderr)
        self.assertNotIn("VPN_PASSWORD", status_stdout + status_stderr)

    def test_whitespace_credentials_fail_acquisition_and_warn_for_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = configured(Path(directory))
            config["VPN_SERVICE_PROVIDER"] = "protonvpn"
            config["VPN_TYPE"] = "openvpn"
            config["VPN_USER"] = "   "
            config["VPN_PASSWORD"] = "\t"
            runner = MountRunner(Path(config["DATA_ROOT"]))
            core = run_preflight(config, "core", runner)
            acquisition = run_preflight(config, "acquisition", runner)
        self.assertTrue(core.passed)
        self.assertGreaterEqual(core.counts["warn"], 2)
        self.assertFalse(acquisition.passed)
        self.assertGreaterEqual(acquisition.counts["fail"], 2)
