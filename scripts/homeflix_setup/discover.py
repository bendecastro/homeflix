"""Read-only discovery of Debian and Ubuntu host capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import subprocess
from typing import Mapping, Protocol, Sequence


PROBE_TIMEOUT_SECONDS = 5.0
SUPPORTED_DISTRIBUTIONS = {"debian", "ubuntu"}


class Runner(Protocol):
    def run(
        self, argv: Sequence[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class MountFact:
    target: str
    source: str
    filesystem: str
    free_bytes: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "source": self.source,
            "filesystem": self.filesystem,
            "free_bytes": self.free_bytes,
        }


@dataclass(frozen=True)
class GraphicsFact:
    render_devices: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.render_devices)

    def to_dict(self) -> dict[str, object]:
        return {"render_devices": list(self.render_devices), "available": self.available}


@dataclass(frozen=True)
class HostFacts:
    os_id: str
    os_version_id: str
    os_pretty_name: str
    supported: bool
    uid: int | None
    gid: int | None
    timezone: str | None
    memory_bytes: int | None
    architecture: str | None
    cpu_model: str | None
    graphics: GraphicsFact
    listening_ports: tuple[int, ...]
    mounts: tuple[MountFact, ...]
    docker_present: bool
    compose_present: bool
    docker_daemon_reachable: bool
    host_nameservers: tuple[str, ...]
    host_search_domains: tuple[str, ...]
    ssh_context: bool
    capability_gaps: tuple[dict[str, str], ...] = field(default_factory=tuple)
    refusal: dict[str, str] | None = None

    def to_dict(self) -> dict[str, object]:
        if self.docker_daemon_reachable:
            docker_dns = {
                "status": "not_tested",
                "reason": "No non-mutating Docker DNS probe is available without creating a container",
            }
        else:
            docker_dns = {
                "status": "not_tested",
                "reason": "Docker daemon is not reachable",
            }
        return {
            "os": {
                "id": self.os_id,
                "version_id": self.os_version_id,
                "pretty_name": self.os_pretty_name,
                "supported": self.supported,
            },
            "identity": {"uid": self.uid, "gid": self.gid},
            "timezone": self.timezone,
            "memory_bytes": self.memory_bytes,
            "cpu": {"architecture": self.architecture, "model": self.cpu_model},
            "graphics": self.graphics.to_dict(),
            "listening_ports": list(self.listening_ports),
            "mounts": [mount.to_dict() for mount in self.mounts],
            "docker": {
                "present": self.docker_present,
                "compose_present": self.compose_present,
                "daemon_reachable": self.docker_daemon_reachable,
            },
            "host_dns": {
                "nameservers": list(self.host_nameservers),
                "search": list(self.host_search_domains),
            },
            "docker_dns": docker_dns,
            "execution_context": {"ssh": self.ssh_context},
            "capability_gaps": list(self.capability_gaps),
            "refusal": self.refusal,
        }


def _run(runner: Runner, *argv: str) -> subprocess.CompletedProcess[str]:
    try:
        return runner.run(argv, timeout=PROBE_TIMEOUT_SECONDS)
    except (FileNotFoundError, PermissionError) as error:
        return subprocess.CompletedProcess(list(argv), 127, "", str(error))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", "probe timed out")
    except OSError as error:
        return subprocess.CompletedProcess(list(argv), 1, "", str(error))


def _value(result: subprocess.CompletedProcess[str]) -> str | None:
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _integer(result: subprocess.CompletedProcess[str]) -> int | None:
    value = _value(result)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _parse_os_release(contents: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in contents.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
            value = value.replace(r"\"", '"').replace(r"\\", "\\")
        values[key.strip()] = value
    return values


def _parse_memory(contents: str) -> int | None:
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB\s*$", contents, re.MULTILINE)
    return int(match.group(1)) * 1024 if match else None


def _parse_cpu_model(contents: str) -> str | None:
    for key in ("model name", "Model", "Hardware", "Processor"):
        match = re.search(rf"^{re.escape(key)}\s*:\s*(.+)$", contents, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return None


def _parse_ports(contents: str) -> tuple[int, ...]:
    ports: set[int] = set()
    for line in contents.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        # In ss output the local endpoint is immediately before the peer endpoint.
        match = re.search(r":(\d+)$", fields[-2])
        if match:
            ports.add(int(match.group(1)))
    return tuple(sorted(ports))


def _parse_mounts(contents: str) -> tuple[MountFact, ...]:
    try:
        payload = json.loads(contents)
    except (json.JSONDecodeError, TypeError):
        return ()
    filesystems = payload.get("filesystems", []) if isinstance(payload, dict) else []
    mounts: list[MountFact] = []
    for item in filesystems:
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        source = item.get("source")
        filesystem = item.get("fstype")
        if not all(isinstance(value, str) for value in (target, source, filesystem)):
            continue
        available = item.get("avail")
        try:
            free_bytes = int(available) if available is not None else None
        except (TypeError, ValueError):
            free_bytes = None
        mounts.append(MountFact(target, source, filesystem, free_bytes))
    return tuple(mounts)


def _parse_resolver(contents: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    nameservers: list[str] = []
    search: list[str] = []
    for line in contents.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0] == "nameserver" and len(fields) >= 2:
            nameservers.append(fields[1])
        elif fields[0] in {"search", "domain"}:
            search.extend(fields[1:])
    return tuple(nameservers), tuple(search)


def _environment(runner: Runner) -> Mapping[str, str]:
    environment = getattr(runner, "environment", None)
    return environment if isinstance(environment, Mapping) else os.environ


def discover_host(runner: Runner) -> HostFacts:
    """Collect host facts without creating files, containers, or setup state."""

    os_result = _run(runner, "cat", "/etc/os-release")
    os_release = _parse_os_release(os_result.stdout if os_result.returncode == 0 else "")
    os_id = os_release.get("ID", "unknown").lower()
    supported = os_id in SUPPORTED_DISTRIBUTIONS

    docker_result = _run(runner, "docker", "--version")
    compose_result = _run(runner, "docker", "compose", "version")
    daemon_result = _run(runner, "docker", "info", "--format", "{{json .ServerVersion}}")
    docker_present = docker_result.returncode == 0
    compose_present = compose_result.returncode == 0
    daemon_reachable = docker_present and daemon_result.returncode == 0

    uid = _integer(_run(runner, "id", "-u"))
    gid = _integer(_run(runner, "id", "-g"))
    timezone = _value(_run(runner, "timedatectl", "show", "--property=Timezone", "--value"))
    memory_result = _run(runner, "cat", "/proc/meminfo")
    architecture = _value(_run(runner, "uname", "-m"))
    cpu_result = _run(runner, "cat", "/proc/cpuinfo")
    graphics_result = _run(
        runner, "find", "/dev/dri", "-maxdepth", "1", "-name", "renderD*", "-type", "c", "-print"
    )
    render_devices = tuple(
        sorted(line for line in graphics_result.stdout.splitlines() if line.startswith("/dev/dri/renderD"))
    )
    ports_result = _run(runner, "ss", "-H", "-lntu")
    mounts_result = _run(
        runner, "findmnt", "--json", "--bytes", "--output", "TARGET,SOURCE,FSTYPE,AVAIL"
    )
    resolver_result = _run(runner, "cat", "/etc/resolv.conf")
    nameservers, search_domains = _parse_resolver(
        resolver_result.stdout if resolver_result.returncode == 0 else ""
    )

    gaps: list[dict[str, str]] = []
    if not docker_present:
        gaps.append(
            {
                "code": "docker_missing",
                "message": "Docker CLI is not available",
                "action": "Install Docker Engine for this supported distribution",
            }
        )
    if not compose_present:
        gaps.append(
            {
                "code": "compose_missing",
                "message": "Docker Compose v2 is not available",
                "action": "Install the Docker Compose plugin",
            }
        )
    if docker_present and not daemon_reachable:
        gaps.append(
            {
                "code": "docker_daemon_unreachable",
                "message": "Docker is installed but its daemon is not reachable",
                "action": "Start Docker or grant the current user access to its socket",
            }
        )

    refusal = None
    if not supported:
        refusal = {
            "code": "unsupported_distribution",
            "message": f"Automated setup does not support distribution {os_id!r}",
            "action": "Use one of the supported Debian and Ubuntu hosts, or follow the manual quickstart",
        }

    return HostFacts(
        os_id=os_id,
        os_version_id=os_release.get("VERSION_ID", ""),
        os_pretty_name=os_release.get("PRETTY_NAME", os_id),
        supported=supported,
        uid=uid,
        gid=gid,
        timezone=timezone,
        memory_bytes=_parse_memory(memory_result.stdout),
        architecture=architecture,
        cpu_model=_parse_cpu_model(cpu_result.stdout),
        graphics=GraphicsFact(render_devices),
        listening_ports=_parse_ports(ports_result.stdout),
        mounts=_parse_mounts(mounts_result.stdout),
        docker_present=docker_present,
        compose_present=compose_present,
        docker_daemon_reachable=daemon_reachable,
        host_nameservers=nameservers,
        host_search_domains=search_domains,
        ssh_context=any(
            name in _environment(runner) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
        ),
        capability_gaps=tuple(gaps),
        refusal=refusal,
    )
