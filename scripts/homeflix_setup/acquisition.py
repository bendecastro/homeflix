"""Torrent acquisition deploy, reconcile, and read-only verification."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Mapping

from .api import ApiError, ArrClient, read_api_key
from .api.client import Transport
from .api.prowlarr import ProwlarrClient
from .api.qbittorrent import (
    QBITTORRENT_PASSWORD_KEY,
    QBITTORRENT_USER,
    QBittorrentClient,
    reconcile_webui_credential,
)
from .command import CommandRunner
from .compose import (
    ACQUISITION_SERVICES,
    compose_command,
    compose_inventory,
    compose_ps,
    compose_up_acquisition,
    compose_up_gluetun,
)
from .core import wait_for_container
from .envfile import EnvDocument
from .state import SetupState
from .vpn import (
    VPN_HEALTH_TIMEOUT,
    _inspect_image_id,
    vpn_config_digest,
    vpn_evidence_is_current,
)


_CHECK_STATUSES = ("pass", "warning", "failure", "not-applicable", "unknown")


def _check(domain: str, passed: bool | None, reason: str, *, status: str | None = None) -> dict[str, object]:
    if status is None:
        status = "pass" if passed is True else "failure" if passed is False else "unknown"
    if status not in _CHECK_STATUSES:
        raise ValueError("verification check status is invalid")
    return {"domain": domain, "status": status, "reason": reason}


def _failed(checks: list[dict[str, object]], **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {"status": "failed", "passed": False, "checks": checks}
    payload.update(extra)
    return payload


def _load_environment(root: Path) -> EnvDocument:
    return EnvDocument.load(root / ".env")


def _load_evidence(root: Path) -> Mapping[str, object] | None:
    try:
        return SetupState.load(root / ".homeflix" / "setup.json").evidence
    except (OSError, ValueError):
        return None


def acquisition_gate_is_current(
    evidence: Mapping[str, object] | None,
    *,
    image_id: str,
    config_digest: str,
) -> bool:
    """True only when current VPN-gate evidence includes successful fail-closed proof."""

    return vpn_evidence_is_current(evidence, image_id=image_id, config_digest=config_digest) and evidence.get("fail_closed") is True


def _remaining(deadline: float, clock: Callable[[], float], cap: float) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("acquisition deadline exhausted")
    return min(cap, remaining)


def deploy_acquisition(
    repository_root: str | os.PathLike[str],
    *,
    runner: CommandRunner | None = None,
    dry_run: bool = False,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    readiness_timeout: float = VPN_HEALTH_TIMEOUT,
) -> dict[str, object]:
    """Start Gluetun, qBittorrent, and Prowlarr after current fail-closed evidence."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
    checks: list[dict[str, object]] = []

    try:
        config = _load_environment(root)
        project_name = config.get("COMPOSE_PROJECT_NAME")
        digest = vpn_config_digest(config)
    except (OSError, ValueError):
        checks.append(_check("fail_closed", None, "fail-closed evidence could not be inspected"))
        return _failed(checks)

    evidence = _load_evidence(root)
    image_id = None
    try:
        remaining = _remaining(operation_deadline, clock, 10)
        image_id = _inspect_image_id(command_runner, timeout=remaining)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        image_id = None

    if evidence is None or evidence.get("fail_closed") is not True:
        checks.append(_check("fail_closed", False, "current fail-closed evidence is required"))
        return _failed(checks)
    if not image_id or not acquisition_gate_is_current(evidence, image_id=image_id, config_digest=digest):
        checks.append(_check("fail_closed", False, "current fail-closed evidence is required"))
        return _failed(checks)
    checks.append(_check("fail_closed", True, "current fail-closed evidence is present"))

    prefix = compose_command(root, project_name=project_name)
    if dry_run:
        return {
            "status": "planned",
            "passed": True,
            "services": list(ACQUISITION_SERVICES),
            "mutation_commands": [
                [*prefix, "up", "--detach", "--no-deps", "gluetun"],
                [*prefix, "up", "--detach", "--no-deps", "qbittorrent", "prowlarr"],
            ],
            "state_written": False,
            "checks": checks,
        }

    try:
        remaining = _remaining(operation_deadline, clock, 300)
        started = compose_up_gluetun(
            root, ("gluetun",), command_runner, project_name=project_name, timeout=remaining
        )
        if started.returncode:
            checks.append(_check("service:gluetun", False, "gluetun could not be started"))
            return _failed(checks)
        remaining = _remaining(operation_deadline, clock, VPN_HEALTH_TIMEOUT)
        if not wait_for_container(
            "gluetun",
            lambda timeout: compose_ps(root, command_runner, project_name=project_name, timeout=timeout),
            timeout=remaining,
            clock=clock,
            sleep=sleep,
        ).ready:
            checks.append(_check("service:gluetun", False, "gluetun is not healthy"))
            return _failed(checks)
        checks.append(_check("service:gluetun", True, "gluetun is healthy"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("service:gluetun", None, "gluetun could not be started"))
        return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 300)
        started = compose_up_acquisition(
            root,
            ("qbittorrent", "prowlarr"),
            command_runner,
            project_name=project_name,
            timeout=remaining,
        )
        if started.returncode:
            checks.append(_check("clients", False, "acquisition clients could not be started"))
            return _failed(checks)
        for service in ("qbittorrent", "prowlarr"):
            remaining = _remaining(operation_deadline, clock, VPN_HEALTH_TIMEOUT)
            ready = wait_for_container(
                service,
                lambda timeout, name=service: compose_ps(
                    root, command_runner, project_name=project_name, timeout=timeout
                ),
                timeout=remaining,
                clock=clock,
                sleep=sleep,
            )
            if not ready.ready:
                checks.append(_check(f"service:{service}", False, f"{service} is not healthy"))
                return _failed(checks)
            checks.append(_check(f"service:{service}", True, f"{service} is healthy"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("clients", None, "acquisition clients could not be started"))
        return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 30)
        inventory = compose_inventory(root, command_runner, project_name=project_name, timeout=remaining)
        nzbget = next((item for item in inventory if item.get("service") == "nzbget"), None)
        running_nzbget = nzbget is not None and nzbget.get("state") in {"running", "restarting"}
        if running_nzbget:
            checks.append(_check("nzbget", False, "nzbget is running"))
            return _failed(checks)
        checks.append(_check("nzbget", True, "nzbget is stopped", status="not-applicable"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        checks.append(_check("nzbget", None, "nzbget state could not be inspected"))

    return {
        "status": "ready",
        "passed": True,
        "services": list(ACQUISITION_SERVICES),
        "checks": checks,
    }


def _env_port(config: EnvDocument, name: str, default: int) -> int:
    raw = config.get(name) or str(default)
    if not raw.isdigit():
        raise ValueError(f"{name} is invalid")
    value = int(raw)
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} is invalid")
    return value


def read_forwarded_port(runner: CommandRunner, *, timeout: float = 10.0) -> int | None:
    """Read Gluetun's documented forwarded-port file. Never guess a number."""

    result = runner.run(
        ("docker", "exec", "gluetun", "cat", "/tmp/gluetun/forwarded_port"),
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        return None
    text = (result.stdout or "").strip()
    if not text.isdigit():
        return None
    value = int(text)
    if not 1 <= value <= 65535:
        return None
    return value


def _namespace_shared(runner: CommandRunner, service: str, *, timeout: float) -> bool | None:
    result = runner.run(
        ("docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", service),
        check=False,
        timeout=timeout,
    )
    mode = (result.stdout or "").strip()
    if result.returncode or not mode:
        return None
    return mode == "container:gluetun"


def _require_gate(
    root: Path,
    runner: CommandRunner,
    config: EnvDocument,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[list[dict[str, object]], bool]:
    checks: list[dict[str, object]] = []
    evidence = _load_evidence(root)
    try:
        remaining = _remaining(deadline, clock, 10)
        image_id = _inspect_image_id(runner, timeout=remaining)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        image_id = None
    if evidence is None or evidence.get("fail_closed") is not True:
        checks.append(_check("fail_closed", False, "current fail-closed evidence is required"))
        return checks, False
    if not image_id or not acquisition_gate_is_current(
        evidence, image_id=image_id, config_digest=vpn_config_digest(config)
    ):
        checks.append(_check("fail_closed", False, "current fail-closed evidence is required"))
        return checks, False
    checks.append(_check("fail_closed", True, "current fail-closed evidence is present"))
    return checks, True


def configure_acquisition(
    repository_root: str | os.PathLike[str],
    *,
    runner: CommandRunner | None = None,
    transports: Mapping[str, Transport] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    readiness_timeout: float = VPN_HEALTH_TIMEOUT,
    api_key_reader: Callable[[str | Path, str, int], str] = read_api_key,
) -> dict[str, object]:
    """Reconcile qBittorrent, Prowlarr, and *arr torrent clients. Never starts NZBGet."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
    chosen = dict(transports or {})
    try:
        config = _load_environment(root)
    except (OSError, ValueError):
        return _failed([_check("fail_closed", None, "fail-closed evidence could not be inspected")])
    checks, gated = _require_gate(root, command_runner, config, deadline=operation_deadline, clock=clock)
    if not gated:
        return _failed(checks)

    try:
        qbit_port = _env_port(config, "QBITTORRENT_PORT", 6969)
        prowlarr_port = _env_port(config, "PROWLARR_PORT", 9696)
        config_root = config.get("CONFIG_ROOT") or ""
        puid_text = config.get("PUID") or ""
        if not config_root or not Path(config_root).is_absolute() or not puid_text.isdigit():
            raise ValueError("required acquisition configuration is missing")
        uid = int(puid_text)
        remaining = _remaining(operation_deadline, clock, 10)
        forwarded = read_forwarded_port(command_runner, timeout=remaining)
        qbittorrent = QBittorrentClient(
            f"http://127.0.0.1:{qbit_port}",
            transport=chosen.get("qbittorrent"),
            deadline=operation_deadline,
            clock=clock,
        ) if chosen.get("qbittorrent") else QBittorrentClient(
            f"http://127.0.0.1:{qbit_port}",
            deadline=operation_deadline,
            clock=clock,
        )
        credential = reconcile_webui_credential(
            qbittorrent,
            root / ".env",
            command_runner,
            timeout=_remaining(operation_deadline, clock, 10),
        )
        qbit_state = qbittorrent.configure(
            forwarded_port=forwarded,
            password=None,
        )
        password = EnvDocument.load(root / ".env").get(QBITTORRENT_PASSWORD_KEY) or ""
        if not password:
            raise ApiError("qbittorrent", "load password", None, "authentication_failed")
        radarr_key = api_key_reader(config_root, "radarr", uid)
        sonarr_key = api_key_reader(config_root, "sonarr", uid)
        prowlarr_key = api_key_reader(config_root, "prowlarr", uid)
        domain = config.get("DOMAIN") or ""
        prowlarr = ProwlarrClient(
            f"http://127.0.0.1:{prowlarr_port}",
            prowlarr_key,
            transport=chosen.get("prowlarr"),
            deadline=operation_deadline,
            clock=clock,
        ) if chosen.get("prowlarr") else ProwlarrClient(
            f"http://127.0.0.1:{prowlarr_port}",
            prowlarr_key,
            deadline=operation_deadline,
            clock=clock,
        )
        prowlarr_state = prowlarr.ensure_applications(
            prowlarr_port=prowlarr_port,
            arr_keys={"radarr": radarr_key, "sonarr": sonarr_key},
        )
        arr_state: dict[str, Any] = {}
        for service in ("radarr", "sonarr"):
            key = radarr_key if service == "radarr" else sonarr_key
            client = ArrClient(
                service,
                "http://127.0.0.1",
                key,
                headers={"Host": f"{service}.{domain}"} if domain else None,
                transport=chosen.get(service),
                deadline=operation_deadline,
                clock=clock,
            ) if chosen.get(service) else ArrClient(
                service,
                "http://127.0.0.1",
                key,
                headers={"Host": f"{service}.{domain}"} if domain else None,
                deadline=operation_deadline,
                clock=clock,
            )
            changed = client.ensure_qbittorrent_client(
                host="gluetun",
                port=qbit_port,
                username=QBITTORRENT_USER,
                password=password,
                force_password=credential.get("credential_updated") is True,
            )
            inspected = client.inspect_download_client(host="gluetun", port=qbit_port)
            discovery = client.inspect(config.get("QUALITY_PROFILE") or "HD-1080p", "/data/media/movies" if service == "radarr" else "/data/media/tv")
            arr_state[service] = {
                "changed": changed,
                "client_exact": inspected.get("exact") is True,
                "completed_handling": discovery.get("completed_handling") is True,
                "hardlinks": discovery.get("media_settings") is True,
                "targeted_connection_exact": discovery.get("targeted_connection_exact") is True,
                "refresh_connection_exact": discovery.get("refresh_connection_exact") is True,
            }
    except ApiError as error:
        return _failed(checks + [_check(error.service, False, str(error.code))])
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        return _failed(checks + [_check("acquisition", None, "acquisition APIs could not be configured")])

    indexer_ok = prowlarr_state.get("indexer_credentials") is True
    status = "configured" if indexer_ok else "credentials_required"
    return {
        "status": status,
        "passed": indexer_ok,
        "credential_updated": credential.get("credential_updated") is True,
        "qbittorrent": {
            "save_path": qbit_state.get("save_path") is True,
            "incomplete": qbit_state.get("incomplete") is True,
            "categories": qbit_state.get("categories") is True,
            "bypass_local_auth": qbit_state.get("bypass_local_auth") is True,
            "port_agrees": qbit_state.get("port_agrees") is True,
        },
        "prowlarr": {
            "radarr_application": prowlarr_state.get("radarr_application") is True,
            "sonarr_application": prowlarr_state.get("sonarr_application") is True,
            "indexer_credentials": indexer_ok,
        },
        "radarr": arr_state["radarr"],
        "sonarr": arr_state["sonarr"],
        "checks": checks,
    }


_VERIFY_MANDATORY = frozenset({
    "fail_closed",
    "namespace",
    "service:qbittorrent",
    "service:prowlarr",
    "paths",
    "categories",
    "connections",
    "port_agrees",
    "jellyfin_discovery",
    "indexers",
    "nzbget",
})


def verify_acquisition(
    repository_root: str | os.PathLike[str],
    *,
    runner: CommandRunner | None = None,
    transports: Mapping[str, Transport] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    readiness_timeout: float = VPN_HEALTH_TIMEOUT,
    api_key_reader: Callable[[str | Path, str, int], str] = read_api_key,
) -> dict[str, object]:
    """Read-only acquisition evidence. Never starts services."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
    chosen = dict(transports or {})
    checks: list[dict[str, object]] = []
    try:
        config = _load_environment(root)
    except (OSError, ValueError):
        return _failed([_check("fail_closed", None, "fail-closed evidence could not be inspected")])

    gate_checks, gated = _require_gate(root, command_runner, config, deadline=operation_deadline, clock=clock)
    checks.extend(gate_checks)
    if not gated:
        return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 30)
        inventory = compose_inventory(root, command_runner, project_name=config.get("COMPOSE_PROJECT_NAME"), timeout=remaining)
        started = [item["service"] for item in inventory if item.get("state") in {"running", "restarting"}]
        if "nzbget" in started:
            checks.append(_check("nzbget", False, "nzbget is running"))
        else:
            checks.append(_check("nzbget", True, "nzbget is stopped", status="not-applicable"))
        for service in ("qbittorrent", "prowlarr"):
            present = service in started
            checks.append(_check(f"service:{service}", present, f"{service} is running" if present else f"{service} is not running"))
        shared = True
        for service in ("qbittorrent", "prowlarr"):
            remaining = _remaining(operation_deadline, clock, 10)
            mode = _namespace_shared(command_runner, service, timeout=remaining)
            if mode is not True:
                shared = False
        checks.append(_check("namespace", shared, "clients share the Gluetun namespace" if shared else "clients do not share the Gluetun namespace"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        for domain in ("namespace", "service:qbittorrent", "service:prowlarr", "nzbget"):
            if not any(item["domain"] == domain for item in checks):
                checks.append(_check(domain, None, "live acquisition inventory could not be inspected"))
        return _failed(checks)

    try:
        qbit_port = _env_port(config, "QBITTORRENT_PORT", 6969)
        prowlarr_port = _env_port(config, "PROWLARR_PORT", 9696)
        config_root = config.get("CONFIG_ROOT") or ""
        puid_text = config.get("PUID") or ""
        uid = int(puid_text)
        remaining = _remaining(operation_deadline, clock, 10)
        forwarded = read_forwarded_port(command_runner, timeout=remaining)
        qbittorrent = QBittorrentClient(
            f"http://127.0.0.1:{qbit_port}",
            transport=chosen.get("qbittorrent"),
            deadline=operation_deadline,
            clock=clock,
        ) if chosen.get("qbittorrent") else QBittorrentClient(
            f"http://127.0.0.1:{qbit_port}",
            deadline=operation_deadline,
            clock=clock,
        )
        password = config.get(QBITTORRENT_PASSWORD_KEY) or ""
        if not password or not qbittorrent.login(QBITTORRENT_USER, password):
            raise ApiError("qbittorrent", "login", None, "authentication_failed")
        qbit = qbittorrent.inspect(forwarded_port=forwarded)
        checks.append(_check("paths", qbit["save_path"] is True and qbit["incomplete"] is True, "download paths match the single-root layout"))
        checks.append(_check("categories", qbit["categories"] is True, "movies/tv/music categories match"))
        checks.append(_check("port_agrees", qbit["port_agrees"] is True, "listen port agrees with the forwarded port"))
        checks.append(_check("localhost_auth", qbit["bypass_local_auth"] is True, "localhost authentication bypass is enabled"))
        prowlarr = ProwlarrClient(
            f"http://127.0.0.1:{prowlarr_port}",
            api_key_reader(config_root, "prowlarr", uid),
            transport=chosen.get("prowlarr"),
            deadline=operation_deadline,
            clock=clock,
        ) if chosen.get("prowlarr") else ProwlarrClient(
            f"http://127.0.0.1:{prowlarr_port}",
            api_key_reader(config_root, "prowlarr", uid),
            deadline=operation_deadline,
            clock=clock,
        )
        prowled = prowlarr.inspect(prowlarr_port=prowlarr_port)
        connections = (
            prowled.get("radarr_application") is True
            and prowled.get("sonarr_application") is True
        )
        discovery_ok = True
        clients_ok = True
        domain = config.get("DOMAIN") or ""
        for service, media_path in (("radarr", "/data/media/movies"), ("sonarr", "/data/media/tv")):
            client = ArrClient(
                service,
                "http://127.0.0.1",
                api_key_reader(config_root, service, uid),
                headers={"Host": f"{service}.{domain}"} if domain else None,
                transport=chosen.get(service),
                deadline=operation_deadline,
                clock=clock,
            ) if chosen.get(service) else ArrClient(
                service,
                "http://127.0.0.1",
                api_key_reader(config_root, service, uid),
                headers={"Host": f"{service}.{domain}"} if domain else None,
                deadline=operation_deadline,
                clock=clock,
            )
            inspected = client.inspect_download_client(host="gluetun", port=qbit_port)
            discovery = client.inspect(config.get("QUALITY_PROFILE") or "HD-1080p", media_path)
            if inspected.get("exact") is not True:
                clients_ok = False
            if discovery.get("targeted_connection_exact") is not True or discovery.get("refresh_connection_exact") is not True:
                discovery_ok = False
        connections = connections and clients_ok
        checks.append(_check("connections", connections, "Prowlarr and *arr torrent connections match"))
        checks.append(_check("jellyfin_discovery", discovery_ok, "existing Jellyfin discovery connections match"))
        indexer_ok = prowled.get("indexer_credentials") is True
        checks.append(
            _check(
                "indexers",
                indexer_ok,
                "usable indexer present" if indexer_ok else "provider/indexer credentials required",
            )
        )
    except ApiError as error:
        checks.append(_check(error.service, False, str(error.code)))
        return _failed(checks)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError, KeyError, TypeError):
        for domain in ("paths", "categories", "connections", "port_agrees", "jellyfin_discovery", "indexers"):
            if not any(item["domain"] == domain for item in checks):
                checks.append(_check(domain, None, "acquisition APIs could not be inspected"))
        return _failed(checks)

    observed = {item["domain"] for item in checks}
    if not _VERIFY_MANDATORY.issubset(observed):
        return _failed(checks)
    failures = [item for item in checks if item["status"] not in {"pass", "warning", "not-applicable"}]
    indexer_only = failures and all(item["domain"] == "indexers" for item in failures)
    if indexer_only:
        return {"status": "credentials_required", "passed": False, "checks": checks}
    passed = not failures
    return {"status": "verified" if passed else "failed", "passed": passed, "checks": checks}

