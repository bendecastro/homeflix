"""Gluetun-only acquisition gate: start, health, DNS, and egress evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
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
VPN_RESTORE_BUDGET = VPN_HEALTH_TIMEOUT + 30.0
VPN_BLOCKED_EGRESS_TIMEOUT = 5.0
EGRESS_URL = "https://api.ipify.org"
_CHECK_STATUSES = ("pass", "warning", "failure", "not-applicable", "unknown")
_EVIDENCE_BOOLS = ("tunnel_healthy", "tunnel_device", "namespace_dns", "egress_distinct")
_OPTIONAL_EVIDENCE_BOOLS = ("fail_closed",)
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


def _compensation_deadline(deadline: float, clock: Callable[[], float]) -> float:
    return max(float(deadline), clock() + VPN_RESTORE_BUDGET)


def _blocked_egress_timeout(deadline: float, clock: Callable[[], float]) -> float:
    leftover = deadline - clock()
    if leftover <= 0:
        raise TimeoutError("VPN gate deadline exhausted")
    available = leftover - VPN_RESTORE_BUDGET
    if available <= 0:
        return min(VPN_BLOCKED_EGRESS_TIMEOUT, leftover)
    return min(VPN_BLOCKED_EGRESS_TIMEOUT, available)


def _blocked_egress_argv(timeout: float) -> tuple[str, ...]:
    seconds = max(1, int(timeout))
    return ("docker", "exec", "gluetun", "wget", "-qO-", "--tries=1", "-T", str(seconds), EGRESS_URL)


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
    allowed = {"recorded_at", "image_id", "config_digest", *_EVIDENCE_BOOLS, *_OPTIONAL_EVIDENCE_BOOLS}
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
    if "fail_closed" in evidence and type(evidence["fail_closed"]) is not bool:
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


def _record_fail_closed(root: Path) -> bool:
    """Add fail_closed only onto current #9 evidence. Never invent a new gate record."""

    existing = _load_stored_evidence(root)
    if not existing:
        return False
    updated = dict(existing)
    updated["fail_closed"] = True
    return _store_evidence(root, updated)


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


_FAIL_CLOSED_MANDATORY = frozenset(
    {
        "intent",
        "vpn_evidence",
        "tunnel_interface",
        "pre_egress",
        "disruption",
        "blocked_egress",
        "restore:gluetun",
        "post_health",
        "post_egress",
    }
)


def _fail_closed_result(
    checks: list[dict[str, object]],
    *,
    disrupted: bool = False,
    restored: bool = False,
    snapshot_services: Sequence[str] = (),
    restored_services: Sequence[str] = (),
    passed: bool = False,
) -> dict[str, object]:
    return {
        "status": "verified" if passed else "failed",
        "passed": passed,
        "disrupted": disrupted,
        "restored": restored,
        "snapshot_services": list(snapshot_services),
        "restored_services": list(restored_services),
        "checks": checks,
    }


def _fail_closed_passed(checks: Sequence[Mapping[str, object]], snapshot: Sequence[str]) -> bool:
    domains = {item.get("domain") for item in checks}
    required = set(_FAIL_CLOSED_MANDATORY) | {f"restore:{name}" for name in snapshot}
    if not required.issubset(domains):
        return False
    return all(item.get("status") in {"pass", "warning"} for item in checks)


def _has_domain(checks: Sequence[Mapping[str, object]], domain: str) -> bool:
    return any(item.get("domain") == domain for item in checks)


def _restore_after_disruption(
    root: Path,
    runner: CommandRunner,
    *,
    project_name: str | None,
    snapshot: Sequence[str],
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[list[dict[str, object]], list[str], bool]:
    checks: list[dict[str, object]] = []
    restored: list[str] = []
    prefix = compose_command(root, project_name=project_name)
    restore_deadline = _compensation_deadline(deadline, clock)

    try:
        remaining = _remaining(restore_deadline, clock, 300)
        result = runner.run((*prefix, "restart", "gluetun"), check=False, timeout=remaining)
        if result.returncode:
            checks.append(_check("restore:gluetun", False, "gluetun could not be restored"))
        else:
            checks.append(_check("restore:gluetun", True, "gluetun restored"))
            restored.append("gluetun")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError, KeyboardInterrupt):
        checks.append(_check("restore:gluetun", None, "gluetun could not be restored"))

    healthy = False
    if "gluetun" in restored:
        try:
            remaining = _remaining(restore_deadline, clock, VPN_HEALTH_TIMEOUT)
            healthy = wait_gluetun_healthy(
                root,
                runner,
                project_name=project_name,
                timeout=remaining,
                clock=clock,
                sleep=sleep,
            )
            if healthy:
                checks.append(_check("post_health", True, "gluetun is healthy"))
            else:
                checks.append(_check("post_health", False, "gluetun is not healthy"))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError, KeyboardInterrupt):
            checks.append(_check("post_health", None, "gluetun health could not be inspected"))

    for service in snapshot:
        try:
            remaining = _remaining(restore_deadline, clock, 300)
            result = runner.run((*prefix, "restart", service), check=False, timeout=remaining)
            if result.returncode:
                checks.append(_check(f"restore:{service}", False, f"{service} could not be restored"))
            else:
                checks.append(_check(f"restore:{service}", True, f"{service} restored"))
                restored.append(service)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError, KeyboardInterrupt):
            checks.append(_check(f"restore:{service}", None, f"{service} could not be restored"))

    complete = "gluetun" in restored and all(service in restored for service in snapshot) and healthy
    return checks, restored, complete


_LINK_LINE = re.compile(r"^\d+:\s+([^:@\s]+)(?:@[^:]+)?:\s+<([^>]*)>", re.MULTILINE)
_ROUTE_DEV = re.compile(r"\bdev\s+(\S+)")


def classify_interface(name: str, flags: str = "", *, default_device: str | None = None) -> str:
    """Return a safe interface class. Never treat the default-route NIC as a tunnel."""

    lowered = name.strip().casefold()
    flagset = {item.strip().casefold() for item in flags.split(",") if item.strip()}
    if lowered in {"lo", "lo0"} or "loopback" in flagset:
        return "loopback"
    if (
        lowered == "docker0"
        or lowered.startswith("br-")
        or lowered.startswith("veth")
        or lowered.startswith("cni")
        or lowered.startswith("br0")
    ):
        return "docker-bridge"
    if lowered.startswith(("eth", "en", "em")):
        return "ethernet"
    if lowered.startswith(("tun", "wg")) or "pointopoint" in flagset:
        return "tunnel"
    if default_device and lowered == default_device.strip().casefold():
        return "default-route"
    return "unknown"


def _parse_default_device(route_text: str) -> str | None:
    match = _ROUTE_DEV.search(route_text)
    if not match:
        return None
    return match.group(1)


def _discover_tunnel_interface(
    runner: CommandRunner,
    *,
    timeout: float,
) -> tuple[str | None, str, str]:
    """Return (iface_name_or_none, classification, reason). Name stays internal."""

    listing = runner.run(
        ("docker", "exec", "gluetun", "ip", "-o", "link", "show"),
        check=False,
        timeout=timeout,
    )
    routes = runner.run(
        ("docker", "exec", "gluetun", "ip", "-o", "route", "show", "default"),
        check=False,
        timeout=timeout,
    )
    if listing.returncode != 0:
        return None, "unknown", "tunnel interface could not be inspected"
    default_device = _parse_default_device(routes.stdout if routes.returncode == 0 else "")
    tunnels: list[str] = []
    refused: list[str] = []
    saw_loopback = False
    for match in _LINK_LINE.finditer(listing.stdout or ""):
        name, flags = match.group(1), match.group(2)
        flagset = {item.strip().casefold() for item in flags.split(",") if item.strip()}
        if "up" not in flagset:
            continue
        kind = classify_interface(name, flags, default_device=default_device)
        if kind == "tunnel":
            tunnels.append(name)
        elif kind == "loopback":
            saw_loopback = True
        else:
            refused.append(kind)
    if len(tunnels) == 1:
        return tunnels[0], "tunnel", "active tunnel interface classified"
    if len(tunnels) > 1:
        return None, "unknown", "multiple tunnel interfaces are present"
    if refused:
        kind = refused[0]
        return None, kind, f"tunnel interface is {kind}"
    if saw_loopback:
        return None, "loopback", "tunnel interface is loopback"
    return None, "unknown", "active tunnel interface was not found"


def _load_stored_evidence(root: Path) -> Mapping[str, object] | None:
    try:
        return SetupState.load(root / ".homeflix" / "setup.json").evidence
    except (OSError, ValueError):
        return None


def verify_vpn_fail_closed(
    repository_root: str | os.PathLike[str],
    *,
    runner: CommandRunner | None = None,
    disrupt: bool = False,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    readiness_timeout: float = VPN_HEALTH_TIMEOUT,
) -> dict[str, object]:
    """Prove Gluetun fails closed, then restore the prior running service set."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
    checks: list[dict[str, object]] = []
    if not disrupt:
        checks.append(_check("intent", False, "disruptive verification requires --disrupt"))
        return _fail_closed_result(checks)
    checks.append(_check("intent", True, "disruptive verification requested"))

    try:
        config = _load_environment(root)
        remaining = _remaining(operation_deadline, clock, 10)
        image_id = _inspect_image_id(command_runner, timeout=remaining)
        if not image_id:
            checks.append(_check("vpn_evidence", None, "gluetun image identity could not be inspected"))
            return _fail_closed_result(checks)
        if not vpn_evidence_is_current(
            _load_stored_evidence(root),
            image_id=image_id,
            config_digest=vpn_config_digest(config),
        ):
            checks.append(_check("vpn_evidence", False, "current non-disruptive VPN-gate evidence is required"))
            return _fail_closed_result(checks)
        checks.append(_check("vpn_evidence", True, "current VPN-gate evidence is present"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("vpn_evidence", None, "VPN-gate evidence could not be inspected"))
        return _fail_closed_result(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 10)
        tunnel_iface, kind, reason = _discover_tunnel_interface(command_runner, timeout=remaining)
        if kind != "tunnel" or not tunnel_iface:
            checks.append(_check("tunnel_interface", False, reason))
            return _fail_closed_result(checks)
        checks.append(_check("tunnel_interface", True, reason))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("tunnel_interface", None, "tunnel interface could not be inspected"))
        return _fail_closed_result(checks)

    project_name = config.get("COMPOSE_PROJECT_NAME")
    snapshot: list[str] = []
    restored_services: list[str] = []
    disrupted = False
    restore_complete = False

    try:
        remaining = _remaining(operation_deadline, clock, 30)
        inventory = compose_inventory(
            root, command_runner, project_name=project_name, timeout=remaining
        )
        snapshot = _running_gated(inventory)

        remaining = _remaining(operation_deadline, clock, 15)
        distinct = compare_egress(command_runner, timeout=remaining)
        if distinct is not True:
            status = None if distinct is None else False
            reason = "egress comparison is unknown" if distinct is None else "tunnel egress matches host egress"
            checks.append(_check("pre_egress", status, reason))
            return _fail_closed_result(checks, snapshot_services=snapshot)
        checks.append(_check("pre_egress", True, "tunnel egress differs from host"))

        remaining = _remaining(operation_deadline, clock, 10)
        disrupted = True
        disrupt_result = command_runner.run(
            ("docker", "exec", "gluetun", "ip", "link", "set", tunnel_iface, "down"),
            check=False,
            timeout=remaining,
        )
        if disrupt_result.returncode:
            checks.append(_check("disruption", False, "tunnel interface could not be disabled"))
        else:
            checks.append(_check("disruption", True, "tunnel interface disabled"))
            remaining = _blocked_egress_timeout(operation_deadline, clock)
            leaked = _fetch_egress(
                command_runner,
                _blocked_egress_argv(remaining),
                timeout=remaining,
            )
            if leaked is None:
                checks.append(_check("blocked_egress", True, "external access is blocked"))
            else:
                checks.append(_check("blocked_egress", False, "external access still succeeded"))

        restore_checks, restored_services, restore_complete = _restore_after_disruption(
            root,
            command_runner,
            project_name=project_name,
            snapshot=snapshot,
            deadline=operation_deadline,
            clock=clock,
            sleep=sleep,
        )
        checks.extend(restore_checks)
        if restore_complete:
            remaining = _remaining(operation_deadline, clock, 15)
            distinct = compare_egress(command_runner, timeout=remaining)
            if distinct is True:
                checks.append(_check("post_egress", True, "tunnel egress differs from host"))
            elif distinct is False:
                checks.append(_check("post_egress", False, "tunnel egress matches host egress"))
            else:
                checks.append(_check("post_egress", None, "egress comparison is unknown"))
    except KeyboardInterrupt:
        if not _has_domain(checks, "transaction"):
            checks.append(_check("transaction", False, "verification was interrupted"))
    except TimeoutError:
        if not _has_domain(checks, "transaction"):
            checks.append(_check("transaction", False, "VPN fail-closed deadline exhausted"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        if not _has_domain(checks, "transaction"):
            checks.append(_check("transaction", None, "fail-closed verification could not be completed"))
    finally:
        if disrupted and not _has_domain(checks, "restore:gluetun"):
            restore_checks, restored_services, restore_complete = _restore_after_disruption(
                root,
                command_runner,
                project_name=project_name,
                snapshot=snapshot,
                deadline=operation_deadline,
                clock=clock,
                sleep=sleep,
            )
            checks.extend(restore_checks)
            if restore_complete and not _has_domain(checks, "post_egress"):
                try:
                    remaining = _remaining(operation_deadline, clock, 15)
                    distinct = compare_egress(command_runner, timeout=remaining)
                    if distinct is True:
                        checks.append(_check("post_egress", True, "tunnel egress differs from host"))
                    elif distinct is False:
                        checks.append(_check("post_egress", False, "tunnel egress matches host egress"))
                    else:
                        checks.append(_check("post_egress", None, "egress comparison is unknown"))
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError, KeyboardInterrupt):
                    checks.append(_check("post_egress", None, "egress comparison could not be inspected"))

    passed = _fail_closed_passed(checks, snapshot) and restore_complete and disrupted
    if passed:
        _record_fail_closed(root)
    return _fail_closed_result(
        checks,
        disrupted=disrupted,
        restored=restore_complete,
        snapshot_services=snapshot,
        restored_services=restored_services,
        passed=passed,
    )
