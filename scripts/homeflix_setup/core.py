"""Live-state deployment and readiness reconciliation for the core stack."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Callable, Mapping, Sequence
from urllib import error, request
import uuid

from .api import ApiError, ArrClient, JellyfinClient, JellyseerrClient, read_api_key, read_settings_api_key
from .api.client import Transport, urllib_transport
from .command import CommandRunner
from .compose import CORE_SERVICES, compose_command, compose_inventory, compose_ps, compose_up
from .envfile import EnvDocument
from .preflight import PreflightReport, run_preflight
from .state import SetupState


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reason: str


@dataclass(frozen=True)
class CoreReadinessEvidence:
    repository_root: Path
    project_name: str
    snapshot: DeploymentSnapshot
    services: tuple[str, ...]
    deadline: float


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
NON_CORE_SERVICES = ("gluetun", "qbittorrent", "nzbget", "prowlarr", "lidarr", "bazarr")
QUICKSYNC_DEVICE = "/dev/dri"

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


def _attest_core_readiness(
    root: Path,
    config: EnvDocument,
    runner: CommandRunner,
    *,
    deadline: float,
    clock: Callable[[], float],
    http_waiter: Callable[..., ReadinessResult],
) -> CoreReadinessEvidence:
    """Establish exact, current, private readiness evidence before API mutation."""
    project_name = config.get("COMPOSE_PROJECT_NAME")
    if project_name != "homeflix":
        raise ValueError("expected Compose project identity was not configured")
    snapshot = capture_deployment_snapshot(root, config)
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("core operation deadline exhausted")
    inventory = compose_inventory(root, runner, project_name=project_name, timeout=remaining)
    if any(item["project"] != "homeflix" for item in inventory):
        raise ValueError("live Compose project identity did not match")
    if tuple(sorted(item["service"] for item in inventory)) != tuple(sorted(CORE_SERVICES)):
        raise ValueError("live Compose core inventory was not exact")
    states = {item["service"]: item for item in inventory}
    readiness = _initial_readiness(
        states, _readiness_targets(config), http_waiter, deadline=deadline, clock=clock
    )
    if not all(result.ready for result in readiness.values()):
        raise ValueError("live core readiness was not established")
    if capture_deployment_snapshot(root, config) != snapshot:
        raise ValueError("deployment inputs changed during readiness attestation")
    return CoreReadinessEvidence(root, project_name, snapshot, CORE_SERVICES, deadline)


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
        if fair_share <= 0:
            readiness[service] = ReadinessResult(False, "readiness deadline exhausted")
        elif observed.get("state") == "running" and observed.get("health") in {"", "healthy"}:
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
        if container_timeout <= 0:
            readiness[service] = ReadinessResult(False, "readiness deadline exhausted")
            remaining_calls -= 1
            continue
        container = container_waiter(service, state_probe, timeout=container_timeout)
        if not container.ready:
            readiness[service] = container
            remaining_calls -= 1
            continue
        remaining = max(0.0, deadline - clock())
        http_timeout = remaining / remaining_calls
        remaining_calls -= 1
        if http_timeout <= 0:
            readiness[service] = ReadinessResult(False, "readiness deadline exhausted")
            continue
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
        if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
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
    runner: CommandRunner | None = None,
    http_waiter: Callable[..., ReadinessResult] = wait_for_http,
    readiness_timeout: float = READINESS_TIMEOUT,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    api_key_reader: Callable[[str | Path, str, int], str] = read_api_key,
    settings_key_reader: Callable[[str | Path, int], str] = read_settings_api_key,
) -> dict[str, object]:
    """Reconcile core application APIs using caller-established live readiness."""

    root = Path(repository_root).resolve()
    config = _load_private_environment(root / ".env")
    required = ("JELLYFIN_ADMIN_USER", "JELLYFIN_ADMIN_PASSWORD", "CONFIG_ROOT", "PUID", "QUALITY_PROFILE", "DOMAIN")
    values = {key: config.get(key) for key in required}
    if any(not value for value in values.values()):
        raise ValueError("required core API configuration is missing")
    config_root = Path(values["CONFIG_ROOT"] or "")
    if not config_root.is_absolute():
        raise ValueError("CONFIG_ROOT must be absolute")
    puid_text = values["PUID"] or ""
    if re.fullmatch(r"[0-9]+", puid_text) is None or int(puid_text) > 2**32 - 1:
        raise ValueError("PUID is invalid")
    expected_uid = int(puid_text)
    domain = values["DOMAIN"] or ""
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", domain):
        raise ValueError("DOMAIN is invalid")
    selected = values["QUALITY_PROFILE"] or ""
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
    _attest_core_readiness(
        root, config, runner or CommandRunner(), deadline=operation_deadline,
        clock=clock, http_waiter=http_waiter,
    )
    runtime_keys = {
        service: api_key_reader(config_root, service, expected_uid)
        for service in ("radarr", "sonarr")
    }
    chosen_transports = dict(transports or {})
    if clock() >= operation_deadline:
        raise TimeoutError("core operation deadline exhausted")
    jellyfin = JellyfinClient(transport=chosen_transports.get("jellyfin", urllib_transport), deadline=operation_deadline, clock=clock)
    created_admin, libraries = jellyfin.reconcile(values["JELLYFIN_ADMIN_USER"] or "", values["JELLYFIN_ADMIN_PASSWORD"] or "")

    arr_results: dict[str, dict[str, object]] = {}
    runtime: dict[str, tuple[str, dict[str, object], dict[str, object]]] = {}
    for service, media_path in (("radarr", "/data/media/movies"), ("sonarr", "/data/media/tv")):
        key = runtime_keys[service]
        client = ArrClient(
            service, "http://127.0.0.1", key,
            headers={"Host": f"{service}.{domain}"},
            transport=chosen_transports.get(service, urllib_transport), deadline=operation_deadline, clock=clock,
        )
        result = client.configure(selected, media_path)
        arr_results[service] = result
        if client.selected_profile is None or client.selected_root is None:
            raise RuntimeError("Arr configuration did not produce runtime selections")
        runtime[service] = (key, client.selected_profile, client.selected_root)

    jellyseerr = JellyseerrClient(
        headers={"Host": f"jellyseerr.{domain}"},
        transport=chosen_transports.get("jellyseerr", urllib_transport), deadline=operation_deadline, clock=clock,
    )
    was_initialized = jellyseerr.initialized()
    if not was_initialized:
        jellyseerr.authenticate_jellyfin(values["JELLYFIN_ADMIN_USER"] or "", values["JELLYFIN_ADMIN_PASSWORD"] or "")
    jellyseerr.authorize(settings_key_reader(config_root, expected_uid))
    jellyseerr.verify_jellyfin()
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


_CHECK_STATUSES = ("pass", "warning", "failure", "not-applicable", "unknown")


def _check(domain: str, passed: bool | None, reason: str, *, status: str | None = None) -> dict[str, object]:
    if status is None:
        status = "pass" if passed is True else "failure" if passed is False else "unknown"
    if status not in _CHECK_STATUSES:
        raise ValueError("verification check status is invalid")
    return {"domain": domain, "status": status, "reason": reason}


def _application_check(
    domain: str,
    observed: Mapping[str, object],
    required: Sequence[str],
    success: str,
    mismatch: str,
) -> dict[str, object]:
    if not required or any(name not in observed or not isinstance(observed[name], bool) for name in required):
        return _check(domain, None, f"{domain} state could not be inspected")
    exact = all(observed[name] is True for name in required)
    return _check(domain, exact, success if exact else mismatch)


def _inspect_quicksync(root: Path, runner: CommandRunner, project_name: str, *, deadline: float | None = None, clock: Callable[[], float] = time.monotonic) -> bool | None:
    prefix = compose_command(root, project_name=project_name)
    remaining = 30.0 if deadline is None else deadline - clock()
    if remaining <= 0:
        raise TimeoutError("core operation deadline exhausted")
    rendered = runner.run((*prefix, "config", "--format", "json"), check=False, timeout=min(30.0, remaining))
    if rendered.returncode:
        raise RuntimeError("rendered Compose configuration is unavailable")
    try:
        config = json.loads(rendered.stdout)
        devices = config["services"]["jellyfin"].get("devices", [])
    except (json.JSONDecodeError, KeyError, TypeError):
        raise ValueError("rendered Compose configuration was malformed") from None
    if not isinstance(devices, list):
        raise ValueError("rendered Compose device configuration was malformed")
    if not devices:
        return None
    rendered_ok = len(devices) == 1 and (
        (
            isinstance(devices[0], dict)
            and devices[0].get("source") == QUICKSYNC_DEVICE
            and devices[0].get("target") == QUICKSYNC_DEVICE
        )
        or devices[0] == f"{QUICKSYNC_DEVICE}:{QUICKSYNC_DEVICE}"
    )
    if not rendered_ok:
        return False
    remaining = 30.0 if deadline is None else deadline - clock()
    if remaining <= 0:
        raise TimeoutError("core operation deadline exhausted")
    container = runner.run((*prefix, "ps", "--quiet", "jellyfin"), check=False, timeout=min(30.0, remaining))
    identifier = container.stdout.strip()
    if container.returncode or re.fullmatch(r"[a-f0-9]{12,64}", identifier) is None:
        return False
    remaining = 30.0 if deadline is None else deadline - clock()
    if remaining <= 0:
        raise TimeoutError("core operation deadline exhausted")
    inspected = runner.run(
        ("docker", "inspect", "--format", "{{json .HostConfig.Devices}}|{{json .State.Running}}", identifier),
        check=False, timeout=min(30.0, remaining),
    )
    if inspected.returncode:
        raise RuntimeError("live container device mapping is unavailable")
    try:
        devices_text, running_text = inspected.stdout.strip().split("|", 1)
        live_devices = json.loads(devices_text)
        running = json.loads(running_text)
    except (json.JSONDecodeError, ValueError):
        raise ValueError("live container device mapping was malformed") from None
    live_ok = running is True and isinstance(live_devices, list) and len(live_devices) == 1 and any(
        isinstance(item, dict)
        and item.get("PathOnHost") == QUICKSYNC_DEVICE
        and item.get("PathInContainer") == QUICKSYNC_DEVICE
        for item in live_devices
    )
    return rendered_ok and live_ok


def _evaluate_runtime_contract(
    root: Path,
    runner: CommandRunner,
    project_name: str | None,
    *,
    timeout: float,
) -> Mapping[str, object]:
    from .contract import evaluate_stack_contract

    if timeout <= 0:
        raise TimeoutError("core operation deadline exhausted")
    rendered = runner.run(
        (*compose_command(root, project_name=project_name), "config", "--format", "json"),
        check=False,
        timeout=min(30.0, timeout),
    )
    if rendered.returncode:
        raise RuntimeError("rendered Compose configuration is unavailable")
    try:
        mapping = json.loads(rendered.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("rendered Compose configuration was malformed") from error
    if not isinstance(mapping, Mapping):
        raise ValueError("rendered Compose configuration was malformed")
    return evaluate_stack_contract(mapping)


def _contract_check(report: Mapping[str, object] | None) -> dict[str, object]:
    if report is None:
        return _check("stack_contract", None, "stack contract could not be inspected")
    findings = report.get("findings")
    if not isinstance(findings, list):
        return _check("stack_contract", None, "stack contract could not be inspected")
    if not findings:
        return _check("stack_contract", True, "stack contract holds")
    codes = []
    for item in findings:
        if isinstance(item, Mapping) and isinstance(item.get("code"), str) and item["code"].isascii():
            codes.append(item["code"])
    reason = "stack contract findings present"
    if codes:
        reason = "stack contract findings: " + ", ".join(dict.fromkeys(codes))
    return _check("stack_contract", False, reason)


def _probe_hardlink_outcome(config: EnvDocument) -> bool | None:
    configured = config.get("DATA_ROOT")
    if not configured:
        return None
    data_root = Path(configured).expanduser()
    torrents = data_root / "torrents"
    media = data_root / "media"
    if not torrents.is_dir() or not media.is_dir():
        return None
    token = uuid.uuid4().hex
    source = torrents / f".homeflix-verify-{token}"
    link = media / f".homeflix-verify-{token}"
    created_source = False
    created_link = False
    try:
        descriptor = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created_source = True
        try:
            os.write(descriptor, b"homeflix-verify\n")
            source_stat = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        os.link(source, link)
        created_link = True
        link_stat = os.lstat(link)
        return (source_stat.st_dev, source_stat.st_ino) == (link_stat.st_dev, link_stat.st_ino)
    except OSError:
        return False
    finally:
        for path, created in ((link, created_link), (source, created_source)):
            if not created:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                pass


def _inspect_data_mount(
    root: Path,
    config: EnvDocument,
    runner: CommandRunner,
    project_name: str | None,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> bool | None:
    configured = config.get("DATA_ROOT")
    if not configured:
        return None
    try:
        data_root = Path(configured).expanduser()
        if not data_root.is_absolute():
            return None
        expected = data_root.resolve(strict=True).stat()
        expected_id = (expected.st_dev, expected.st_ino)
    except OSError:
        return None

    prefix = compose_command(root, project_name=project_name)
    observed = 0
    for service in ("radarr", "sonarr"):
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("core operation deadline exhausted")
        container = runner.run((*prefix, "ps", "--quiet", service), check=False, timeout=min(30.0, remaining))
        identifier = container.stdout.strip()
        if container.returncode or re.fullmatch(r"[a-f0-9]{12,64}", identifier) is None:
            return None
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("core operation deadline exhausted")
        inspected = runner.run(
            ("docker", "inspect", "--format", "{{json .Mounts}}", identifier),
            check=False, timeout=min(30.0, remaining),
        )
        if inspected.returncode:
            return None
        try:
            mounts = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(mounts, list):
            return None
        matches = [
            item for item in mounts
            if isinstance(item, Mapping)
            and item.get("Type", item.get("type")) in {None, "bind"}
            and item.get("Destination", item.get("destination", item.get("Target", item.get("target")))) == "/data"
        ]
        if len(matches) != 1:
            return False
        source = matches[0].get("Source", matches[0].get("source"))
        if not isinstance(source, str) or not source:
            return False
        try:
            live = Path(source).resolve(strict=True).stat()
        except OSError:
            return False
        if (live.st_dev, live.st_ino) != expected_id:
            return False
        observed += 1
    return True if observed == 2 else None


def _inspect_docker(runner: CommandRunner, *, timeout: float) -> bool | None:
    if timeout <= 0:
        raise TimeoutError("core operation deadline exhausted")
    try:
        inspected = runner.run(("docker", "info"), check=False, timeout=min(10.0, timeout))
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    return inspected.returncode == 0


def verify_core(
    repository_root: str | Path,
    *,
    runner: CommandRunner | None = None,
    transports: Mapping[str, Transport] | None = None,
    api_key_reader: Callable[[str | Path, str, int], str] = read_api_key,
    settings_key_reader: Callable[[str | Path, int], str] = read_settings_api_key,
    http_waiter: Callable[..., ReadinessResult] = wait_for_http,
    readiness_timeout: float = READINESS_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    deadline: float | None = None,
    quicksync_inspector: Callable[..., bool | None] = _inspect_quicksync,
    docker_inspector: Callable[..., bool | None] = _inspect_docker,
    contract_evaluator: Callable[..., Mapping[str, object]] | None = None,
    mount_inspector: Callable[..., bool | None] = _inspect_data_mount,
    hardlink_prober: Callable[..., bool | None] = _probe_hardlink_outcome,
) -> dict[str, object]:
    """Inspect live core state. Checkpoints are never consulted or changed."""
    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
    checks: list[dict[str, object]] = []

    def record_docker(observed: bool | None) -> None:
        if observed is True:
            checks.append(_check("docker", True, "docker daemon is reachable"))
        elif observed is False:
            checks.append(_check("docker", False, "docker daemon is unavailable"))
        else:
            checks.append(_check("docker", None, "docker daemon could not be inspected"))

    try:
        if docker_inspector is _inspect_docker:
            remaining = operation_deadline - clock()
            if remaining <= 0:
                raise TimeoutError("core operation deadline exhausted")
            docker = docker_inspector(command_runner, timeout=remaining)
        else:
            if clock() >= operation_deadline:
                raise TimeoutError("core operation deadline exhausted")
            docker = docker_inspector(root, command_runner, "homeflix")
        record_docker(docker)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        record_docker(None)

    try:
        config = _load_private_environment(root / ".env")
        project_name = config.get("COMPOSE_PROJECT_NAME")
        project_ok = project_name == "homeflix"
    except (OSError, RuntimeError, ValueError):
        config = None
        project_name = None
        project_ok = False

    try:
        remaining = operation_deadline - clock()
        if remaining <= 0:
            raise TimeoutError("core operation deadline exhausted")
        if contract_evaluator is None:
            if config is None:
                raise ValueError("environment configuration is unavailable")
            report = _evaluate_runtime_contract(
                root, command_runner, project_name, timeout=remaining,
            )
        else:
            report = contract_evaluator(root)
        checks.append(_contract_check(report))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError, TimeoutError):
        checks.append(_contract_check(None))

    try:
        if config is None:
            raise ValueError("environment configuration is unavailable")
        remaining = operation_deadline - clock()
        if remaining <= 0:
            raise TimeoutError("core operation deadline exhausted")
        inventory = compose_inventory(root, command_runner, project_name=project_name, timeout=remaining)
        observed_services = {item["service"] for item in inventory}
        if not observed_services.issubset(set(CORE_SERVICES) | set(NON_CORE_SERVICES)):
            raise ValueError("Compose service inventory contained an unknown project service")
        projects_ok = bool(inventory) and all(item["project"] == "homeflix" for item in inventory)
        checks.append(_check("compose_project", project_ok and projects_ok, "expected project scope observed" if project_ok and projects_ok else "expected project scope was not observed"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        exhausted = clock() >= operation_deadline
        if not any(item["domain"] == "docker" for item in checks):
            record_docker(None)
        if not any(item["domain"] == "stack_contract" for item in checks):
            checks.append(_contract_check(None))
        scope_reason = "time budget exhausted" if exhausted else "project scope could not be inspected"
        service_reason = "time budget exhausted" if exhausted else "service readiness could not be inspected"
        inventory_reason = "time budget exhausted" if exhausted else "project service inventory could not be inspected"
        app_reason = "time budget exhausted" if exhausted else "state could not be inspected"
        quicksync_reason = "time budget exhausted" if exhausted else "device selection could not be inspected"
        checks.append(_check("compose_project", None, scope_reason))
        checks.extend(_check(f"service:{service}", None, service_reason) for service in CORE_SERVICES)
        checks.append(_check("acquisition_absent", None, inventory_reason))
        checks.append(_check("mount", None, "time budget exhausted" if exhausted else "data mount identity could not be inspected"))
        checks.append(_check("hardlink_outcome", None, "time budget exhausted" if exhausted else "hardlink outcome could not be inspected"))
        checks.extend(_check(domain, None, f"{domain} {app_reason}") for domain in ("jellyfin", "radarr", "sonarr", "jellyseerr"))
        checks.append(_check("quicksync", None, quicksync_reason))
        return {"status": "failed", "passed": False, "checks": checks}

    by_service = {item["service"]: item for item in inventory}
    core_states = {name: by_service.get(name, {}) for name in CORE_SERVICES}
    try:
        targets = _readiness_targets(config)
        readiness = _initial_readiness(core_states, targets, http_waiter, deadline=operation_deadline, clock=clock)
        for service in CORE_SERVICES:
            item = core_states[service]
            known = item.get("state") == "running" and item.get("health") in {"", "healthy"}
            passed = known and readiness[service].ready
            checks.append(_check(f"service:{service}", passed, "service is healthy and ready" if passed else "service is not healthy and ready"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        for service in CORE_SERVICES:
            checks.append(_check(f"service:{service}", None, "service readiness could not be inspected"))

    present_non_core = sorted(name for name in NON_CORE_SERVICES if name in by_service)
    absent = not present_non_core
    checks.append(_check("acquisition_absent", absent, "acquisition services are absent" if absent else "Non-core project services present: " + ", ".join(present_non_core)))

    try:
        if mount_inspector is _inspect_data_mount:
            mount = mount_inspector(
                root, config, command_runner, project_name,
                deadline=operation_deadline, clock=clock,
            )
        else:
            if clock() >= operation_deadline:
                raise TimeoutError("core operation deadline exhausted")
            mount = mount_inspector(root, command_runner, "homeflix")
        if mount is True:
            checks.append(_check("mount", True, "DATA_ROOT and live /data binds share one identity"))
        elif mount is False:
            checks.append(_check("mount", False, "DATA_ROOT and live /data binds disagree or are missing"))
        else:
            checks.append(_check("mount", None, "data mount identity could not be inspected"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        reason = "time budget exhausted" if clock() >= operation_deadline else "data mount identity could not be inspected"
        checks.append(_check("mount", None, reason))

    try:
        if hardlink_prober is _probe_hardlink_outcome:
            if clock() >= operation_deadline:
                raise TimeoutError("core operation deadline exhausted")
            hardlink = hardlink_prober(config)
        else:
            if clock() >= operation_deadline:
                raise TimeoutError("core operation deadline exhausted")
            hardlink = hardlink_prober(root, command_runner, "homeflix")
        if hardlink is True:
            checks.append(_check("hardlink_outcome", True, "probe shared one device and inode"))
        elif hardlink is False:
            checks.append(_check("hardlink_outcome", False, "probe did not share one device and inode"))
        else:
            checks.append(_check("hardlink_outcome", None, "hardlink outcome could not be inspected"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        reason = "time budget exhausted" if clock() >= operation_deadline else "hardlink outcome could not be inspected"
        checks.append(_check("hardlink_outcome", None, reason))

    values = {key: config.get(key) for key in ("JELLYFIN_ADMIN_USER", "JELLYFIN_ADMIN_PASSWORD", "CONFIG_ROOT", "PUID", "QUALITY_PROFILE")}
    chosen = dict(transports or {})
    runtime: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    try:
        if any(not value for value in values.values()) or re.fullmatch(r"[0-9]+", values["PUID"] or "") is None:
            raise ValueError("required verification configuration is missing")
        uid = int(values["PUID"] or "")
        if clock() >= operation_deadline:
            raise TimeoutError("core operation deadline exhausted")
        jf = JellyfinClient(transport=chosen.get("jellyfin", urllib_transport), deadline=operation_deadline, clock=clock).inspect(values["JELLYFIN_ADMIN_USER"] or "", values["JELLYFIN_ADMIN_PASSWORD"] or "")
        checks.append(_application_check("jellyfin", jf, ("initialized", "libraries_exact"), "initialized with exact libraries", "initialization or exact libraries differ"))
        for service, media_path in (("radarr", "/data/media/movies"), ("sonarr", "/data/media/tv")):
            key = api_key_reader(values["CONFIG_ROOT"] or "", service, uid)
            domain = config.get("DOMAIN") or ""
            inspected = ArrClient(service, "http://127.0.0.1", key, headers={"Host": f"{service}.{domain}"}, transport=chosen.get(service, urllib_transport), deadline=operation_deadline, clock=clock).inspect(values["QUALITY_PROFILE"] or "", media_path)
            checks.append(_application_check(service, inspected, ("profile_exact", "root_exact", "media_settings", "completed_handling"), "selected profile, root, and media settings match", "selected profile, root, or media settings differ"))
            if inspected.get("runtime_root") is not None:
                runtime[service] = (inspected["runtime_profile"], inspected["runtime_root"])  # type: ignore[assignment]
        seerr = JellyseerrClient(headers={"Host": f"jellyseerr.{config.get('DOMAIN') or ''}"}, transport=chosen.get("jellyseerr", urllib_transport), deadline=operation_deadline, clock=clock)
        seerr.authorize(settings_key_reader(values["CONFIG_ROOT"] or "", uid))
        inspected_seerr = seerr.inspect(runtime)
        checks.append(_application_check("jellyseerr", inspected_seerr, tuple(inspected_seerr), "initialized with exact internal default services", "initialization or internal default services differ"))
    except ApiError as caught:
        existing = {item["domain"] for item in checks}
        reason = "time budget exhausted" if caught.code == "deadline_exhausted" else "state could not be inspected"
        for domain in ("jellyfin", "radarr", "sonarr", "jellyseerr"):
            if domain not in existing:
                checks.append(_check(domain, None, f"{domain} {reason}"))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        existing = {item["domain"] for item in checks}
        reason = "time budget exhausted" if clock() >= operation_deadline else "state could not be inspected"
        for domain in ("jellyfin", "radarr", "sonarr", "jellyseerr"):
            if domain not in existing:
                checks.append(_check(domain, None, f"{domain} {reason}"))

    try:
        if quicksync_inspector is _inspect_quicksync:
            quicksync = quicksync_inspector(root, command_runner, "homeflix", deadline=operation_deadline, clock=clock)
        else:
            if clock() >= operation_deadline:
                raise TimeoutError("core operation deadline exhausted")
            quicksync = quicksync_inspector(root, command_runner, "homeflix")
        if quicksync is None:
            checks.append(_check("quicksync", None, "not selected", status="not-applicable"))
        else:
            checks.append(
                _check(
                    "quicksync",
                    quicksync,
                    "rendered and live device mappings match" if quicksync else
                    "rendered or live device mapping is invalid",
                )
            )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        checks.append(_check("quicksync", None, "device mapping could not be inspected"))
    passed = _runtime_passed(checks)
    return {"status": "verified" if passed else "failed", "passed": passed, "checks": checks}


_MANDATORY_DOMAINS = frozenset({
    "docker",
    "stack_contract",
    "compose_project",
    *(f"service:{service}" for service in CORE_SERVICES),
    "acquisition_absent",
    "mount",
    "hardlink_outcome",
    "jellyfin",
    "radarr",
    "sonarr",
    "jellyseerr",
})


def _runtime_passed(checks: Sequence[Mapping[str, object]]) -> bool:
    """Optional not-applicable must not wash mandatory unknown, skip, or failure."""

    observed = {item.get("domain") for item in checks}
    if not _MANDATORY_DOMAINS.issubset(observed):
        return False
    for item in checks:
        status = item.get("status")
        domain = item.get("domain")
        if status == "not-applicable":
            if domain in _MANDATORY_DOMAINS:
                return False
            continue
        if status not in {"pass", "warning"}:
            return False
    return True


def reconcile_core(repository_root: str | Path, **kwargs: object) -> dict[str, object]:
    """Repair safe core drift with existing idempotent primitives, then verify live state."""
    root = Path(repository_root).resolve()
    operation_clock = kwargs.get("clock", time.monotonic)
    if not callable(operation_clock):
        raise ValueError("operation clock is invalid")
    timeout = float(kwargs.get("readiness_timeout", READINESS_TIMEOUT))
    operation_deadline = float(kwargs.get("deadline", operation_clock() + max(0.0, timeout)))
    remaining = operation_deadline - operation_clock()
    if remaining <= 0:
        return {"status": "timeout", "reason": "Core reconciliation deadline was exhausted"}
    deploy_kwargs = {key: value for key, value in kwargs.items() if key in {"runner", "preflight_runner", "http_waiter", "container_waiter", "clock", "snapshotter"}}
    deploy_kwargs["readiness_timeout"] = remaining
    deploy_kwargs["deadline"] = operation_deadline
    try:
        deploy = deploy_core(root, **deploy_kwargs)
    except TimeoutError:
        return {"status": "timeout", "reason": "Core reconciliation deadline was exhausted"}
    deployment_verified = deploy.get("status") in {"ready", "already_ready"} or (
        deploy.get("status") == "checkpoint_failed"
        and all(isinstance(item, dict) and item.get("ready") is True for item in deploy.get("services", []))
    )
    if not deployment_verified:
        return {"status": "deployment_failed", "deploy": deploy}
    if operation_clock() >= operation_deadline:
        return {"status": "timeout", "deploy": deploy, "reason": "Core reconciliation deadline was exhausted"}
    try:
        configured = configure_core(root, deadline=operation_deadline, clock=operation_clock, **{key: value for key, value in kwargs.items() if key in {"runner", "http_waiter", "transports", "api_key_reader", "settings_key_reader"}})
    except TimeoutError:
        return {"status": "timeout", "deploy": deploy, "reason": "Core reconciliation deadline was exhausted"}
    except ApiError as caught:
        if caught.code == "deadline_exhausted":
            return {"status": "timeout", "deploy": deploy, "reason": "Core reconciliation deadline was exhausted"}
        raise
    if operation_clock() >= operation_deadline:
        return {"status": "timeout", "deploy": deploy, "configure": configured, "reason": "Core reconciliation deadline was exhausted"}
    verified = verify_core(root, deadline=operation_deadline, **{key: value for key, value in kwargs.items() if key in {"runner", "transports", "api_key_reader", "settings_key_reader", "http_waiter", "clock", "quicksync_inspector", "docker_inspector", "contract_evaluator", "mount_inspector", "hardlink_prober"}})
    if not verified["passed"]:
        return {"status": "verification_failed", "deploy": deploy, "configure": configured, "verify": verified}
    state_path = root / ".homeflix" / "setup.json"
    try:
        state = SetupState.load(state_path)
    except (OSError, ValueError):
        state = SetupState()
    state.checkpoints.update({"core_containers_started": True, "core_api_configured": True, "core_verified": True})
    try:
        state.save(state_path)
    except (OSError, ValueError):
        return {"status": "checkpoint_failed", "deploy": deploy, "configure": configured, "verify": verified, "reason": "Verified core state could not be checkpointed"}
    return {"status": "verified", "deploy": deploy, "configure": configured, "verify": verified}


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
    deadline: float | None = None,
    snapshotter: Callable[[str | Path, EnvDocument], DeploymentSnapshot] = capture_deployment_snapshot,
) -> dict[str, object]:
    """Reconcile core under one readiness deadline shared by initial and post-start checks."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
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
    state_warning: str | None = None
    try:
        state = SetupState.load(state_path)
    except (OSError, ValueError):
        state = SetupState()
        state_warning = "Existing checkpoint state could not be read; live reconciliation continued"
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
    readiness_deadline = operation_deadline
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
            **({"checkpoint_warning": state_warning} if state_warning else {}),
        }

    if clock() >= readiness_deadline:
        return {"status": "timeout", "changed": False, "services": _diagnostics(initial_states, readiness), "checkpoint_recorded": False, "reason": "Core deployment deadline was exhausted"}
    if preflight_runner is run_preflight:
        report = preflight_runner(config, "core", command_runner, deadline=readiness_deadline, clock=clock)
    else:
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
    if clock() >= readiness_deadline:
        return {"status": "timeout", "changed": False, "services": _diagnostics(initial_states, readiness), "checkpoint_recorded": False, "reason": "Core deployment deadline was exhausted"}
    startup_process_failed = False
    start_returncode = 1
    try:
        start = compose_up(
            root, CORE_SERVICES, command_runner, project_name=project_name,
            timeout=max(0.0, readiness_deadline - clock()),
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
        **({"checkpoint_warning": state_warning} if state_warning else {}),
    }
