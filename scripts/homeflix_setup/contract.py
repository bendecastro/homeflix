"""Pure stack-contract checks against one already-rendered Compose mapping."""

from __future__ import annotations

import ipaddress
from typing import Any, Mapping

from .compose import CORE_SERVICES
from .core import NON_CORE_SERVICES


SCHEMA_VERSION = 1
VPN_NAMESPACE_SERVICES = ("qbittorrent", "nzbget", "prowlarr")
ARR_DATA_SERVICES = ("radarr", "sonarr", "lidarr")
GLUETUN_NAMESPACE = "container:gluetun"
HEALTH_PROBE = "http://127.0.0.1:9999/"
DEUNHEALTH_LABEL = "deunhealth.restart.on.unhealthy"


def _services(rendered: Mapping[str, Any]) -> dict[str, Any]:
    raw = rendered.get("services")
    if not isinstance(raw, Mapping):
        return {}
    return {name: value for name, value in raw.items() if isinstance(value, Mapping)}


def _finding(code: str, message: str, *, service: str | None = None) -> dict[str, object]:
    finding: dict[str, object] = {"code": code, "message": message}
    if service is not None:
        finding["service"] = service
    return finding


def _volumes(service: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = service.get("volumes")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _check_vpn_namespace(services: Mapping[str, Any]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for name in VPN_NAMESPACE_SERVICES:
        service = services.get(name)
        if service is None:
            continue
        mode = service.get("network_mode")
        if mode != GLUETUN_NAMESPACE:
            findings.append(
                _finding(
                    "vpn_namespace",
                    "VPN-risky service must share Gluetun's network namespace",
                    service=name,
                )
            )
    return findings


def _labels(service: Mapping[str, Any]) -> dict[str, str]:
    raw = service.get("labels")
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        parsed: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, value = item.split("=", 1)
            parsed[key] = value
        return parsed
    return {}


def _healthcheck_probes_namespace(service: Mapping[str, Any]) -> bool:
    healthcheck = service.get("healthcheck")
    if not isinstance(healthcheck, Mapping):
        return False
    test = healthcheck.get("test")
    if isinstance(test, str):
        command = test
    elif isinstance(test, list):
        command = " ".join(str(part) for part in test)
    else:
        return False
    return HEALTH_PROBE in command and ":8888" not in command


def _namespace_sharing_services(services: Mapping[str, Any]) -> list[str]:
    return [
        name
        for name, service in services.items()
        if service.get("network_mode") == GLUETUN_NAMESPACE
    ]


def _check_self_heal(services: Mapping[str, Any]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for name in _namespace_sharing_services(services):
        service = services[name]
        if not _healthcheck_probes_namespace(service):
            findings.append(
                _finding(
                    "self_heal_healthcheck",
                    "namespace-sharing service must probe Gluetun's namespace health endpoint",
                    service=name,
                )
            )
        if _labels(service).get(DEUNHEALTH_LABEL) != "true":
            findings.append(
                _finding(
                    "self_heal_label",
                    "namespace-sharing service must carry the deunhealth restart label",
                    service=name,
                )
            )
    return findings


def _router_names(labels: Mapping[str, str]) -> set[str]:
    names: set[str] = set()
    prefix = "traefik.http.routers."
    suffix = ".rule"
    for key, value in labels.items():
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        name = key[len(prefix) : -len(suffix)]
        if name and "." not in name and "Host(" in value:
            names.add(name)
    return names


def _environment(service: Mapping[str, Any]) -> dict[str, str]:
    raw = service.get("environment")
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        parsed: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, value = item.split("=", 1)
            parsed[key] = value
        return parsed
    return {}


def _parse_networks(value: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in value.split(","):
        text = item.strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            continue
    return networks


def _proxy_subnet(rendered: Mapping[str, Any]) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    networks = rendered.get("networks")
    if not isinstance(networks, Mapping):
        return None
    proxy = networks.get("traefik-network")
    if not isinstance(proxy, Mapping):
        return None
    ipam = proxy.get("ipam")
    if not isinstance(ipam, Mapping):
        return None
    config = ipam.get("config")
    if not isinstance(config, list) or not config:
        return None
    first = config[0]
    if not isinstance(first, Mapping) or not first.get("subnet"):
        return None
    try:
        return ipaddress.ip_network(str(first["subnet"]), strict=False)
    except ValueError:
        return None


def _phase_allowlists(rendered: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    extension = rendered.get("x-homeflix")
    phases = extension.get("phases") if isinstance(extension, Mapping) else None
    if not isinstance(phases, Mapping):
        return set(), set()
    core = phases.get("core")
    acquisition = phases.get("acquisition")
    return (
        {str(name) for name in core} if isinstance(core, list) else set(),
        {str(name) for name in acquisition} if isinstance(acquisition, list) else set(),
    )


def _service_phase(service: Mapping[str, Any]) -> str | None:
    extension = service.get("x-homeflix")
    if isinstance(extension, Mapping):
        phase = extension.get("phase")
        if phase in {"core", "acquisition"}:
            return str(phase)
    labels = _labels(service)
    label_phase = labels.get("homeflix.phase") or labels.get("x-homeflix.phase")
    if label_phase in {"core", "acquisition"}:
        return label_phase
    return None


def _check_phases(
    rendered: Mapping[str, Any], services: Mapping[str, Any]
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    core_allowlist, acquisition_allowlist = _phase_allowlists(rendered)
    for name, service in services.items():
        phase = _service_phase(service)
        if phase == "core":
            core_allowlist.add(name)
        elif phase == "acquisition":
            acquisition_allowlist.add(name)
    overlap = sorted(core_allowlist & acquisition_allowlist)
    if overlap:
        findings.append(
            _finding(
                "phase_overlap",
                "core and acquisition phase allowlists must be disjoint",
                service=overlap[0],
            )
        )
    classified = set(CORE_SERVICES) | set(NON_CORE_SERVICES)
    assigned = core_allowlist | acquisition_allowlist
    for name in sorted(classified - assigned):
        if name in services:
            findings.append(
                _finding(
                    "phase_missing",
                    "classified service is missing an explicit phase allowlist field",
                    service=name,
                )
            )
    if core_allowlist != set(CORE_SERVICES) or acquisition_allowlist != set(NON_CORE_SERVICES):
        if not overlap:
            findings.append(
                _finding(
                    "phase_mismatch",
                    "rendered phase allowlists must agree with CORE_SERVICES and NON_CORE_SERVICES",
                )
            )
    return findings


def _check_proxy_subnets(
    rendered: Mapping[str, Any], services: Mapping[str, Any]
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    proxy = _proxy_subnet(rendered)
    gluetun = services.get("gluetun")
    if proxy is None or not isinstance(gluetun, Mapping):
        findings.append(
            _finding(
                "proxy_subnet_allowlist",
                "proxy network subnet must be pinned and allowlisted on Gluetun",
            )
        )
        return findings
    allowed = _parse_networks(_environment(gluetun).get("FIREWALL_OUTBOUND_SUBNETS", ""))
    if proxy not in allowed:
        findings.append(
            _finding(
                "proxy_subnet_allowlist",
                "Gluetun must allowlist the exact pinned proxy subnet",
            )
        )
    if any(proxy != network and proxy.subnet_of(network) for network in allowed):
        findings.append(
            _finding(
                "proxy_lan_collapsed",
                "proxy and LAN outbound allowlist entries must stay separate",
            )
        )
    return findings


def _check_proxy_routes(services: Mapping[str, Any]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    gluetun = services.get("gluetun")
    gluetun_routers = _router_names(_labels(gluetun)) if isinstance(gluetun, Mapping) else set()
    for name in _namespace_sharing_services(services):
        owned_here = name in _router_names(_labels(services[name]))
        owned_on_gluetun = name in gluetun_routers
        if owned_here or not owned_on_gluetun:
            findings.append(
                _finding(
                    "proxy_route_owner",
                    "Gluetun must own Traefik routes for namespace-sharing services",
                    service=name,
                )
            )
    return findings


def _data_root_source(volume: Mapping[str, Any]) -> str | None:
    source = volume.get("source")
    if isinstance(source, str) and source:
        return source
    return None


def _agreed_data_root(sources: Mapping[str, str]) -> str | None:
    # Agreement across every present *arr is the configured root. A single
    # service's source is never treated as canonical when siblings disagree.
    unique = {source for source in sources.values()}
    if len(unique) <= 1:
        return next(iter(unique), None)
    counts: dict[str, int] = {}
    for source in sources.values():
        counts[source] = counts.get(source, 0) + 1
    top = max(counts.values())
    modes = [source for source, count in counts.items() if count == top]
    if len(modes) == 1 and top > 1:
        return modes[0]
    return None


def _check_arr_data_roots(services: Mapping[str, Any]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    sources: dict[str, str] = {}
    for name in ARR_DATA_SERVICES:
        service = services.get(name)
        if service is None:
            continue
        volumes = _volumes(service)
        data_roots = [volume for volume in volumes if volume.get("target") == "/data"]
        split_targets = [
            volume
            for volume in volumes
            if isinstance(volume.get("target"), str)
            and volume.get("target", "").startswith("/data/")
        ]
        if len(data_roots) != 1 or split_targets:
            findings.append(
                _finding(
                    "arr_data_root",
                    "service must mount exactly the single data root at /data",
                    service=name,
                )
            )
            continue
        source = _data_root_source(data_roots[0])
        if source is None:
            findings.append(
                _finding(
                    "arr_data_root",
                    "service must mount exactly the single data root at /data",
                    service=name,
                )
            )
            continue
        sources[name] = source
        bind = data_roots[0].get("bind")
        create_host_path = bind.get("create_host_path") if isinstance(bind, Mapping) else None
        if create_host_path is not False:
            findings.append(
                _finding(
                    "arr_create_host_path",
                    "data-root bind must set create_host_path to false",
                    service=name,
                )
            )
    agreed = _agreed_data_root(sources)
    for name, source in sources.items():
        if agreed is None or source != agreed:
            findings.append(
                _finding(
                    "arr_data_root",
                    "service must mount exactly the single data root at /data",
                    service=name,
                )
            )
    return findings


def evaluate_stack_contract(rendered: Mapping[str, Any]) -> dict[str, object]:
    """Return a structured, secret-free report for one rendered Compose mapping."""

    services = _services(rendered)
    findings = (
        _check_vpn_namespace(services)
        + _check_self_heal(services)
        + _check_arr_data_roots(services)
        + _check_proxy_routes(services)
        + _check_proxy_subnets(rendered, services)
        + _check_phases(rendered, services)
    )
    findings.sort(key=lambda item: (str(item["code"]), str(item.get("service") or "")))
    passed = not findings
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass" if passed else "fail",
        "passed": passed,
        "findings": findings,
    }
