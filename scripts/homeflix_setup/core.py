"""Live-state deployment and readiness reconciliation for the core stack."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from typing import Callable, Mapping, Sequence
from urllib import error, request

from .command import CommandRunner
from .compose import CORE_SERVICES, compose_command, compose_ps, compose_up
from .envfile import EnvDocument
from .preflight import PreflightReport, run_preflight
from .state import SetupState


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reason: str


HttpProbe = Callable[[str, Mapping[str, str], float], bool]
StateProbe = Callable[[], Mapping[str, Mapping[str, str]]]


def _http_probe(url: str, headers: Mapping[str, str], timeout: float) -> bool:
    outgoing = request.Request(url, headers=dict(headers), method="GET")
    try:
        with request.urlopen(outgoing, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (error.HTTPError, error.URLError, OSError, TimeoutError):
        return False


def wait_for_http(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 90,
    interval: float = 2,
    probe: HttpProbe = _http_probe,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ReadinessResult:
    """Wait for a bounded HTTP response without exposing response bodies or addresses."""

    deadline = clock() + timeout
    while True:
        if probe(url, headers or {}, min(5.0, max(0.1, deadline - clock()))):
            return ReadinessResult(True, "ready")
        if clock() >= deadline:
            return ReadinessResult(False, "HTTP readiness timed out")
        sleep(min(interval, max(0.0, deadline - clock())))


def wait_for_container(
    service: str,
    state_probe: StateProbe,
    *,
    timeout: float = 90,
    interval: float = 2,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ReadinessResult:
    """Wait for running, non-unhealthy Compose state using an injectable probe."""

    deadline = clock() + timeout
    while True:
        try:
            observed = state_probe().get(service, {})
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            observed = {}
        state = observed.get("state", "unknown")
        health = observed.get("health", "")
        if state == "running" and health in {"", "healthy"}:
            return ReadinessResult(True, "ready")
        if clock() >= deadline:
            if state == "running" and health == "unhealthy":
                return ReadinessResult(False, "container reported unhealthy")
            if state == "running":
                return ReadinessResult(False, "container health did not become ready")
            return ReadinessResult(False, "container did not reach running state")
        sleep(min(interval, max(0.0, deadline - clock())))


def _readiness_targets(config: EnvDocument) -> dict[str, tuple[str, dict[str, str]]]:
    domain = config.get("DOMAIN")
    if not domain:
        raise ValueError("DOMAIN must be configured for core readiness")
    return {
        "traefik": ("http://127.0.0.1:8080/api/rawdata", {}),
        "jellyfin": ("http://127.0.0.1:8096/System/Info/Public", {}),
        "jellyseerr": ("http://127.0.0.1/api/v1/status", {"Host": f"jellyseerr.{domain}"}),
        "radarr": ("http://127.0.0.1/ping", {"Host": f"radarr.{domain}"}),
        "sonarr": ("http://127.0.0.1/ping", {"Host": f"sonarr.{domain}"}),
    }


def _diagnostics(
    states: Mapping[str, Mapping[str, str]], readiness: Mapping[str, ReadinessResult]
) -> list[dict[str, object]]:
    diagnostics: list[dict[str, object]] = []
    for service in CORE_SERVICES:
        observed = states.get(service, {})
        state = observed.get("state", "absent")
        health = observed.get("health", "")
        probe = readiness.get(service, ReadinessResult(False, "readiness was not observed"))
        running = state == "running"
        healthy = running and health in {"", "healthy"}
        diagnostics.append({
            "service": service,
            "desired_state": "running",
            "current_state": state if state in {"running", "exited", "restarting", "paused", "dead"} else "absent",
            "healthy": healthy,
            "ready": healthy and probe.ready,
            "reason": probe.reason if healthy else (
                "container reported unhealthy" if health == "unhealthy" else
                "container health is not ready" if running else "container is not running"
            ),
        })
    return diagnostics


def deploy_core(
    repository_root: str | Path,
    *,
    runner: CommandRunner | None = None,
    dry_run: bool = False,
    preflight_runner: Callable[[EnvDocument, str, object], PreflightReport] = run_preflight,
    http_waiter: Callable[..., ReadinessResult] = wait_for_http,
    container_waiter: Callable[..., ReadinessResult] = wait_for_container,
) -> dict[str, object]:
    """Reconcile the immutable core allowlist, retaining working partial startup."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    config = EnvDocument.load(root / ".env")
    prefix = compose_command(root)
    up_argv = [*prefix, "up", "--detach", *CORE_SERVICES]
    ps_argv = [*prefix, "ps", "--format", "json"]
    if dry_run:
        return {
            "status": "planned",
            "services": list(CORE_SERVICES),
            "commands": [ps_argv, up_argv],
            "read_only_commands": [ps_argv],
            "mutation_commands": [up_argv],
            "state_written": False,
        }

    state_path = root / ".homeflix" / "setup.json"
    state = SetupState.load(state_path)
    targets = _readiness_targets(config)
    try:
        initial_states = compose_ps(root, command_runner)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return {
            "status": "live_state_failed",
            "changed": False,
            "services": _diagnostics({}, {}),
            "checkpoint_recorded": False,
            "reason": "Compose service state could not be verified",
        }

    readiness: dict[str, ReadinessResult] = {}
    for service in CORE_SERVICES:
        observed = initial_states.get(service, {})
        if observed.get("state") == "running" and observed.get("health") in {"", "healthy"}:
            url, headers = targets[service]
            readiness[service] = http_waiter(url, headers=headers)
        else:
            readiness[service] = ReadinessResult(False, "readiness was not observed")
    if all(result.ready for result in readiness.values()):
        checkpoint_changed = not state.checkpoints.get("core_containers_started", False)
        if checkpoint_changed:
            state.checkpoints["core_containers_started"] = True
            state.save(state_path)
        return {
            "status": "already_ready",
            "changed": False,
            "services": _diagnostics(initial_states, readiness),
            "checkpoint_recorded": True,
        }

    # This phase-aware preflight is intentionally the final operation before mutation.
    report = preflight_runner(config, "core", command_runner)
    if not report.passed:
        return {
            "status": "preflight_failed",
            "changed": False,
            "preflight": report.to_dict(),
            "services": _diagnostics(initial_states, readiness),
            "checkpoint_recorded": False,
        }
    start = compose_up(root, CORE_SERVICES, command_runner)

    final_readiness: dict[str, ReadinessResult] = {}
    for service in CORE_SERVICES:
        container = container_waiter(service, lambda: compose_ps(root, command_runner))
        if not container.ready:
            final_readiness[service] = container
            continue
        url, headers = targets[service]
        final_readiness[service] = http_waiter(url, headers=headers)
    try:
        final_states = compose_ps(root, command_runner)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        final_states = {}
    diagnostics = _diagnostics(final_states, final_readiness)
    succeeded = start.returncode == 0 and all(item["ready"] for item in diagnostics)
    if succeeded:
        state.checkpoints["core_containers_started"] = True
        state.save(state_path)
    return {
        "status": "ready" if succeeded else "partial_failure",
        "changed": True,
        "services": diagnostics,
        "checkpoint_recorded": succeeded,
    }
