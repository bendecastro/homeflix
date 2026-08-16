from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.homeflix_setup.api import ApiError

from scripts.homeflix_setup.cli import main
from scripts.homeflix_setup.preflight import CheckResult, PreflightReport
from tests.helpers import REPOSITORY_ROOT, parse_single_json, run_cli


def run_main(*args: str, repository_root: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        return_code = main(args, repository_root=repository_root)
    return return_code, stdout.getvalue(), stderr.getvalue()


class StatusCliTests(unittest.TestCase):
    def test_json_status_is_one_object_with_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository_root = Path(directory)
            return_code, stdout, stderr = run_main(
                "--json", "status", repository_root=repository_root
            )

            self.assertEqual(return_code, 0, stderr)
            status = parse_single_json(stdout)
            self.assertEqual(status["schema_version"], 1)
            self.assertFalse(status["state_exists"])
            self.assertFalse((repository_root / ".homeflix" / "setup.json").exists())

    def test_status_never_reads_or_emits_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "ENV_SECRET_MUST_NOT_APPEAR"
            (root / ".env").write_text(f"JELLYFIN_ADMIN_PASSWORD={secret}\nDATA_ROOT=/private/root\n", encoding="utf-8")
            code, stdout, stderr = run_main("--json", "status", repository_root=root)
        self.assertEqual(code, 0)
        self.assertNotIn(secret, stdout + stderr)
        self.assertNotIn("/private/root", stdout + stderr)

    def test_setup_core_dry_run_has_ordered_core_only_plan_and_no_calls_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.homeflix_setup.cli.discover_host", side_effect=AssertionError("must not discover")), patch("scripts.homeflix_setup.cli.configure", side_effect=AssertionError("must not configure")), patch("scripts.homeflix_setup.cli.run_preflight", side_effect=AssertionError("must not preflight")), patch("scripts.homeflix_setup.cli.deploy_core", side_effect=AssertionError("must not deploy")), patch("scripts.homeflix_setup.cli.configure_core", side_effect=AssertionError("must not initialize")), patch("scripts.homeflix_setup.cli.verify_core", side_effect=AssertionError("must not verify")):
                code, stdout, stderr = run_main("--json", "setup", "core", "--dry-run", repository_root=root)
            payload = parse_single_json(stdout)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(payload["phases"], ["configure", "preflight:core", "deploy:core", "initialize:core", "verify:core"])
            self.assertEqual(payload["acquisition_mutations"], [])
            self.assertFalse(payload["state_written"])
            self.assertEqual(list(root.iterdir()), [])
            rendered = json.dumps(payload).casefold()
            for forbidden in ("gluetun", "qbittorrent", "nzbget", "prowlarr"):
                self.assertNotIn(forbidden, rendered)

    def test_setup_core_reconfigures_fresh_and_resumed_in_order(self) -> None:
        for resumed in (False, True):
            with self.subTest(resumed=resumed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                roots = ("/fixture/data", "/fixture/config", "/fixture/cache")
                if resumed:
                    (root / ".env").write_text(
                        "DATA_ROOT=/fixture/data\nCONFIG_ROOT=/fixture/config\nCACHE_ROOT=/fixture/cache\n"
                        "QUALITY_PROFILE=Fixture HD\nJELLYFIN_ADMIN_PASSWORD=PRESERVE_ME\n",
                        encoding="utf-8",
                    )
                    (root / ".env").chmod(0o600)
                calls = []
                def configure_fixture(repository_root, facts, **kwargs):
                    calls.append(("configure", kwargs))
                    if not (root / ".env").exists():
                        (root / ".env").write_text(
                            "DATA_ROOT=/fixture/data\nCONFIG_ROOT=/fixture/config\nCACHE_ROOT=/fixture/cache\nQUALITY_PROFILE=Fixture HD\n",
                            encoding="utf-8",
                        )
                        (root / ".env").chmod(0o600)
                    return {}
                def preflight_fixture(*args, **kwargs): calls.append(("preflight", {})); return PreflightReport("core", (CheckResult("fixture", "pass", "passed"),))
                def deploy_fixture(*args, **kwargs):
                    calls.append(("deploy", {}))
                    if resumed:
                        return {"status": "checkpoint_failed", "services": [{"ready": True} for _ in range(5)]}
                    return {"status": "already_ready"}
                def initialize_fixture(*args, **kwargs): calls.append(("initialize", {})); return {"status": "configured"}
                def verify_fixture(*args, **kwargs): calls.append(("verify", {})); return {"status": "verified", "passed": True, "checks": []}
                argv = ["--json", "setup", "core"]
                if not resumed:
                    argv += ["--data-root", roots[0], "--config-root", roots[1], "--cache-root", roots[2], "--quality-profile", "Fixture HD"]
                with patch("scripts.homeflix_setup.cli.discover_host", return_value=object()), patch("scripts.homeflix_setup.cli.configure", side_effect=configure_fixture), patch("scripts.homeflix_setup.cli.run_preflight", side_effect=preflight_fixture), patch("scripts.homeflix_setup.cli.deploy_core", side_effect=deploy_fixture), patch("scripts.homeflix_setup.cli.configure_core", side_effect=initialize_fixture), patch("scripts.homeflix_setup.cli.verify_core", side_effect=verify_fixture):
                    code, stdout, stderr = run_main(*argv, repository_root=root)
                self.assertEqual(code, 0, stderr)
                self.assertEqual([name for name, _ in calls], ["configure", "preflight", "deploy", "initialize", "verify"])
                self.assertEqual(tuple(calls[0][1][name] for name in ("data_root", "config_root", "cache_root")), roots)
                self.assertNotIn("PRESERVE_ME", stdout + stderr)

    def test_setup_core_uses_one_deadline_and_skips_after_configuration_exhausts_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [0.0]
            def configure_fixture(*args, **kwargs):
                (root / ".env").write_text("DATA_ROOT=/d\nCONFIG_ROOT=/c\nCACHE_ROOT=/k\n", encoding="utf-8")
                (root / ".env").chmod(0o600)
                now[0] = 90.0
                return {}
            with patch("scripts.homeflix_setup.cli.time.monotonic", side_effect=lambda: now[0]), patch("scripts.homeflix_setup.cli.discover_host", return_value=object()), patch("scripts.homeflix_setup.cli.configure", side_effect=configure_fixture), patch("scripts.homeflix_setup.cli.run_preflight", side_effect=AssertionError("preflight must be skipped")):
                code, stdout, stderr = run_main("--json", "setup", "core", "--data-root", "/d", "--config-root", "/c", "--cache-root", "/k", repository_root=root)
        payload = parse_single_json(stdout)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(payload["status"], "timeout")
        self.assertEqual([phase["status"] for phase in payload["phases"]], ["fail", "skipped", "skipped", "skipped", "skipped"])

    def test_setup_maps_only_outer_deadline_api_error_to_timeout(self) -> None:
        for error_code, expected in (("deadline_exhausted", "timeout"), ("transport_error", "initialization_failed")):
            with self.subTest(error_code=error_code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".env").write_text("DATA_ROOT=/d\nCONFIG_ROOT=/c\nCACHE_ROOT=/k\nQUALITY_PROFILE=HD-1080p\n", encoding="utf-8")
                (root / ".env").chmod(0o600)
                preflight = PreflightReport("core", (CheckResult("fixture", "pass", "passed"),))
                with patch("scripts.homeflix_setup.cli.discover_host", return_value=object()), patch("scripts.homeflix_setup.cli.configure", return_value={}), patch("scripts.homeflix_setup.cli.run_preflight", return_value=preflight), patch("scripts.homeflix_setup.cli.deploy_core", return_value={"status": "already_ready"}), patch("scripts.homeflix_setup.cli.configure_core", side_effect=ApiError("jellyfin", "initialize", None, error_code)):
                    code, stdout, stderr = run_main("--json", "setup", "core", repository_root=root)
                payload = parse_single_json(stdout)
                self.assertEqual(code, 1); self.assertEqual(stderr, "")
                self.assertEqual(payload["status"], expected)
                self.assertEqual(payload["phases"][-1]["status"], "skipped")

    def test_setup_core_phase_failures_are_truthful_and_skip_later_work(self) -> None:
        cases = (
            ("configure", "configuration_failed", ["fail", "skipped", "skipped", "skipped", "skipped"]),
            ("preflight", "preflight_failed", ["complete", "fail", "skipped", "skipped", "skipped"]),
            ("deploy", "deployment_failed", ["complete", "pass", "fail", "skipped", "skipped"]),
            ("initialize", "initialization_failed", ["complete", "pass", "complete", "fail", "skipped"]),
            ("verify", "verification_failed", ["complete", "pass", "complete", "complete", "fail"]),
        )
        for failing, expected_status, expected_phases in cases:
            with self.subTest(failing=failing), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".env").write_text("DATA_ROOT=/d\nCONFIG_ROOT=/c\nCACHE_ROOT=/k\nQUALITY_PROFILE=HD-1080p\n", encoding="utf-8")
                (root / ".env").chmod(0o600)
                configure_effect = ValueError("private") if failing == "configure" else {}
                preflight_result = PreflightReport("core", (CheckResult("fixture", "fail" if failing == "preflight" else "pass", "fixture"),))
                deploy_result = {"status": "partial_failure" if failing == "deploy" else "already_ready"}
                initialize_effect = ValueError("private") if failing == "initialize" else {"status": "configured"}
                verify_result = {"status": "failed", "passed": False, "checks": []} if failing == "verify" else {"status": "verified", "passed": True, "checks": []}
                with patch("scripts.homeflix_setup.cli.discover_host", return_value=object()), patch("scripts.homeflix_setup.cli.configure", side_effect=configure_effect if isinstance(configure_effect, Exception) else None, return_value={} if not isinstance(configure_effect, Exception) else None), patch("scripts.homeflix_setup.cli.run_preflight", return_value=preflight_result), patch("scripts.homeflix_setup.cli.deploy_core", return_value=deploy_result), patch("scripts.homeflix_setup.cli.configure_core", side_effect=initialize_effect if isinstance(initialize_effect, Exception) else None, return_value={} if not isinstance(initialize_effect, Exception) else None), patch("scripts.homeflix_setup.cli.verify_core", return_value=verify_result):
                    code, stdout, stderr = run_main("--json", "setup", "core", repository_root=root)
                payload = parse_single_json(stdout)
                self.assertEqual(code, 1)
                self.assertEqual(stderr, "")
                self.assertEqual(payload["status"], expected_status)
                self.assertEqual([phase["status"] for phase in payload["phases"]], expected_phases)
                self.assertNotIn("private", stdout)

    def test_json_status_reports_corrupt_and_future_state_as_one_error_object(self) -> None:
        invalid_contents = (
            "not json",
            json.dumps({"schema_version": 2, "checkpoints": {}, "host_facts": {}}),
        )
        for contents in invalid_contents:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                repository_root = Path(directory)
                state_path = repository_root / ".homeflix" / "setup.json"
                state_path.parent.mkdir()
                state_path.write_text(contents, encoding="utf-8")

                return_code, stdout, stderr = run_main(
                    "--json", "status", repository_root=repository_root
                )

                self.assertEqual(return_code, 1)
                self.assertEqual(stderr, "")
                error = parse_single_json(stdout)
                self.assertEqual(error["error"]["code"], "invalid_state")
                self.assertIsInstance(error["error"]["message"], str)

    def test_text_status_reports_invalid_state_concisely_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository_root = Path(directory)
            state_path = repository_root / ".homeflix" / "setup.json"
            state_path.parent.mkdir()
            state_path.write_text("not json", encoding="utf-8")

            return_code, stdout, stderr = run_main("status", repository_root=repository_root)

        self.assertEqual(return_code, 1)
        self.assertEqual(stdout, "")
        self.assertRegex(stderr, r"^homeflix: invalid setup state: .+\n$")
        self.assertNotIn("Traceback", stderr)

    def test_secret_reveal_refuses_json_and_redirected_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "must-not-appear"
            (root / ".env").write_text(
                f"JELLYFIN_ADMIN_USER=admin\nJELLYFIN_ADMIN_PASSWORD={secret}\n",
                encoding="utf-8",
            )
            json_code, json_stdout, json_stderr = run_main(
                "--json", "secrets", "reveal", "jellyfin", repository_root=root
            )
            pipe_code, pipe_stdout, pipe_stderr = run_main(
                "secrets", "reveal", "jellyfin", repository_root=root
            )
        self.assertEqual(json_code, 2)
        self.assertEqual(pipe_code, 2)
        self.assertNotIn(secret, json_stdout + json_stderr + pipe_stdout + pipe_stderr)

    def test_invalid_domain_is_structured_for_all_discovery_entry_points(self) -> None:
        commands = (
            ("discover",),
            ("host", "prepare"),
            ("configure", "--data-root", "/fixture/data", "--config-root", "/fixture/config", "--cache-root", "/fixture/cache"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("DOMAIN='invalid domain'\n", encoding="utf-8")
            for command in commands:
                with self.subTest(command=command, output="text"):
                    code, stdout, stderr = run_main(*command, repository_root=root)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(stdout, "")
                    self.assertRegex(stderr, r"^homeflix: .+: LAN DNS domain is invalid\n$")
                    self.assertNotIn("Traceback", stderr)
                with self.subTest(command=command, output="json"):
                    code, stdout, stderr = run_main("--json", *command, repository_root=root)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(stderr, "")
                    error = parse_single_json(stdout)
                    self.assertEqual(set(error), {"error"})
                    self.assertIn(error["error"]["code"], {"discovery_refused", "configuration_refused"})
                    self.assertEqual(error["error"]["message"], "LAN DNS domain is invalid")

    def test_initialize_core_renders_structured_secret_free_results_and_errors(self) -> None:
        result = {
            "status": "configured",
            "jellyfin": {"administrator_created": True, "libraries": ["Movies", "Shows", "Music"]},
            "radarr": {"profile": "Fixture HD", "root": "/data/media/movies", "targeted_connection_changed": True, "refresh_connection_changed": True},
            "sonarr": {"profile": "Fixture HD", "root": "/data/media/tv", "targeted_connection_changed": False, "refresh_connection_changed": False},
            "jellyseerr": {"initialized": True},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.homeflix_setup.cli.configure_core", return_value=result):
                code, stdout, stderr = run_main("--json", "initialize", "core", repository_root=root)
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "configured")
            self.assertTrue(payload["radarr"]["targeted_connection_changed"])
            self.assertTrue(payload["radarr"]["refresh_connection_changed"])
            self.assertNotIn("apiKey", stdout)
            self.assertNotIn("X-Emby-Token", stdout)
            self.assertNotIn("JELLYFIN", stdout)
            for error_code in ("profile_not_found", "transport_error", "deadline_exhausted"):
                with self.subTest(error_code=error_code), patch("scripts.homeflix_setup.cli.configure_core", side_effect=ApiError("radarr", "initialize", None, error_code)):
                    code, stdout, stderr = run_main("--json", "initialize", "core", repository_root=root)
                self.assertEqual(code, 1)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout)["error"]["code"], error_code)

    def test_launcher_works_cross_cwd_without_changing_repository_state(self) -> None:
        state_path = REPOSITORY_ROOT / ".homeflix" / "setup.json"
        existed_before = state_path.exists()
        contents_before = state_path.read_bytes() if existed_before else None

        with tempfile.TemporaryDirectory() as directory:
            result = run_cli("--json", "status", cwd=Path(directory))

        self.assertIn(result.returncode, (0, 1), result.stderr)
        parse_single_json(result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(state_path.exists(), existed_before)
        if existed_before:
            self.assertEqual(state_path.read_bytes(), contents_before)


class VerifyContractCliTests(unittest.TestCase):
    def test_json_verify_contract_is_one_object_and_does_not_steal_verify_core(self) -> None:
        code, stdout, stderr = run_main("--json", "verify", "contract", repository_root=REPOSITORY_ROOT)
        self.assertEqual(code, 0, stderr + stdout)
        payload = parse_single_json(stdout)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["findings"], [])
        self.assertNotIn("checks", payload)

    def test_verify_contract_does_not_write_compose(self) -> None:
        compose = REPOSITORY_ROOT / "docker-compose.yml"
        before = compose.read_bytes()
        code, stdout, stderr = run_main("--json", "verify", "contract", repository_root=REPOSITORY_ROOT)
        self.assertEqual(code, 0, stderr + stdout)
        self.assertEqual(compose.read_bytes(), before)

    def test_verify_contract_output_stays_secret_free_for_example_and_fake_env(self) -> None:
        code, stdout, stderr = run_main("--json", "verify", "contract", repository_root=REPOSITORY_ROOT)
        self.assertEqual(code, 0, stderr)
        combined = stdout + stderr
        for name in ("VPN_PASSWORD", "OPENVPN_PASSWORD", "JELLYFIN_ADMIN_PASSWORD"):
            self.assertNotIn(name, combined)

        secrets = {
            "VPN_PASSWORD": "vpn-secret-value",
            "OPENVPN_PASSWORD": "openvpn-secret-value",
            "JELLYFIN_ADMIN_PASSWORD": "jellyfin-secret-value",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docker-compose.yml").write_bytes((REPOSITORY_ROOT / "docker-compose.yml").read_bytes())
            env = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
            for key, value in secrets.items():
                env = env.replace(f"{key}=", f"{key}={value}", 1)
            (root / ".env").write_text(env, encoding="utf-8")
            code, stdout, stderr = run_main("--json", "verify", "contract", repository_root=root)
        self.assertEqual(code, 0, stderr + stdout)
        combined = stdout + stderr
        for value in secrets.values():
            self.assertNotIn(value, combined)
        for name in secrets:
            self.assertNotIn(name, combined)


class VerifyCoreCliTests(unittest.TestCase):
    def test_json_verify_core_exits_1_on_unknown_and_does_not_steal_findings(self) -> None:
        payload = {
            "status": "failed",
            "passed": False,
            "checks": [
                {"domain": "docker", "status": "unknown", "reason": "docker daemon could not be inspected"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.homeflix_setup.cli.verify_core", return_value=payload):
                code, stdout, stderr = run_main("--json", "verify", "core", repository_root=root)
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        result = parse_single_json(stdout)
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("findings", result)
        self.assertEqual(result["checks"][0]["status"], "unknown")
        self.assertNotIn("127.0.0.1", stdout)

    def test_verify_vpn_requires_disrupt_and_routine_vpn_verify_stays_separate(self) -> None:
        from scripts.homeflix_setup.cli import build_parser

        parser = build_parser()
        parsed_verify = parser.parse_args(["verify", "vpn"])
        self.assertEqual(parsed_verify.command, "verify")
        self.assertEqual(parsed_verify.phase, "vpn")
        self.assertFalse(parsed_verify.disrupt)
        parsed_disrupt = parser.parse_args(["verify", "vpn", "--disrupt"])
        self.assertTrue(parsed_disrupt.disrupt)
        parsed = parser.parse_args(["vpn", "verify", "--dry-run"])
        self.assertEqual(parsed.command, "vpn")
        self.assertEqual(parsed.vpn_command, "verify")
        self.assertTrue(parsed.dry_run)
        self.assertFalse(parsed.disrupt)
        parsed_vpn_disrupt = parser.parse_args(["vpn", "verify", "--disrupt"])
        self.assertTrue(parsed_vpn_disrupt.disrupt)
        parsed_reveal = parser.parse_args(["secrets", "reveal", "jellyfin"])
        self.assertEqual(parsed_reveal.secrets_command, "reveal")
        self.assertEqual(parsed_reveal.service, "jellyfin")

    def test_verify_core_and_contract_refuse_disrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for phase in ("core", "contract"):
                with self.subTest(phase=phase):
                    with patch("scripts.homeflix_setup.cli.verify_core", side_effect=AssertionError("must not verify core")):
                        with patch("scripts.homeflix_setup.cli.evaluate_stack_contract", side_effect=AssertionError("must not evaluate contract")):
                            code, stdout, stderr = run_main("--json", "verify", phase, "--disrupt", repository_root=root)
                    self.assertEqual(code, 1)
                    combined = stdout + stderr
                    self.assertIn("disruptive verification applies only to verify vpn", combined)

