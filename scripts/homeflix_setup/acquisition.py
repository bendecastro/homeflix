"""Selected acquisition deploy, reconcile, and read-only verification."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Mapping

from .api import ApiError, ArrClient, read_api_key
from .api.client import Transport
from .api.nzbget import (
    DEFAULT_USER as NZBGET_USER,
    NZBGET_PASSWORD_KEY,
    NZBGET_USER_KEY,
    NzbgetClient,
    reconcile_control_credential,
)
from .api.prowlarr import ProwlarrClient
from .api.qbittorrent import (
    QBITTORRENT_PASSWORD_KEY,
    QBITTORRENT_USER,
    QBittorrentClient,
    reconcile_webui_credential,
)
from .command import CommandRunner
from .compose import (
    ACQUISITION_CLIENT_SERVICES,
    compose_command,
    compose_inventory,
    compose_ps,
    compose_stop_acquisition,
    compose_up_acquisition,
    compose_up_gluetun,
)
from .core import wait_for_container
from .envfile import EnvDocument
from .state import ACQUISITION_CLIENT_SELECTIONS, SetupState
from .vpn import (
    VPN_HEALTH_TIMEOUT,
    _inspect_image_id,
    namespace_shared,
    vpn_config_digest,
    vpn_evidence_is_current,
)


_CHECK_STATUSES = ("pass", "warning", "failure", "not-applicable", "unknown")
DEFAULT_CLIENTS = "torrent"


def normalize_clients(value: str | None) -> str:
    if value is None:
        return DEFAULT_CLIENTS
    if not isinstance(value, str):
        raise ValueError("acquisition clients must be torrent, usenet, or both")
    normalized = value.strip().casefold()
    if normalized not in ACQUISITION_CLIENT_SELECTIONS:
        raise ValueError("acquisition clients must be torrent, usenet, or both")
    return normalized


def load_persisted_clients(root: Path) -> str | None:
    try:
        state = SetupState.load(root / ".homeflix" / "setup.json")
    except (OSError, ValueError):
        return None
    selection = state.acquisition_clients
    if selection in ACQUISITION_CLIENT_SELECTIONS:
        return selection
    return None


def resolve_clients(root: Path, requested: str | None) -> str:
    if requested is not None:
        return normalize_clients(requested)
    return load_persisted_clients(root) or DEFAULT_CLIENTS


def persist_clients(root: Path, clients: str) -> None:
    selection = normalize_clients(clients)
    path = root / ".homeflix" / "setup.json"
    try:
        state = SetupState.load(path)
    except (OSError, ValueError):
        state = SetupState()
    state.acquisition_clients = selection
    state.save(path)


def selected_services(clients: str) -> tuple[str, ...]:
    selection = normalize_clients(clients)
    services = ["gluetun"]
    if selection in {"torrent", "both"}:
        services.append("qbittorrent")
    if selection in {"usenet", "both"}:
        services.append("nzbget")
    services.append("prowlarr")
    return tuple(services)


def unselected_download_clients(clients: str) -> tuple[str, ...]:
    selection = normalize_clients(clients)
    return tuple(service for service in ACQUISITION_CLIENT_SERVICES if service not in selected_services(selection))


def selection_changed(root: Path, requested: str | None) -> bool:
    if requested is None:
        return False
    persisted = load_persisted_clients(root)
    return persisted is not None and persisted != normalize_clients(requested)


def _wants_torrent(selection: str) -> bool:
    return selection in {"torrent", "both"}


def _wants_usenet(selection: str) -> bool:
    return selection in {"usenet", "both"}


def _news_server_from_env(config: EnvDocument) -> dict[str, str] | None:
    host = (config.get("USENET_HOST") or "").strip()
    user = (config.get("USENET_USER") or "").strip()
    password = (config.get("USENET_PASSWORD") or "").strip()
    port = (config.get("USENET_PORT") or "563").strip()
    if not host or not user or not password:
        return None
    return {"host": host, "port": port, "username": user, "password": password}


def _make_arr_client(
    service: str,
    *,
    key: str,
    domain: str,
    transport: Transport | None,
    deadline: float,
    clock: Callable[[], float],
) -> ArrClient:
    kwargs: dict[str, Any] = {
        "headers": {"Host": f"{service}.{domain}"} if domain else None,
        "deadline": deadline,
        "clock": clock,
    }
    if transport is not None:
        kwargs["transport"] = transport
    return ArrClient(service, "http://127.0.0.1", key, **kwargs)


def _verify_mandatory(selection: str) -> frozenset[str]:
    domains = {
        "fail_closed",
        "namespace",
        "service:prowlarr",
        "connections",
        "jellyfin_discovery",
        "indexers",
    }
    if _wants_torrent(selection):
        domains.update({"service:qbittorrent", "paths", "categories", "port_agrees"})
    if _wants_usenet(selection):
        domains.update({"service:nzbget", "news_servers", "paths", "categories"})
    domains.update(unselected_download_clients(selection))
    return frozenset(domains)


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
    clients: str | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    readiness_timeout: float = VPN_HEALTH_TIMEOUT,
) -> dict[str, object]:
    """Start Gluetun and the selected acquisition clients after current fail-closed evidence."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
    checks: list[dict[str, object]] = []
    try:
        selection = resolve_clients(root, clients)
    except ValueError:
        checks.append(_check("clients", False, "acquisition clients must be torrent, usenet, or both"))
        return _failed(checks)
    selected = selected_services(selection)
    gated_selected = tuple(service for service in selected if service != "gluetun")
    unselected = unselected_download_clients(selection)
    changed = selection_changed(root, clients)

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
    mutation_commands = [
        [*prefix, "up", "--detach", "--no-deps", "gluetun"],
        [*prefix, "up", "--detach", "--no-deps", *gated_selected],
    ]
    if changed and unselected:
        mutation_commands.append([*prefix, "stop", *unselected])
    if dry_run:
        return {
            "status": "planned",
            "passed": True,
            "clients": selection,
            "services": list(selected),
            "mutation_commands": mutation_commands,
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
            gated_selected,
            command_runner,
            project_name=project_name,
            timeout=remaining,
        )
        if started.returncode:
            checks.append(_check("clients", False, "acquisition clients could not be started"))
            return _failed(checks)
        for service in gated_selected:
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

    if changed and unselected:
        try:
            remaining = _remaining(operation_deadline, clock, 60)
            compose_stop_acquisition(
                root, unselected, command_runner, project_name=project_name, timeout=remaining
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
            checks.append(_check("clients", False, "unselected acquisition clients could not be stopped"))
            return _failed(checks)

    try:
        remaining = _remaining(operation_deadline, clock, 30)
        inventory = compose_inventory(root, command_runner, project_name=project_name, timeout=remaining)
        running = {
            item.get("service")
            for item in inventory
            if item.get("state") in {"running", "restarting"}
        }
        for service in unselected:
            if service in running:
                checks.append(_check(service, False, f"{service} is running"))
                return _failed(checks)
            checks.append(_check(service, True, f"{service} is stopped", status="not-applicable"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        for service in unselected:
            checks.append(_check(service, None, f"{service} state could not be inspected"))

    persist_clients(root, selection)
    return {
        "status": "ready",
        "passed": True,
        "clients": selection,
        "services": list(selected),
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
    clients: str | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    readiness_timeout: float = VPN_HEALTH_TIMEOUT,
    api_key_reader: Callable[[str | Path, str, int], str] = read_api_key,
) -> dict[str, object]:
    """Reconcile selected download clients, Prowlarr, and *arr connections."""

    root = Path(repository_root).resolve()
    command_runner = runner or CommandRunner()
    operation_deadline = deadline if deadline is not None else clock() + max(0.0, readiness_timeout)
    chosen = dict(transports or {})
    try:
        selection = resolve_clients(root, clients)
        config = _load_environment(root)
    except (OSError, ValueError):
        return _failed([_check("fail_closed", None, "fail-closed evidence could not be inspected")])
    checks, gated = _require_gate(root, command_runner, config, deadline=operation_deadline, clock=clock)
    if not gated:
        return _failed(checks)

    wants_torrent = _wants_torrent(selection)
    wants_usenet = _wants_usenet(selection)
    credential_updated = False
    qbit_state: dict[str, Any] = {}
    nzb_state: dict[str, Any] = {}
    try:
        prowlarr_port = _env_port(config, "PROWLARR_PORT", 9696)
        config_root = config.get("CONFIG_ROOT") or ""
        puid_text = config.get("PUID") or ""
        if not config_root or not Path(config_root).is_absolute() or not puid_text.isdigit():
            raise ValueError("required acquisition configuration is missing")
        uid = int(puid_text)
        qbit_password = ""
        nzb_user = NZBGET_USER
        nzb_password = ""
        qbit_port = _env_port(config, "QBITTORRENT_PORT", 6969) if wants_torrent else 0
        nzb_port = _env_port(config, "NZBGET_PORT", 6789) if wants_usenet else 0
        if wants_torrent:
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
            credential_updated = credential.get("credential_updated") is True or credential_updated
            qbit_state = qbittorrent.configure(forwarded_port=forwarded, password=None)
            qbit_password = EnvDocument.load(root / ".env").get(QBITTORRENT_PASSWORD_KEY) or ""
            if not qbit_password:
                raise ApiError("qbittorrent", "load password", None, "authentication_failed")
        if wants_usenet:
            nzbget = NzbgetClient(
                f"http://127.0.0.1:{nzb_port}",
                transport=chosen.get("nzbget"),
                deadline=operation_deadline,
                clock=clock,
            ) if chosen.get("nzbget") else NzbgetClient(
                f"http://127.0.0.1:{nzb_port}",
                deadline=operation_deadline,
                clock=clock,
            )
            nzb_credential = reconcile_control_credential(nzbget, root / ".env")
            credential_updated = nzb_credential.get("credential_updated") is True or credential_updated
            env_now = EnvDocument.load(root / ".env")
            nzb_state = nzbget.configure(news_server=_news_server_from_env(env_now))
            nzb_user = env_now.get(NZBGET_USER_KEY) or NZBGET_USER
            nzb_password = env_now.get(NZBGET_PASSWORD_KEY) or ""
            if not nzb_password:
                raise ApiError("nzbget", "load password", None, "authentication_failed")
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
            client = _make_arr_client(
                service,
                key=key,
                domain=domain,
                transport=chosen.get(service),
                deadline=operation_deadline,
                clock=clock,
            )
            changed = False
            client_exact = True
            if wants_torrent:
                changed = client.ensure_qbittorrent_client(
                    host="gluetun",
                    port=qbit_port,
                    username=QBITTORRENT_USER,
                    password=qbit_password,
                    force_password=credential_updated,
                ) or changed
                inspected = client.inspect_download_client(host="gluetun", port=qbit_port)
                client_exact = client_exact and inspected.get("exact") is True
            if wants_usenet:
                changed = client.ensure_nzbget_client(
                    host="gluetun",
                    port=nzb_port,
                    username=nzb_user,
                    password=nzb_password,
                    force_password=credential_updated,
                ) or changed
                inspected = client.inspect_download_client(
                    host="gluetun", port=nzb_port, implementation="Nzbget"
                )
                client_exact = client_exact and inspected.get("exact") is True
            discovery = client.inspect(
                config.get("QUALITY_PROFILE") or "HD-1080p",
                "/data/media/movies" if service == "radarr" else "/data/media/tv",
            )
            arr_state[service] = {
                "changed": changed,
                "client_exact": client_exact,
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
    news_ok = (not wants_usenet) or nzb_state.get("news_servers") is True
    ready = indexer_ok and news_ok
    persist_clients(root, selection)
    payload: dict[str, object] = {
        "status": "configured" if ready else "credentials_required",
        "passed": ready,
        "clients": selection,
        "credential_updated": credential_updated,
        "prowlarr": {
            "radarr_application": prowlarr_state.get("radarr_application") is True,
            "sonarr_application": prowlarr_state.get("sonarr_application") is True,
            "indexer_credentials": indexer_ok,
        },
        "radarr": arr_state["radarr"],
        "sonarr": arr_state["sonarr"],
        "checks": checks,
    }
    if wants_torrent:
        payload["qbittorrent"] = {
            "save_path": qbit_state.get("save_path") is True,
            "incomplete": qbit_state.get("incomplete") is True,
            "categories": qbit_state.get("categories") is True,
            "bypass_local_auth": qbit_state.get("bypass_local_auth") is True,
            "port_agrees": qbit_state.get("port_agrees") is True,
        }
    if wants_usenet:
        payload["nzbget"] = {
            "paths": nzb_state.get("paths") is True,
            "categories": nzb_state.get("categories") is True,
            "news_servers": nzb_state.get("news_servers") is True,
        }
    return payload


def verify_acquisition(
    repository_root: str | os.PathLike[str],
    *,
    runner: CommandRunner | None = None,
    transports: Mapping[str, Transport] | None = None,
    clients: str | None = None,
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
        selection = resolve_clients(root, clients)
        config = _load_environment(root)
    except (OSError, ValueError):
        return _failed([_check("fail_closed", None, "fail-closed evidence could not be inspected")])

    gate_checks, gated = _require_gate(root, command_runner, config, deadline=operation_deadline, clock=clock)
    checks.extend(gate_checks)
    if not gated:
        return _failed(checks)

    wants_torrent = _wants_torrent(selection)
    wants_usenet = _wants_usenet(selection)
    selected = [service for service in selected_services(selection) if service != "gluetun"]
    unselected = unselected_download_clients(selection)
    try:
        remaining = _remaining(operation_deadline, clock, 30)
        inventory = compose_inventory(root, command_runner, project_name=config.get("COMPOSE_PROJECT_NAME"), timeout=remaining)
        started = [item["service"] for item in inventory if item.get("state") in {"running", "restarting"}]
        for service in unselected:
            if service in started:
                checks.append(_check(service, False, f"{service} is running"))
            else:
                checks.append(_check(service, True, f"{service} is stopped", status="not-applicable"))
        for service in selected:
            present = service in started
            checks.append(_check(f"service:{service}", present, f"{service} is running" if present else f"{service} is not running"))
        shared = True
        for service in selected:
            remaining = _remaining(operation_deadline, clock, 10)
            mode = namespace_shared(command_runner, service, timeout=remaining)
            if mode is not True:
                shared = False
        checks.append(_check("namespace", shared, "clients share the Gluetun namespace" if shared else "clients do not share the Gluetun namespace"))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, TimeoutError):
        for domain in ("namespace", *{f"service:{name}" for name in selected}, *unselected):
            if not any(item["domain"] == domain for item in checks):
                checks.append(_check(domain, None, "live acquisition inventory could not be inspected"))
        return _failed(checks)

    try:
        prowlarr_port = _env_port(config, "PROWLARR_PORT", 9696)
        config_root = config.get("CONFIG_ROOT") or ""
        puid_text = config.get("PUID") or ""
        uid = int(puid_text)
        paths_ok = True
        categories_ok = True
        if wants_torrent:
            qbit_port = _env_port(config, "QBITTORRENT_PORT", 6969)
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
            paths_ok = paths_ok and qbit["save_path"] is True and qbit["incomplete"] is True
            categories_ok = categories_ok and qbit["categories"] is True
            checks.append(_check("port_agrees", qbit["port_agrees"] is True, "listen port agrees with the forwarded port"))
            checks.append(_check("localhost_auth", qbit["bypass_local_auth"] is True, "localhost authentication bypass is enabled"))
        else:
            qbit_port = 0
        nzb_port = 0
        if wants_usenet:
            nzb_port = _env_port(config, "NZBGET_PORT", 6789)
            nzbget = NzbgetClient(
                f"http://127.0.0.1:{nzb_port}",
                transport=chosen.get("nzbget"),
                deadline=operation_deadline,
                clock=clock,
            ) if chosen.get("nzbget") else NzbgetClient(
                f"http://127.0.0.1:{nzb_port}",
                deadline=operation_deadline,
                clock=clock,
            )
            nzb_user = config.get(NZBGET_USER_KEY) or NZBGET_USER
            nzb_password = config.get(NZBGET_PASSWORD_KEY) or ""
            if not nzb_password or not nzbget.login(nzb_user, nzb_password):
                raise ApiError("nzbget", "login", None, "authentication_failed")
            nzb = nzbget.inspect()
            paths_ok = paths_ok and nzb["paths"] is True
            categories_ok = categories_ok and nzb["categories"] is True
            checks.append(
                _check(
                    "news_servers",
                    nzb["news_servers"] is True,
                    "news-server credentials present" if nzb["news_servers"] is True else "news-server credentials required",
                )
            )
        checks.append(_check("paths", paths_ok, "download paths match the single-root layout"))
        checks.append(_check("categories", categories_ok, "movies/tv/music categories match"))
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
            client = _make_arr_client(
                service,
                key=api_key_reader(config_root, service, uid),
                domain=domain,
                transport=chosen.get(service),
                deadline=operation_deadline,
                clock=clock,
            )
            if wants_torrent:
                inspected = client.inspect_download_client(host="gluetun", port=qbit_port)
                if inspected.get("exact") is not True:
                    clients_ok = False
            if wants_usenet:
                inspected = client.inspect_download_client(
                    host="gluetun", port=nzb_port, implementation="Nzbget"
                )
                if inspected.get("exact") is not True:
                    clients_ok = False
            discovery = client.inspect(config.get("QUALITY_PROFILE") or "HD-1080p", media_path)
            if discovery.get("targeted_connection_exact") is not True or discovery.get("refresh_connection_exact") is not True:
                discovery_ok = False
        connections = connections and clients_ok
        checks.append(_check("connections", connections, "Prowlarr and *arr download-client connections match"))
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
        for domain in ("paths", "categories", "connections", "port_agrees", "jellyfin_discovery", "indexers", "news_servers"):
            if domain in _verify_mandatory(selection) and not any(item["domain"] == domain for item in checks):
                checks.append(_check(domain, None, "acquisition APIs could not be inspected"))
        return _failed(checks)

    observed = {item["domain"] for item in checks}
    if not _verify_mandatory(selection).issubset(observed):
        return _failed(checks)
    failures = [item for item in checks if item["status"] not in {"pass", "warning", "not-applicable"}]
    credential_domains = {"indexers"}
    if wants_usenet:
        credential_domains.add("news_servers")
    credential_only = failures and all(item["domain"] in credential_domains for item in failures)
    if credential_only:
        return {"status": "credentials_required", "passed": False, "clients": selection, "checks": checks}
    passed = not failures
    return {
        "status": "verified" if passed else "failed",
        "passed": passed,
        "clients": selection,
        "checks": checks,
    }

