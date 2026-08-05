"""Live-state deployment and readiness reconciliation for the core stack."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Callable, Mapping, Sequence
from urllib import error, request

from .api import ArrClient, JellyfinClient, JellyseerrClient, read_api_key, read_settings_api_key
from .api.client import Transport, urllib_transport
from .command import CommandRunner
from .compose import CORE_SERVICES, compose_command, compose_ps, compose_up
from .envfile import EnvDocument
from .preflight import PreflightReport, run_preflight
from .state import SetupState


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reason: str


@dataclass(frozen=True)
class ArtifactIdentity:
    device: int
    inode: int
    size: int
    sha256: str
    mtime_ns: int = 0


@dataclass(frozen=True)
class DeploymentSnapshot:
    environment: ArtifactIdentity
    compose: ArtifactIdentity
    override: ArtifactIdentity | None
    data_root_identity: tuple[int, int] | None
    data_mount_record: tuple[str, str, str, str] | None


READINESS_TIMEOUT = 90.0

HttpProbe = Callable[[str, Mapping[str, str], float], bool]
StateProbe = Callable[[float], Mapping[str, Mapping[str, str]]]


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

    deadline = clock() + max(0.0, timeout)
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return ReadinessResult(False, "HTTP readiness timed out")
        if probe(url, headers or {}, min(5.0, remaining)):
            return ReadinessResult(True, "ready")
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

    deadline = clock() + max(0.0, timeout)
    observed: Mapping[str, str] = {}
    while True:
        if clock() >= deadline:
            state = observed.get("state", "unknown")
            health = observed.get("health", "")
            if state == "running" and health == "unhealthy":
                return ReadinessResult(False, "container reported unhealthy")
            if state == "running":
                return ReadinessResult(False, "container health did not become ready")
            return ReadinessResult(False, "container did not reach running state")
        try:
            observed = state_probe(max(0.0, deadline - clock())).get(service, {})
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            observed = {}
        state = observed.get("state", "unknown")
        health = observed.get("health", "")
        if state == "running" and health in {"", "healthy"}:
            return ReadinessResult(True, "ready")
        sleep(min(interval, max(0.0, deadline - clock())))


def _read_artifact(
    path: Path, *, optional: bool = False
) -> tuple[bytes, ArtifactIdentity] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError("deployment artifact is unavailable") from None
    except OSError as error:
        raise ValueError("deployment artifact cannot be verified") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("deployment artifact must be a regular file")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 128 * 1024):
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ValueError("deployment artifact changed while being read")
        identity = ArtifactIdentity(
            before.st_dev, before.st_ino, before.st_size, digest.hexdigest(), before.st_mtime_ns
        )
        return b"".join(chunks), identity
    finally:
        os.close(descriptor)


def _artifact_identity(path: Path, *, optional: bool = False) -> ArtifactIdentity | None:
    captured = _read_artifact(path, optional=optional)
    return None if captured is None else captured[1]


def _unescape_mount(value: str) -> str:
    for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value


def _data_mount_snapshot(config: EnvDocument) -> tuple[tuple[int, int], tuple[str, str, str, str]] | tuple[None, None]:
    configured = config.get("DATA_ROOT")
    if not configured:
        return None, None
    path = Path(configured).expanduser()
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError("DATA_ROOT identity cannot be verified")
        identity = path.stat()
        device = f"{os.major(identity.st_dev)}:{os.minor(identity.st_dev)}"
        candidates: list[tuple[Path, tuple[str, str, str, str]]] = []
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            separator = fields.index("-")
            mount_point = Path(_unescape_mount(fields[4]))
            try:
                path.relative_to(mount_point)
            except ValueError:
                continue
            if fields[2] != device:
                continue
            candidates.append((mount_point, (fields[2], str(mount_point), fields[separator + 1], fields[separator + 2])))
        if not candidates:
            raise ValueError("DATA_ROOT mount identity cannot be verified")
        record = max(candidates, key=lambda item: len(item[0].parts))[1]
    except (OSError, ValueError, IndexError) as error:
        if isinstance(error, ValueError) and str(error).startswith("DATA_ROOT"):
            raise
        raise ValueError("DATA_ROOT mount identity cannot be verified") from error
    return (identity.st_dev, identity.st_ino), record


def capture_deployment_snapshot(repository_root: str | Path, config: EnvDocument) -> DeploymentSnapshot:
    """Capture mutation inputs; a final adjacent comparison narrows, but cannot remove, unmount races."""

    root = Path(repository_root).resolve()
    data_identity, mount_record = _data_mount_snapshot(config)
    environment = _artifact_identity(root / ".env")
    compose = _artifact_identity(root / "docker-compose.yml")
    assert environment is not None and compose is not None
    return DeploymentSnapshot(
        environment,
        compose,
        _artifact_identity(root / "docker-compose.override.yml", optional=True),
        data_identity,
        mount_record,
    )


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


def _initial_readiness(
    states: Mapping[str, Mapping[str, str]],
    targets: Mapping[str, tuple[str, dict[str, str]]],
    http_waiter: Callable[..., ReadinessResult],
    *,
    deadline: float,
    clock: Callable[[], float],
) -> dict[str, ReadinessResult]:
    readiness: dict[str, ReadinessResult] = {}
    if not all(
        states.get(service, {}).get("state") == "running"
        and states.get(service, {}).get("health") in {"", "healthy"}
        for service in CORE_SERVICES
    ):
        return {
            service: ReadinessResult(False, "readiness was not observed")
            for service in CORE_SERVICES
        }
    remaining_services = len(CORE_SERVICES)
    for service in CORE_SERVICES:
        observed = states.get(service, {})
        remaining = max(0.0, deadline - clock())
        fair_share = remaining / remaining_services
        remaining_services -= 1
        if observed.get("state") == "running" and observed.get("health") in {"", "healthy"}:
            url, headers = targets[service]
            readiness[service] = http_waiter(url, headers=headers, timeout=fair_share)
        else:
            readiness[service] = ReadinessResult(False, "readiness was not observed")
    return readiness


def _post_start_readiness(
    targets: Mapping[str, tuple[str, dict[str, str]]],
    state_probe: StateProbe,
    http_waiter: Callable[..., ReadinessResult],
    container_waiter: Callable[..., ReadinessResult],
    *,
    deadline: float,
    clock: Callable[[], float],
) -> dict[str, ReadinessResult]:
    readiness: dict[str, ReadinessResult] = {}
    remaining_calls = len(CORE_SERVICES) * 2
    for service in CORE_SERVICES:
        remaining = max(0.0, deadline - clock())
        container_timeout = remaining / remaining_calls
        remaining_calls -= 1
        container = container_waiter(service, state_probe, timeout=container_timeout)
        if not container.ready:
            readiness[service] = container
            remaining_calls -= 1
            continue
        remaining = max(0.0, deadline - clock())
        http_timeout = remaining / remaining_calls
        remaining_calls -= 1
        url, headers = targets[service]
        readiness[service] = http_waiter(url, headers=headers, timeout=http_timeout)
    return readiness


def _load_private_environment(path: Path) -> EnvDocument:
    captured = _read_artifact(path)
    assert captured is not None
    raw, identity = captured
    try:
        metadata = path.stat(follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != (identity.device, identity.inode):
            raise ValueError("environment file changed while being read")
        if metadata.st_mode & stat.S_IROTH:
            raise ValueError("environment file permissions are unsafe")
        document = EnvDocument.parse(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError("environment configuration is not valid UTF-8") from None
    document.source_path = path
    return document


def configure_core(
    repository_root: str | Path,
    *,
    transports: Mapping[str, Transport] | None = None,
    api_key_reader: Callable[[str | Path], str] = read_api_key,
    settings_key_reader: Callable[[str | Path], str] = read_settings_api_key,
) -> dict[str, object]:
    """Reconcile core application APIs after the explicit container checkpoint."""

    root = Path(repository_root).resolve()
    state = SetupState.load(root / ".homeflix" / "setup.json")
    if state.checkpoints.get("core_containers_started") is not True:
        raise ValueError("core containers must be live before API configuration")
    config = _load_private_environment(root / ".env")
    required = ("JELLYFIN_ADMIN_USER", "JELLYFIN_ADMIN_PASSWORD", "CONFIG_ROOT", "QUALITY_PROFILE", "DOMAIN")
    values = {key: config.get(key) for key in required}
    if any(not value for value in values.values()):
        raise ValueError("required core API configuration is missing")
    config_root = Path(values["CONFIG_ROOT"] or "")
    if not config_root.is_absolute():
        raise ValueError("CONFIG_ROOT must be absolute")
    domain = values["DOMAIN"] or ""
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", domain):
        raise ValueError("DOMAIN is invalid")
    selected = values["QUALITY_PROFILE"] or ""
    chosen_transports = dict(transports or {})
    jellyfin = JellyfinClient(transport=chosen_transports.get("jellyfin", urllib_transport))
    created_admin = jellyfin.initialize(values["JELLYFIN_ADMIN_USER"] or "", values["JELLYFIN_ADMIN_PASSWORD"] or "")
    libraries = jellyfin.ensure_libraries()

    arr_results: dict[str, dict[str, object]] = {}
    runtime: dict[str, tuple[str, dict[str, object], dict[str, object]]] = {}
    for service, media_path in (("radarr", "/data/media/movies"), ("sonarr", "/data/media/tv")):
        key = api_key_reader(config_root / service / "config.xml")
        client = ArrClient(
            service, "http://127.0.0.1", key,
            headers={"Host": f"{service}.{domain}"},
            transport=chosen_transports.get(service, urllib_transport),
        )
        result = client.configure(selected, media_path)
        arr_results[service] = result
        if client.selected_profile is None or client.selected_root is None:
            raise RuntimeError("Arr configuration did not produce runtime selections")
        runtime[service] = (key, client.selected_profile, client.selected_root)

    jellyseerr = JellyseerrClient(
        headers={"Host": f"jellyseerr.{domain}"},
        transport=chosen_transports.get("jellyseerr", urllib_transport),
    )
    was_initialized = jellyseerr.initialized()
    if not was_initialized:
        jellyseerr.authenticate_jellyfin(values["JELLYFIN_ADMIN_USER"] or "", values["JELLYFIN_ADMIN_PASSWORD"] or "")
    jellyseerr.authorize(settings_key_reader(config_root / "jellyseerr" / "settings.json"))
    connected: dict[str, bool] = {}
    for service in ("radarr", "sonarr"):
        key, profile, media_root = runtime[service]
        connected[service] = jellyseerr.ensure_arr(service, key, profile, media_root)
    initialized_now = jellyseerr.finish()
    return {
        "status": "configured",
        "jellyfin": {"administrator_created": created_admin, "libraries": libraries},
        "radarr": arr_results["radarr"],
        "sonarr": arr_results["sonarr"],
        "jellyseerr": {"radarr_changed": connected["radarr"], "sonarr_changed": connected["sonarr"], "initialized": was_initialized or initialized_now},
    }


def deploy_core(
    repository_root: str | Path,
    *,
    runner: CommandRunner | None = None,
    dry_run: bool = False,
    preflight_runner: Callable[[EnvDocument, str, object], PreflightReport] = run_preflight,
    http_waiter: Callable[..., ReadinessResult] = wait_for_http,
    container_waiter: Callable[..., ReadinessResult] = wait_for_container,
    readiness_timeout: float = READINESS_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    snapshotter: Callable[[str | Path, EnvDocument], DeploymentSnapshot] = capture_deployment_snapshot,
) -> dict[str, object]:
    """Reconcile core under one readiness deadline shared by initial and post-start checks."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    environment_path = root / ".env"
    captured_environment = _read_artifact(environment_path)
    assert captured_environment is not None
    environment_bytes, environment_identity = captured_environment
    try:
        config = EnvDocument.parse(environment_bytes.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError("environment configuration is not valid UTF-8") from error
    config.source_path = environment_path
    project_name = config.get("COMPOSE_PROJECT_NAME")
    prefix = compose_command(root, project_name=project_name)
    up_argv = [*prefix, "up", "--detach", "--no-deps", *CORE_SERVICES]
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
        baseline_snapshot = snapshotter(root, config)
    except (OSError, ValueError):
        return {
            "status": "deployment_snapshot_failed",
            "changed": False,
            "services": _diagnostics({}, {}),
            "checkpoint_recorded": False,
            "reason": "Deployment inputs could not be verified",
        }
    if baseline_snapshot.environment != environment_identity:
        return {
            "status": "config_drift",
            "changed": False,
            "services": _diagnostics({}, {}),
            "checkpoint_recorded": False,
            "reason": "Environment configuration changed before reconciliation",
        }
    readiness_deadline = clock() + max(0.0, readiness_timeout)
    initial_ps_timeout = max(0.0, readiness_deadline - clock())
    if initial_ps_timeout <= 0:
        return {
            "status": "live_state_failed",
            "changed": False,
            "services": _diagnostics({}, {}),
            "checkpoint_recorded": False,
            "reason": "Compose service state could not be verified within the readiness deadline",
        }
    try:
        initial_states = compose_ps(
            root, command_runner, project_name=project_name, timeout=initial_ps_timeout
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return {
            "status": "live_state_failed",
            "changed": False,
            "services": _diagnostics({}, {}),
            "checkpoint_recorded": False,
            "reason": "Compose service state could not be verified",
        }

    readiness = _initial_readiness(
        initial_states, targets, http_waiter, deadline=readiness_deadline, clock=clock
    )
    if all(result.ready for result in readiness.values()):
        diagnostics = _diagnostics(initial_states, readiness)
        if not state.checkpoints.get("core_containers_started", False):
            state.checkpoints["core_containers_started"] = True
            try:
                state.save(state_path)
            except (OSError, ValueError):
                return {
                    "status": "checkpoint_failed",
                    "changed": False,
                    "services": diagnostics,
                    "checkpoint_recorded": False,
                    "reason": "Verified core readiness could not be checkpointed",
                }
        return {
            "status": "already_ready",
            "changed": False,
            "services": diagnostics,
            "checkpoint_recorded": True,
        }

    report = preflight_runner(config, "core", command_runner)
    if not report.passed:
        return {
            "status": "preflight_failed",
            "changed": False,
            "preflight": report.to_dict(),
            "services": _diagnostics(initial_states, readiness),
            "checkpoint_recorded": False,
        }
    try:
        after_preflight = snapshotter(root, config)
    except (OSError, ValueError):
        return {
            "status": "deployment_drift",
            "changed": False,
            "services": _diagnostics(initial_states, readiness),
            "checkpoint_recorded": False,
            "reason": "Deployment inputs changed or became unverifiable during preflight",
            "safety_note": "External mount changes after the final drift guard cannot be eliminated",
        }
    if baseline_snapshot != after_preflight:
        return {
            "status": "deployment_drift",
            "changed": False,
            "services": _diagnostics(initial_states, readiness),
            "checkpoint_recorded": False,
            "reason": "Deployment inputs changed during preflight",
            "safety_note": "External mount changes after the final drift guard cannot be eliminated",
        }
    # Keep this final snapshot comparison immediately adjacent to the only mutation.
    startup_process_failed = False
    start_returncode = 1
    try:
        start = compose_up(
            root, CORE_SERVICES, command_runner, project_name=project_name
        )
        start_returncode = start.returncode
    except (OSError, subprocess.SubprocessError):
        # The command may have started some services before the local process failed.
        # Reconcile live state below without exposing the exception, argv, or output.
        startup_process_failed = True

    last_states = initial_states

    def state_probe(timeout: float) -> Mapping[str, Mapping[str, str]]:
        nonlocal last_states
        if timeout <= 0:
            raise RuntimeError("readiness deadline exhausted")
        observed = compose_ps(
            root, command_runner, project_name=project_name, timeout=timeout
        )
        last_states = observed
        return observed

    final_readiness = _post_start_readiness(
        targets,
        state_probe,
        http_waiter,
        container_waiter,
        deadline=readiness_deadline,
        clock=clock,
    )
    state_verification_failed = False
    try:
        final_timeout = max(0.0, readiness_deadline - clock())
        if final_timeout <= 0:
            raise RuntimeError("readiness deadline exhausted")
        final_states = compose_ps(
            root, command_runner, project_name=project_name, timeout=final_timeout
        )
        last_states = final_states
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        state_verification_failed = True
        final_states = last_states
    diagnostics = _diagnostics(final_states, final_readiness)
    succeeded = (
        not startup_process_failed
        and not state_verification_failed
        and start_returncode == 0
        and all(item["ready"] for item in diagnostics)
    )
    if succeeded:
        state.checkpoints["core_containers_started"] = True
        try:
            state.save(state_path)
        except (OSError, ValueError):
            return {
                "status": "checkpoint_failed",
                "changed": True,
                "services": diagnostics,
                "checkpoint_recorded": False,
                "reason": "Verified core readiness could not be checkpointed",
                "safety_note": "External mount changes after the final drift guard cannot be eliminated",
            }
    return {
        "status": (
            "ready" if succeeded else
            "state_verification_failed" if state_verification_failed else
            "startup_failed" if startup_process_failed else
            "partial_failure"
        ),
        "changed": True,
        "services": diagnostics,
        "checkpoint_recorded": succeeded,
        **(
            {"reason": "Final Compose service state could not be verified"}
            if state_verification_failed else
            {"reason": "Compose startup command did not complete"}
            if startup_process_failed else {}
        ),
        "safety_note": "External mount changes after the final drift guard cannot be eliminated",
    }
