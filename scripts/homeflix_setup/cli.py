"""Argument parsing and rendering for the Homeflix setup CLI."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Sequence

from .api import ApiError
from .backup import BackupError, create_backup, list_backups, prune_backups, restore_backup, retrieve_backup
from .command import CommandRunner
from .compose import configure, render_compose_config
from .contract import evaluate_stack_contract
from .core import READINESS_TIMEOUT, _load_private_environment, configure_core, deploy_core, verify_core
from .discover import HostFacts, discover_host
from .envfile import EnvDocument
from .host import HostPreparationPlan, apply_host_preparation, plan_host_preparation
from .preflight import PreflightReport, run_preflight
from .secrets import reveal_jellyfin
from .state import SetupState
from .vpn import verify_vpn, verify_vpn_fail_closed


class _DeadlineRunner(CommandRunner):
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline

    def run(self, argv, *, input_text=None, check=False, redact=(), timeout=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("core setup deadline exhausted")
        capped = remaining if timeout is None else min(float(timeout), remaining)
        return super().run(argv, input_text=input_text, check=check, redact=redact, timeout=capped)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="homeflix",
        description="Inspect and configure a Homeflix deployment.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit one JSON object")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "status",
        help="show non-secret local setup status",
        description="Show setup status. Invalid state returns exit status 1.",
    )
    subparsers.add_parser(
        "discover",
        help="inspect the local host without changing it",
        description="Discover Debian/Ubuntu host capabilities without persisting private facts.",
    )
    host_parser = subparsers.add_parser("host", help="inspect or prepare host prerequisites")
    host_subparsers = host_parser.add_subparsers(dest="host_command", required=True)
    prepare_parser = host_subparsers.add_parser(
        "prepare",
        help="plan Docker host preparation (read-only unless --apply is given)",
    )
    mode = prepare_parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply the exact plan after rediscovery")
    mode.add_argument("--dry-run", action="store_true", help="explicitly request the default read-only plan")
    prepare_parser.add_argument(
        "--confirm-plan",
        metavar="FINGERPRINT",
        help="confirm the exact SHA-256 fingerprint emitted by the reviewed plan",
    )
    configure_parser = subparsers.add_parser(
        "configure", help="generate secure host configuration and a local Compose override"
    )
    configure_parser.add_argument("--data-root", required=True)
    configure_parser.add_argument("--config-root", required=True)
    configure_parser.add_argument("--cache-root", required=True)
    configure_parser.add_argument("--quality-profile", default="HD-1080p")
    configure_parser.add_argument("--direct-setup-ports", action="store_true")
    preflight_parser = subparsers.add_parser(
        "preflight", help="validate configuration and storage without starting containers"
    )
    preflight_parser.add_argument("--phase", choices=("core", "acquisition"), default="core")
    initialize_parser = subparsers.add_parser(
        "initialize", help="reconcile application APIs after core deployment"
    )
    initialize_parser.add_argument("phase", choices=("core",))
    deploy_parser = subparsers.add_parser(
        "deploy", help="reconcile an explicit deployment phase allowlist"
    )
    deploy_parser.add_argument("phase", choices=("core",))
    deploy_parser.add_argument(
        "--dry-run", action="store_true", help="print exact planned commands without probing or changing the host"
    )
    verify_parser = subparsers.add_parser(
        "verify",
        help="inspect a deployment phase; vpn --disrupt is the explicit fail-closed exception",
    )
    verify_parser.add_argument("phase", choices=("core", "contract", "vpn"))
    verify_parser.add_argument(
        "--disrupt",
        action="store_true",
        help="run explicit fail-closed VPN disruption (vpn phase only)",
    )
    setup_parser = subparsers.add_parser("setup", help="run a resumable convenience setup composition")
    setup_parser.add_argument("phase", choices=("core",))
    setup_parser.add_argument("--dry-run", action="store_true")
    setup_parser.add_argument("--data-root")
    setup_parser.add_argument("--config-root")
    setup_parser.add_argument("--cache-root")
    setup_parser.add_argument("--quality-profile")
    setup_parser.add_argument("--direct-setup-ports", action="store_true")
    secrets_parser = subparsers.add_parser("secrets", help="explicitly retrieve local credentials")
    secrets_subparsers = secrets_parser.add_subparsers(dest="secrets_command", required=True)
    reveal_parser = secrets_subparsers.add_parser("reveal", help="reveal credentials on /dev/tty only")
    reveal_parser.add_argument("service", choices=("jellyfin",))
    secrets_subparsers.add_parser("vpn", help="enter VPN provider credentials on /dev/tty only")
    vpn_parser = subparsers.add_parser("vpn", help="acquisition VPN gate")
    vpn_subparsers = vpn_parser.add_subparsers(dest="vpn_command", required=True)
    vpn_verify_parser = vpn_subparsers.add_parser(
        "verify",
        help="start Gluetun only and collect current acquisition-gate evidence",
    )
    vpn_verify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the Gluetun-only plan without starting services",
    )
    vpn_verify_parser.add_argument(
        "--disrupt",
        action="store_true",
        help="prove VPN fail-closed behavior and restore the prior running set",
    )
    backup_parser = subparsers.add_parser("backup", help="create and restore CONFIG_ROOT backup artifacts")
    backup_subparsers = backup_parser.add_subparsers(dest="backup_command", required=True)
    backup_subparsers.add_parser("create", help="snapshot CONFIG_ROOT into the local artifact repository")
    backup_subparsers.add_parser("list", help="list local backup artifacts newest first")
    retrieve_parser = backup_subparsers.add_parser("retrieve", help="copy one artifact out of the repository")
    retrieve_parser.add_argument("--archive", required=True, help="archive filename")
    retrieve_parser.add_argument("--to", required=True, dest="retrieve_to", help="destination file or directory")
    backup_subparsers.add_parser("prune", help="retain BACKUP_KEEP matching artifacts")
    restore_parser = backup_subparsers.add_parser(
        "restore", help="extract one artifact into an empty scratch directory"
    )
    restore_parser.add_argument("--to", required=True, dest="restore_to", help="empty scratch directory")
    restore_parser.add_argument("--archive", help="archive filename (default: newest)")
    return parser


def _configured_domain(repository_root: Path) -> str:
    for candidate in (repository_root / ".env", repository_root / ".env.example"):
        if not candidate.exists():
            continue
        try:
            domain = EnvDocument.load(candidate).get("DOMAIN")
        except OSError:
            continue
        if domain:
            return domain
    return "homeflix"


def _input_error(*, json_output: bool, code: str, label: str, error: Exception) -> int:
    if json_output:
        print(json.dumps({"error": {"code": code, "message": str(error)}}, sort_keys=True))
    else:
        print(f"homeflix: {label}: {error}", file=sys.stderr)
    return 1


def _status(repository_root: Path) -> dict[str, object]:
    state_path = repository_root / ".homeflix" / "setup.json"
    exists = state_path.exists()
    state = SetupState.load(state_path)
    return {
        "schema_version": state.schema_version,
        "state_exists": exists,
        "checkpoints": state.checkpoints,
        "host_facts": state.host_facts,
    }


def main(argv: Sequence[str] | None = None, *, repository_root: Path | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    root = repository_root or Path(__file__).resolve().parents[2]

    discovered: HostFacts | None = None
    preparation: HostPreparationPlan | None = None
    preflight: PreflightReport | None = None
    if arguments.command == "secrets":
        if arguments.json_output:
            print("homeflix: secret operations do not support JSON output", file=sys.stderr)
            return 2
        if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
            print("homeflix: secret operations require an unredirected controlling terminal", file=sys.stderr)
            return 2
        if arguments.secrets_command == "vpn":
            try:
                from .secrets import set_vpn_secrets

                result = set_vpn_secrets(root / ".env")
            except (OSError, ValueError, RuntimeError) as error:
                print(f"homeflix: unable to store VPN credentials: {error}", file=sys.stderr)
                return 1
            for item in result.get("keys", []):
                print(f"{item['name']}: {item['status']}")
            return 0
        try:
            reveal_jellyfin(root / ".env")
        except (OSError, ValueError, RuntimeError) as error:
            print(f"homeflix: unable to reveal Jellyfin credentials: {error}", file=sys.stderr)
            return 1
        return 0
    if arguments.command == "status":
        try:
            result = _status(root)
        except (OSError, ValueError) as error:
            if arguments.json_output:
                print(
                    json.dumps(
                        {"error": {"code": "invalid_state", "message": str(error)}},
                        sort_keys=True,
                    )
                )
            else:
                print(f"homeflix: invalid setup state: {error}", file=sys.stderr)
            return 1
    elif arguments.command == "discover":
        try:
            discovered = discover_host(CommandRunner(), domain=_configured_domain(root))
        except (OSError, ValueError) as error:
            return _input_error(
                json_output=arguments.json_output,
                code="discovery_refused",
                label="discovery refused",
                error=error,
            )
        result = discovered.to_dict()
    elif arguments.command == "host" and arguments.host_command == "prepare":
        runner = CommandRunner()
        try:
            discovered = discover_host(runner, domain=_configured_domain(root))
        except (OSError, ValueError) as error:
            return _input_error(
                json_output=arguments.json_output,
                code="discovery_refused",
                label="host discovery refused",
                error=error,
            )
        preparation = plan_host_preparation(discovered)
        if arguments.apply and preparation.refusal is None:
            if not arguments.confirm_plan:
                preparation = replace(
                    preparation,
                    refusal={
                        "code": "plan_confirmation_required",
                        "message": "Applying host preparation requires the reviewed plan fingerprint",
                        "action": "Run a dry plan, review it, then pass --confirm-plan FINGERPRINT",
                    },
                )
            else:
                preparation = apply_host_preparation(
                    preparation, runner, confirm_plan=arguments.confirm_plan
                )
        result = preparation.to_dict()
    elif arguments.command == "configure":
        try:
            discovered = discover_host(CommandRunner(), domain=_configured_domain(root))
            result = configure(
                root,
                discovered,
                data_root=arguments.data_root,
                config_root=arguments.config_root,
                cache_root=arguments.cache_root,
                quality_profile=arguments.quality_profile,
                direct_setup_ports=arguments.direct_setup_ports,
            )
        except (OSError, ValueError) as error:
            return _input_error(
                json_output=arguments.json_output,
                code="configuration_refused",
                label="configuration refused",
                error=error,
            )
    elif arguments.command == "preflight":
        try:
            config = EnvDocument.load(root / ".env")
            preflight = run_preflight(config, arguments.phase, CommandRunner())
            result = preflight.to_dict()
        except (OSError, ValueError) as error:
            return _input_error(
                json_output=arguments.json_output,
                code="preflight_refused",
                label="preflight refused",
                error=error,
            )
    elif arguments.command == "initialize" and arguments.phase == "core":
        try:
            result = configure_core(root)
        except ApiError as error:
            return _input_error(
                json_output=arguments.json_output,
                code=error.code,
                label="API initialization refused",
                error=error,
            )
        except (OSError, RuntimeError, ValueError):
            return _input_error(
                json_output=arguments.json_output,
                code="initialization_refused",
                label="API initialization refused",
                error=RuntimeError("core APIs could not be configured safely"),
            )
    elif arguments.command == "verify" and arguments.phase == "vpn":
        try:
            result = verify_vpn_fail_closed(root, disrupt=arguments.disrupt)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
            return _input_error(
                json_output=arguments.json_output,
                code="verification_refused",
                label="VPN verification refused",
                error=RuntimeError("VPN fail-closed verification could not be completed safely"),
            )
    elif arguments.command == "verify" and arguments.phase == "contract":
        if arguments.disrupt:
            return _input_error(
                json_output=arguments.json_output,
                code="verification_refused",
                label="verification refused",
                error=RuntimeError("disruptive verification applies only to verify vpn"),
            )
        try:
            result = evaluate_stack_contract(render_compose_config(root))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return _input_error(
                json_output=arguments.json_output,
                code="verification_refused",
                label="verification refused",
                error=RuntimeError("stack contract could not be verified safely"),
            )
    elif arguments.command == "verify" and arguments.phase == "core":
        if arguments.disrupt:
            return _input_error(
                json_output=arguments.json_output,
                code="verification_refused",
                label="verification refused",
                error=RuntimeError("disruptive verification applies only to verify vpn"),
            )
        try:
            result = verify_core(root)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return _input_error(json_output=arguments.json_output, code="verification_refused", label="verification refused", error=RuntimeError("core state could not be verified safely"))
    elif arguments.command == "setup" and arguments.phase == "core":
        if arguments.dry_run:
            result = {
                "status": "planned", "state_written": False,
                "phases": ["configure", "preflight:core", "deploy:core", "initialize:core", "verify:core"],
                "services": ["traefik", "jellyfin", "jellyseerr", "radarr", "sonarr"],
                "commands": [
                    ["scripts/homeflix", "configure", "--data-root", "DATA_ROOT", "--config-root", "CONFIG_ROOT", "--cache-root", "CACHE_ROOT"],
                    ["scripts/homeflix", "preflight", "--phase", "core"],
                    ["docker", "compose", "--project-name", "homeflix", "up", "--detach", "--no-deps", "traefik", "jellyfin", "jellyseerr", "radarr", "sonarr"],
                    ["scripts/homeflix", "initialize", "core"], ["scripts/homeflix", "verify", "core"],
                ],
                "required_human_inputs": ["DATA_ROOT", "CONFIG_ROOT", "CACHE_ROOT", "quality profile if the default is unsuitable"],
                "acquisition_mutations": [],
            }
        else:
            operation_deadline = time.monotonic() + READINESS_TIMEOUT
            phase_names = ("configure", "preflight:core", "deploy:core", "initialize:core", "verify:core")
            phases: list[dict[str, object]] = []

            def stop(status: str, phase: str, details: object | None = None) -> dict[str, object]:
                phases.append({"phase": phase, "status": "fail"})
                completed = {item["phase"] for item in phases}
                phases.extend({"phase": name, "status": "skipped"} for name in phase_names if name not in completed)
                failure: dict[str, object] = {"status": status, "phases": phases}
                if details is not None:
                    failure["details"] = details
                return failure

            existing: EnvDocument | None = None
            try:
                if (root / ".env").exists():
                    existing = _load_private_environment(root / ".env")
                supplied = (arguments.data_root, arguments.config_root, arguments.cache_root)
                if any(supplied) and not all(supplied):
                    raise ValueError("all three root paths are required when configuring setup")
                if all(supplied):
                    data_root, config_root, cache_root = supplied
                elif existing is not None:
                    data_root, config_root, cache_root = (
                        existing.get("DATA_ROOT"), existing.get("CONFIG_ROOT"), existing.get("CACHE_ROOT")
                    )
                    if not all((data_root, config_root, cache_root)):
                        raise ValueError("existing configuration is missing required root paths")
                else:
                    raise ValueError("DATA_ROOT, CONFIG_ROOT, and CACHE_ROOT are required for a new setup")
                quality_profile = arguments.quality_profile or (
                    existing.get("QUALITY_PROFILE") if existing is not None else None
                ) or "HD-1080p"
                discovered = discover_host(_DeadlineRunner(operation_deadline), domain=_configured_domain(root))
                configure(
                    root, discovered,
                    data_root=data_root, config_root=config_root, cache_root=cache_root,
                    quality_profile=quality_profile,
                    direct_setup_ports=arguments.direct_setup_ports,
                )
                if time.monotonic() >= operation_deadline:
                    raise TimeoutError("core setup deadline exhausted")
                phases.append({"phase": "configure", "status": "complete"})
            except TimeoutError:
                result = stop("timeout", "configure")
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                result = stop("configuration_failed", "configure")
            else:
                try:
                    config = _load_private_environment(root / ".env")
                    preflight = run_preflight(config, "core", _DeadlineRunner(operation_deadline), deadline=operation_deadline)
                except TimeoutError:
                    result = stop("timeout", "preflight:core")
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    result = stop("preflight_failed", "preflight:core")
                else:
                    if not preflight.passed:
                        result = stop("preflight_failed", "preflight:core", preflight.to_dict())
                    else:
                        phases.append({"phase": "preflight:core", "status": "pass"})
                        try:
                            deployed = deploy_core(root, deadline=operation_deadline)
                        except TimeoutError:
                            result = stop("timeout", "deploy:core")
                        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                            result = stop("deployment_failed", "deploy:core")
                        else:
                            deployment_verified = deployed.get("status") in {"ready", "already_ready"} or (
                                deployed.get("status") == "checkpoint_failed"
                                and all(isinstance(item, dict) and item.get("ready") is True for item in deployed.get("services", []))
                            )
                            if deployed.get("status") == "timeout":
                                result = stop("timeout", "deploy:core", deployed)
                            elif not deployment_verified:
                                result = stop("deployment_failed", "deploy:core", deployed)
                            else:
                                phases.append({"phase": "deploy:core", "status": "complete"})
                                try:
                                    initialized = configure_core(root, deadline=operation_deadline)
                                except TimeoutError:
                                    result = stop("timeout", "initialize:core")
                                except ApiError as error:
                                    result = stop("timeout" if error.code == "deadline_exhausted" else "initialization_failed", "initialize:core")
                                except (OSError, RuntimeError, ValueError):
                                    result = stop("initialization_failed", "initialize:core")
                                else:
                                    phases.append({"phase": "initialize:core", "status": "complete"})
                                    try:
                                        verified = verify_core(root, deadline=operation_deadline)
                                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                                        result = stop("verification_failed", "verify:core")
                                    else:
                                        if verified.get("passed") is not True:
                                            result = stop("verification_failed", "verify:core", verified)
                                        else:
                                            phases.append({"phase": "verify:core", "status": "pass"})
                                            state_path = root / ".homeflix" / "setup.json"
                                            try:
                                                state = SetupState.load(state_path)
                                            except (OSError, ValueError):
                                                state = SetupState()
                                            state.checkpoints.update({"configured": True, "core_containers_started": True, "core_api_configured": True, "core_verified": True})
                                            try:
                                                state.save(state_path)
                                            except (OSError, ValueError):
                                                result = {"status": "checkpoint_failed", "phases": phases, "reason": "Verified core state could not be checkpointed"}
                                            else:
                                                result = {"status": "verified", "phases": phases, "verify": verified}
    elif arguments.command == "deploy" and arguments.phase == "core":
        try:
            result = deploy_core(root, dry_run=arguments.dry_run)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return _input_error(
                json_output=arguments.json_output,
                code="deployment_refused",
                label="deployment refused",
                error=RuntimeError("core deployment could not be completed safely"),
            )
    elif arguments.command == "vpn" and arguments.vpn_command == "verify":
        if arguments.disrupt:
            if arguments.dry_run:
                return _input_error(
                    json_output=arguments.json_output,
                    code="verification_refused",
                    label="VPN verification refused",
                    error=RuntimeError("disruptive verification cannot be combined with --dry-run"),
                )
            try:
                result = verify_vpn_fail_closed(root, disrupt=True)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
                return _input_error(
                    json_output=arguments.json_output,
                    code="verification_refused",
                    label="VPN verification refused",
                    error=RuntimeError("VPN fail-closed verification could not be completed safely"),
                )
        else:
            try:
                result = verify_vpn(root, dry_run=arguments.dry_run)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
                return _input_error(
                    json_output=arguments.json_output,
                    code="verification_refused",
                    label="VPN verification refused",
                    error=RuntimeError("VPN gate could not be verified safely"),
                )
    elif arguments.command == "backup":
        try:
            if arguments.backup_command == "create":
                result = create_backup(root)
            elif arguments.backup_command == "list":
                result = list_backups(root)
            elif arguments.backup_command == "retrieve":
                result = retrieve_backup(root, archive=arguments.archive, destination=arguments.retrieve_to)
            elif arguments.backup_command == "prune":
                result = prune_backups(root)
            elif arguments.backup_command == "restore":
                result = restore_backup(root, destination=arguments.restore_to, archive=arguments.archive)
            else:  # pragma: no cover - argparse limits command values
                raise AssertionError(f"unhandled backup command {arguments.backup_command}")
        except BackupError as error:
            return _input_error(
                json_output=arguments.json_output,
                code=error.code,
                label="backup refused",
                error=error,
            )
        except (OSError, RuntimeError, ValueError):
            return _input_error(
                json_output=arguments.json_output,
                code="backup_refused",
                label="backup refused",
                error=RuntimeError("backup could not be completed safely"),
            )
    else:  # pragma: no cover - argparse limits command values
        raise AssertionError(f"unhandled command {arguments.command}")

    if arguments.json_output:
        print(json.dumps(result, sort_keys=True))
    elif preparation is not None:
        print(json.dumps(result, indent=2, sort_keys=True))
        if preparation.refusal:
            print(f"homeflix: {preparation.refusal['message']}", file=sys.stderr)
            print(f"Action: {preparation.refusal['action']}", file=sys.stderr)
    elif arguments.command == "initialize":
        print("Core API initialization: " + str(result["status"]))
        print("Jellyfin libraries: " + " ".join(result["jellyfin"]["libraries"]))
        for service in ("radarr", "sonarr"):
            print(f"{service}: profile={result[service]['profile']} root={result[service]['root']}")
        print("Jellyseerr initialized: " + str(result["jellyseerr"]["initialized"]).lower())
    elif arguments.command == "deploy":
        if result["status"] == "planned":
            print("Core services: " + " ".join(result["services"]))
            for command in result["read_only_commands"]:
                print("Read-only command: " + " ".join(command))
            for command in result["mutation_commands"]:
                print("Mutation command: " + " ".join(command))
        else:
            print(f"Core deployment: {result['status']}")
            for item in result["services"]:
                print(
                    f"{item['service']}: state={item['current_state']} "
                    f"ready={str(item['ready']).lower()} reason={item['reason']}"
                )
    elif arguments.command == "verify" and arguments.phase == "contract":
        print(f"Stack contract: {result['status']}")
        for item in result["findings"]:
            service = item.get("service")
            target = f"{item['code']}: {service}" if service else str(item["code"])
            print(f"FAIL: {target}: {item['message']}")
    elif arguments.command == "vpn" or (arguments.command == "verify" and arguments.phase == "vpn"):
        print(f"VPN verify: {result['status']}")
        for item in result.get("checks", []):
            print(f"{str(item['status']).upper()}: {item['domain']}: {item['reason']}")
        if result.get("status") == "planned":
            for command in result.get("mutation_commands", []):
                print("Mutation command: " + " ".join(command))
    elif arguments.command in {"verify", "setup"}:
        print(f"Core {arguments.command}: {result['status']}")
        if arguments.command == "verify":
            for item in result["checks"]:
                print(f"{str(item['status']).upper()}: {item['domain']}: {item['reason']}")
    elif arguments.command == "backup" and arguments.backup_command == "create":
        print(
            f"OK archive={result['archive']} sqlite={result['sqlite']} "
            f"keep={result['keep']} dest=set"
        )
    elif arguments.command == "backup" and arguments.backup_command == "list":
        for name in result["archives"]:
            print(name)
    elif arguments.command == "backup" and arguments.backup_command == "retrieve":
        print(f"OK archive={result['archive']} dest=set")
    elif arguments.command == "backup" and arguments.backup_command == "prune":
        print(f"OK keep={result['keep']} dest=set")
    elif arguments.command == "backup" and arguments.backup_command == "restore":
        for relative in result.get("databases", []):
            print(f"OK sqlite {relative}")
        print(
            f"OK archive={result['archive']} sqlite_ok={result['sqlite_ok']} "
            f"sqlite_fail={result['sqlite_fail']} dest={arguments.restore_to}"
        )
    elif arguments.command == "configure":
        for item in result["environment"]["keys"]:
            print(f"{item['name']}: {item['status']}")
        for item in result["credentials"]:
            print(f"{item['name']}: {item['status']}")
        print(f"Compose override: {result['override']['status']}")
    elif preflight is not None:
        for item in preflight.results:
            print(f"{item.status.upper()}: {item.message}")
        counts = preflight.counts
        print(f"Result: {counts['pass']} passed, {counts['warn']} warnings, {counts['fail']} failures")
    elif discovered is not None:
        if discovered.refusal:
            print(f"homeflix: {discovered.refusal['message']}", file=sys.stderr)
            print(f"Action: {discovered.refusal['action']}", file=sys.stderr)
        else:
            if discovered.docker_daemon_reachable is True:
                docker = "ready"
            elif discovered.docker_daemon_reachable is False:
                docker = "not ready"
            else:
                docker = "probe unavailable"
            print(
                f"Host: {discovered.os_pretty_name} ({discovered.architecture or 'unknown architecture'})"
            )
            mounts = (
                str(len(discovered.mounts))
                if discovered.mounts_status == "ok"
                else f"unavailable ({discovered.mounts_reason})"
            )
            ports = (
                str(len(discovered.listening_ports))
                if discovered.listening_ports_status == "ok"
                else f"unavailable ({discovered.listening_ports_reason})"
            )
            graphics = (
                str(len(discovered.graphics.render_devices))
                if discovered.graphics.status == "ok"
                else f"unavailable ({discovered.graphics.reason})"
            )
            print(
                f"Docker: {docker}; mounts: {mounts}; listening ports: {ports}; "
                f"graphics devices: {graphics}"
            )
            if discovered.host_dns_status != "ok":
                print(f"Host DNS: unavailable ({discovered.host_dns_reason})")
            for gap in discovered.capability_gaps:
                print(f"Capability gap: {gap['message']}. Action: {gap['action']}")
    else:
        state_description = "present" if result["state_exists"] else "not created"
        print(f"Setup state: {state_description} (schema {result['schema_version']})")
    if preparation is not None:
        return 1 if preparation.refusal is not None else 0
    if arguments.command == "preflight" and preflight is not None:
        return 0 if preflight.passed else 1
    if arguments.command == "deploy":
        return 0 if result["status"] in {"planned", "ready", "already_ready"} else 1
    if arguments.command == "initialize":
        return 0
    if arguments.command == "verify":
        return 0 if result.get("passed") is True else 1
    if arguments.command == "vpn":
        return 0 if result.get("status") in {"planned", "verified"} or result.get("passed") is True else 1
    if arguments.command == "setup":
        return 0 if result.get("status") in {"planned", "verified"} else 1
    if arguments.command == "backup":
        return 0 if result.get("status") in {"created", "listed", "retrieved", "pruned", "restored"} else 1
    return 1 if discovered is not None and not discovered.supported else 0
