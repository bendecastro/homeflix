"""Gluetun-only acquisition gate: start, health, DNS, and egress evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse

from .command import CommandRunner
from .compose import (
    GLUETUN_SERVICES,
    compose_command,
    compose_inventory,
    compose_ps,
    compose_up_gluetun,
    render_compose_config,
)
from .contract import evaluate_stack_contract
from .core import wait_for_container
from .envfile import EnvDocument
from .preflight import PreflightReport, run_preflight
from .state import SetupState


GATED_SERVICES = ("qbittorrent", "nzbget", "prowlarr")
VPN_EVIDENCE_TTL_SECONDS = 24 * 60 * 60
VPN_HEALTH_TIMEOUT = 120.0
EGRESS_URL = "https://api.ipify.org"
_CHECK_STATUSES = ("pass", "warning", "failure", "not-applicable", "unknown")
_EVIDENCE_BOOLS = ("tunnel_healthy", "tunnel_device", "namespace_dns", "egress_distinct")
_MANDATORY_DOMAINS = frozenset({
    "stack_contract",
    "preflight",
    "gated_services",
    "service:gluetun",
    "tunnel_device",
    "namespace_dns",
    "egress",
})


def _check(domain: str, passed: bool | None, reason: str, *, status: str | None = None) -> dict[str, object]:
    if status is None:
        status = "pass" if passed is True else "failure" if passed is False else "unknown"
    if status not in _CHECK_STATUSES:
        raise ValueError("verification check status is invalid")
    return {"domain": domain, "status": status, "reason": reason}


def _remaining(deadline: float, clock: Callable[[], float], cap: float) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("VPN gate deadline exhausted")
    return min(cap, remaining)


def _load_environment(root: Path) -> EnvDocument:
    return EnvDocument.load(root / ".env")


def vpn_config_digest(config: EnvDocument) -> str:
    """Identity of VPN-relevant configuration, including hashed secret material."""

    parts = []
    for key in (
        "VPN_SERVICE_PROVIDER",
        "VPN_TYPE",
        "VPN_SERVER_COUNTRIES",
        "VPN_DNS",
        "VPN_HEALTH_TARGET",
        "GLUETUN_TAG",
        "VPN_USER",
        "VPN_PASSWORD",
    ):
        parts.append(f"{key}={config.get(key) or ''}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _health_hostname(config: EnvDocument) -> str:
    target = config.get("VPN_HEALTH_TARGET") or "cloudflare.com:443"
    host = target.split("/", 1)[0]
    if "://" in target:
        parsed = urlparse(target)
        host = parsed.hostname or host
    return host.split(":", 1)[0] or "cloudflare.com"


def _contract_check(report: Mapping[str, object] | None) -> dict[str, object]:
    if report is None:
        return _check("stack_contract", None, "stack contract could not be inspected")
    findings = report.get("findings")
    if not isinstance(findings, list):
        return _check("stack_contract", None, "stack contract could not be inspected")
    if report.get("passed") is True and not findings:
        return _check("stack_contract", True, "stack contract holds")
    codes = [
        item.get("code")
        for item in findings
        if isinstance(item, Mapping) and isinstance(item.get("code"), str)
    ]
    reason = "stack contract findings present"
    if codes:
        reason = "stack contract findings: " + ", ".join(str(code) for code in codes if isinstance(code, str))
    return _check("stack_contract", False, reason)


def _failed(checks: list[dict[str, object]]) -> dict[str, object]:
    return {"status": "failed", "passed": False, "checks": checks, "state_written": False}


def _runtime_passed(checks: Sequence[Mapping[str, object]]) -> bool:
    observed = {item.get("domain") for item in checks}
    if not _MANDATORY_DOMAINS.issubset(observed):
        return False
    for item in checks:
        if item.get("status") not in {"pass", "warning"}:
            return False
    return True


def vpn_evidence_is_current(
    evidence: Mapping[str, object] | None,
    *,
    image_id: str,
    config_digest: str,
    now: datetime | None = None,
    ttl_seconds: int = VPN_EVIDENCE_TTL_SECONDS,
) -> bool:
    """True only for bounded, unexpired evidence matching the current image and config."""

    if not evidence:
        return False
    allowed = {"recorded_at", "image_id", "config_digest", *_EVIDENCE_BOOLS}
    if set(evidence) - allowed:
        return False
    recorded = evidence.get("recorded_at")
    stored_image = evidence.get("image_id")
    stored_digest = evidence.get("config_digest")
    if not isinstance(recorded, str) or not isinstance(stored_image, str) or not isinstance(stored_digest, str):
        return False
    if stored_image != image_id or stored_digest != config_digest:
        return False
    if any(evidence.get(name) is not True for name in _EVIDENCE_BOOLS):
        return False
    try:
        stamp = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
    except ValueError:
        return False
    moment = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (moment - stamp).total_seconds() <= ttl_seconds


def _gluetun_up_argv(root: Path, project_name: str | None) -> list[str]:
    return [*compose_command(root, project_name=project_name), "up", "--detach", "--no-deps", *GLUETUN_SERVICES]


def _running_gated(inventory: Sequence[Mapping[str, str]]) -> list[str]:
    running: list[str] = []
    for item in inventory:
        if item.get("service") in GATED_SERVICES and item.get("state") in {"running", "restarting"}:
            running.append(item["service"])
    return running


def start_gluetun_only(
    repository_root: str | os.PathLike[str],
    runner: CommandRunner,
    *,
    project_name: str | None = None,
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    return compose_up_gluetun(
        repository_root, GLUETUN_SERVICES, runner, project_name=project_name, timeout=timeout
    )


def wait_gluetun_healthy(
    repository_root: str | os.PathLike[str],
    runner: CommandRunner,
    *,
    project_name: str | None = None,
    timeout: float = VPN_HEALTH_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    def probe(remaining: float) -> Mapping[str, Mapping[str, str]]:
        return compose_ps(repository_root, runner, project_name=project_name, timeout=remaining)

    result = wait_for_container("gluetun", probe, timeout=timeout, clock=clock, sleep=sleep)
    return result.ready


def resolve_in_namespace(
    runner: CommandRunner,
    hostname: str,
    *,
    timeout: float,
) -> bool:
    result = runner.run(
        ("docker", "exec", "gluetun", "getent", "hosts", hostname),
        check=False,
        timeout=timeout,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _probe_tunnel_device(runner: CommandRunner, *, timeout: float) -> bool:
    result = runner.run(
        ("docker", "exec", "gluetun", "test", "-e", "/dev/net/tun"),
        check=False,
        timeout=timeout,
    )
    return result.returncode == 0


def _fetch_egress(runner: CommandRunner, argv: Sequence[str], *, timeout: float) -> str | None:
    result = runner.run(tuple(argv), check=False, timeout=timeout)
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def compare_egress(runner: CommandRunner, *, timeout: float) -> bool | None:
    """Return True when host and tunnel egress differ. Never expose addresses."""

    host = _fetch_egress(
        runner,
        ("curl", "-fsS", "--max-time", str(max(1, int(timeout))), EGRESS_URL),
        timeout=timeout,
    )
    tunnel = _fetch_egress(
        runner,
        ("docker", "exec", "gluetun", "wget", "-qO-", EGRESS_URL),
        timeout=timeout,
    )
    if host is None or tunnel is None:
        return None
    return host != tunnel


def _inspect_image_id(runner: CommandRunner, *, timeout: float) -> str | None:
    result = runner.run(
        ("docker", "inspect", "--format", "{{.Image}}", "gluetun"),
        check=False,
        timeout=timeout,
    )
    image = (result.stdout or "").strip()
    if result.returncode != 0 or not image:
        return None
    return image


def _store_evidence(root: Path, evidence: Mapping[str, object]) -> bool:
    state_path = root / ".homeflix" / "setup.json"
    try:
        state = SetupState.load(state_path)
    except (OSError, ValueError):
        state = SetupState()
    state.evidence = dict(evidence)
    try:
        state.save(state_path)
    except (OSError, ValueError):
        return False
    return True


def verify_vpn(
    repository_root: str | os.PathLike[str],
    *,
    runner: CommandRunner | None = None,
    dry_run: bool = False,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    contract_evaluator: Callable[[Path], Mapping[str, object]] | None = None,
    preflight: Callable[..., PreflightReport] = run_preflight,
    readiness_timeout: float = VPN_HEALTH_TIMEOUT,
) -> dict[str, object]:
    """Collect current non-disruptive Gluetun gate evidence. Never starts gated clients."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
    checks: list[dict[str, object]] = []

    try:
        config = _load_environment(root)
        project_name = config.get("COMPOSE_PROJECT_NAME")
    except (OSError, ValueError):
        checks.append(_check("stack_contract", None, "stack contract could not be inspected"))
        return _failed(checks)

    try:
        if contract_evaluator is None:
            report = evaluate_stack_contract(render_compose_config(root))
        else:
            report = contract_evaluator(root)
        checks.append(_contract_check(report))
        if report.get("passed") is not True:
            return _failed(checks)
    except (OSError, RuntimeError, ValueError, TypeError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("stack_contract", None, "stack contract could not be inspected"))
        return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 30)
        report = preflight(config, "acquisition", command_runner, deadline=operation_deadline, clock=clock)
        if remaining and report.passed:
            checks.append(_check("preflight", True, "acquisition preflight passed"))
        else:
            checks.append(_check("preflight", False, "acquisition preflight failed"))
            return _failed(checks)
    except TypeError:
        report = preflight(config, "acquisition", command_runner)
        if report.passed:
            checks.append(_check("preflight", True, "acquisition preflight passed"))
        else:
            checks.append(_check("preflight", False, "acquisition preflight failed"))
            return _failed(checks)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("preflight", None, "acquisition preflight could not be inspected"))
        return _failed(checks)

    up_argv = _gluetun_up_argv(root, project_name)
    if dry_run:
        prefix = compose_command(root, project_name=project_name)
        return {
            "status": "planned",
            "passed": True,
            "services": list(GLUETUN_SERVICES),
            "mutation_commands": [up_argv],
            "read_only_commands": [
                [*prefix, "config", "--format", "json"],
                [*prefix, "ps", "--all", "--format", "json"],
            ],
            "state_written": False,
            "checks": checks,
        }

    try:
        remaining = _remaining(operation_deadline, clock, 30)
        inventory = compose_inventory(root, command_runner, project_name=project_name, timeout=remaining)
        running = _running_gated(inventory)
        if running:
            checks.append(
                _check(
                    "gated_services",
                    False,
                    "gated services are already running",
                )
            )
            return _failed(checks)
        checks.append(_check("gated_services", True, "gated services are stopped"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("gated_services", None, "gated services could not be inspected"))
        return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 300)
        started = start_gluetun_only(root, command_runner, project_name=project_name, timeout=remaining)
        if started.returncode:
            checks.append(_check("service:gluetun", False, "gluetun could not be started"))
            return _failed(checks)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("service:gluetun", None, "gluetun could not be started"))
        return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, VPN_HEALTH_TIMEOUT)
        healthy = wait_gluetun_healthy(
            root,
            command_runner,
            project_name=project_name,
            timeout=remaining,
            clock=clock,
            sleep=sleep,
        )
        if not healthy:
            checks.append(_check("service:gluetun", False, "gluetun is not healthy"))
            return _failed(checks)
        checks.append(_check("service:gluetun", True, "gluetun is healthy"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("service:gluetun", None, "gluetun health could not be inspected"))
        return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 10)
        if not _probe_tunnel_device(command_runner, timeout=remaining):
            checks.append(_check("tunnel_device", False, "tunnel device is missing"))
            return _failed(checks)
        checks.append(_check("tunnel_device", True, "tunnel device is present"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("tunnel_device", None, "tunnel device could not be inspected"))
        return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 10)
        if not resolve_in_namespace(command_runner, _health_hostname(config), timeout=remaining):
            checks.append(_check("namespace_dns", False, "namespace DNS probe failed"))
            return _failed(checks)
        checks.append(_check("namespace_dns", True, "namespace DNS probe succeeded"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("namespace_dns", None, "namespace DNS probe could not be inspected"))
        return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 15)
        distinct = compare_egress(command_runner, timeout=remaining)
        if distinct is None:
            checks.append(_check("egress", None, "egress comparison is unknown"))
            return _failed(checks)
        if distinct is False:
            checks.append(_check("egress", False, "tunnel egress matches host egress"))
            return _failed(checks)
        checks.append(_check("egress", True, "tunnel egress differs from host"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("egress", None, "egress comparison could not be inspected"))
        return _failed(checks)

    image_id = None
    try:
        remaining = _remaining(operation_deadline, clock, 10)
        image_id = _inspect_image_id(command_runner, timeout=remaining)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        image_id = None
    if not image_id:
        checks.append(_check("evidence", None, "gluetun image identity could not be inspected"))
        return _failed(checks)

    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence = {
        "recorded_at": recorded_at,
        "image_id": image_id,
        "config_digest": vpn_config_digest(config),
        "tunnel_healthy": True,
        "tunnel_device": True,
        "namespace_dns": True,
        "egress_distinct": True,
    }
    written = _store_evidence(root, evidence)
    passed = _runtime_passed(checks)
    return {
        "status": "verified" if passed and written else "failed",
        "passed": passed and written,
        "checks": checks,
        "evidence": {
            **evidence,
            "current": True,
        },
        "state_written": written,
    }
