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
    status: str = "ok"
    reason: str | None = None

    @property
    def available(self) -> bool | None:
        return bool(self.render_devices) if self.status == "ok" else None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "render_devices": list(self.render_devices),
            "available": self.available,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


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
    listening_ports_status: str
    listening_ports_reason: str | None
    mounts: tuple[MountFact, ...]
    mounts_status: str
    mounts_reason: str | None
    docker_present: bool | None
    docker_cli_status: str
    docker_cli_reason: str | None
    compose_present: bool | None
    compose_status: str
    compose_reason: str | None
    docker_daemon_reachable: bool | None
    docker_daemon_status: str
    docker_daemon_reason: str | None
    host_nameservers: tuple[str, ...]
    host_search_domains: tuple[str, ...]
    host_dns_status: str
    host_dns_reason: str | None
    ssh_context: bool
    os_codename: str = ""
    deployment_user: str | None = None
    user_groups: tuple[str, ...] = ()
    session_groups: tuple[str, ...] = ()
    privilege_escalation: str = "unknown"
    probe_errors: dict[str, str] = field(default_factory=dict)
    capability_gaps: tuple[dict[str, str], ...] = field(default_factory=tuple)
    refusal: dict[str, str] | None = None

    def to_dict(self) -> dict[str, object]:
        if self.docker_daemon_reachable is True:
            docker_dns = {
                "status": "not_tested",
                "reason": "No non-mutating Docker DNS probe is available without creating a container",
            }
        elif self.docker_daemon_reachable is False:
            docker_dns = {
                "status": "not_tested",
                "reason": "Docker daemon is not reachable",
            }
        else:
            docker_dns = {
                "status": "not_tested",
                "reason": "Docker daemon reachability probe did not complete",
            }
        listening_ports: dict[str, object] = {
            "status": self.listening_ports_status,
            "ports": list(self.listening_ports),
        }
        if self.listening_ports_reason is not None:
            listening_ports["reason"] = self.listening_ports_reason
        mounts: dict[str, object] = {
            "status": self.mounts_status,
            "items": [mount.to_dict() for mount in self.mounts],
        }
        if self.mounts_reason is not None:
            mounts["reason"] = self.mounts_reason
        host_dns: dict[str, object] = {
            "status": self.host_dns_status,
            "nameservers": list(self.host_nameservers),
            "search": list(self.host_search_domains),
        }
        if self.host_dns_reason is not None:
            host_dns["reason"] = self.host_dns_reason
        docker: dict[str, object] = {
            "present": self.docker_present,
            "cli_status": self.docker_cli_status,
            "compose_present": self.compose_present,
            "compose_status": self.compose_status,
            "daemon_reachable": self.docker_daemon_reachable,
            "daemon_status": self.docker_daemon_status,
        }
        if self.docker_cli_reason is not None:
            docker["cli_reason"] = self.docker_cli_reason
        if self.compose_reason is not None:
            docker["compose_reason"] = self.compose_reason
        if self.docker_daemon_reason is not None:
            docker["daemon_reason"] = self.docker_daemon_reason
        return {
            "os": {
                "id": self.os_id,
                "version_id": self.os_version_id,
                "pretty_name": self.os_pretty_name,
                "codename": self.os_codename,
                "supported": self.supported,
            },
            "identity": {
                "uid": self.uid,
                "gid": self.gid,
                "user": self.deployment_user,
                "groups": list(self.user_groups),
                "session_groups": list(self.session_groups),
                "privilege_escalation": self.privilege_escalation,
            },
            "timezone": self.timezone,
            "memory_bytes": self.memory_bytes,
            "cpu": {"architecture": self.architecture, "model": self.cpu_model},
            "graphics": self.graphics.to_dict(),
            "listening_ports": listening_ports,
            "mounts": mounts,
            "docker": docker,
            "host_dns": host_dns,
            "docker_dns": docker_dns,
            "execution_context": {"ssh": self.ssh_context},
            "probe_errors": self.probe_errors,
            "capability_gaps": list(self.capability_gaps),
            "refusal": self.refusal,
        }


def _run(runner: Runner, *argv: str) -> subprocess.CompletedProcess[str]:
    try:
        return runner.run(argv, timeout=PROBE_TIMEOUT_SECONDS)
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(list(argv), 127, "", str(error))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", "probe timed out")
    except OSError as error:
        return subprocess.CompletedProcess(list(argv), 126, "", str(error))


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


def _parse_mounts(contents: str) -> tuple[MountFact, ...] | None:
    try:
        payload = json.loads(contents)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("filesystems", []), list):
        return None
    filesystems = payload.get("filesystems", [])
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


def _probe_failure_reason(
    result: subprocess.CompletedProcess[str], label: str
) -> str | None:
    if result.returncode == 0:
        return None
    if result.returncode == 127:
        return f"{label} probe command is unavailable"
    if result.returncode == 124:
        return f"{label} probe timed out"
    return f"{label} probe exited with status {result.returncode}"


def _command_probe_status(result: subprocess.CompletedProcess[str]) -> str:
    if result.returncode == 0:
        return "ok"
    if result.returncode == 127:
        return "missing"
    return "error"


def _compose_probe_status(result: subprocess.CompletedProcess[str]) -> str:
    status = _command_probe_status(result)
    if status != "error" or result.returncode != 1:
        return status
    stderr = result.stderr.casefold()
    missing_signatures = (
        "'compose' is not a docker command",
        "unknown command: docker compose",
    )
    return "missing" if any(signature in stderr for signature in missing_signatures) else "error"


def _presence_for_status(status: str) -> bool | None:
    if status == "ok":
        return True
    if status == "missing":
        return False
    return None


def _compose_probe_reason(
    result: subprocess.CompletedProcess[str], status: str
) -> str | None:
    if status == "missing":
        return "Docker Compose plugin is not available"
    return _probe_failure_reason(result, "Docker Compose")


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
    docker_status = _command_probe_status(docker_result)
    compose_status = _compose_probe_status(compose_result)
    daemon_status = _command_probe_status(daemon_result)
    docker_present = _presence_for_status(docker_status)
    compose_present = _presence_for_status(compose_status)
    daemon_reachable = _presence_for_status(daemon_status)

    uid_result = _run(runner, "id", "-u")
    gid_result = _run(runner, "id", "-g")
    user_result = _run(runner, "id", "-un")
    session_groups_result = _run(runner, "id", "-nG")
    deployment_user_value = _value(user_result)
    groups_result = (
        _run(runner, "id", "-nG", deployment_user_value)
        if deployment_user_value is not None
        else session_groups_result
    )
    sudo_result = _run(runner, "sudo", "-n", "true")
    timezone_result = _run(
        runner, "timedatectl", "show", "--property=Timezone", "--value"
    )
    memory_result = _run(runner, "cat", "/proc/meminfo")
    architecture_result = _run(runner, "uname", "-m")
    cpu_result = _run(runner, "cat", "/proc/cpuinfo")
    uid = _integer(uid_result)
    gid = _integer(gid_result)
    deployment_user = deployment_user_value
    groups_value = _value(groups_result)
    user_groups = tuple(groups_value.split()) if groups_value is not None else ()
    session_groups_value = _value(session_groups_result)
    session_groups = tuple(session_groups_value.split()) if session_groups_value is not None else ()
    if uid == 0:
        privilege_escalation = "root"
    elif sudo_result.returncode == 0:
        privilege_escalation = "sudo_noninteractive"
    elif sudo_result.returncode == 127:
        privilege_escalation = "missing"
    else:
        privilege_escalation = "authorization_required"
    timezone = _value(timezone_result)
    architecture = _value(architecture_result)
    graphics_result = _run(
        runner, "find", "/dev/dri", "-maxdepth", "1", "-name", "renderD*", "-type", "c", "-print"
    )
    graphics_reason = _probe_failure_reason(graphics_result, "graphics")
    render_devices = tuple(
        sorted(
            line
            for line in graphics_result.stdout.splitlines()
            if graphics_reason is None and line.startswith("/dev/dri/renderD")
        )
    )
    ports_result = _run(runner, "ss", "-H", "-lntu")
    ports_reason = _probe_failure_reason(ports_result, "listening-port")
    mounts_result = _run(
        runner,
        "findmnt",
        "--list",
        "--json",
        "--bytes",
        "--output",
        "TARGET,SOURCE,FSTYPE,AVAIL",
    )
    mounts_reason = _probe_failure_reason(mounts_result, "mount")
    mounts = _parse_mounts(mounts_result.stdout) if mounts_reason is None else None
    if mounts is None and mounts_reason is None:
        mounts_reason = "mount probe returned invalid data"
    resolver_result = _run(runner, "cat", "/etc/resolv.conf")
    dns_reason = _probe_failure_reason(resolver_result, "host-DNS")
    nameservers, search_domains = _parse_resolver(
        resolver_result.stdout if dns_reason is None else ""
    )

    gaps: list[dict[str, str]] = []
    if docker_status == "missing":
        gaps.append(
            {
                "code": "docker_missing",
                "message": "Docker CLI is not available",
                "action": "Install Docker Engine for this supported distribution",
            }
        )
    elif docker_status == "error":
        gaps.append(
            {
                "code": "docker_probe_error",
                "message": _probe_failure_reason(docker_result, "Docker CLI") or "Docker probe failed",
                "action": "Retry Docker discovery",
            }
        )
    if compose_status == "missing":
        gaps.append(
            {
                "code": "compose_missing",
                "message": "Docker Compose v2 is not available",
                "action": "Install the Docker Compose plugin",
            }
        )
    elif compose_status == "error":
        gaps.append(
            {
                "code": "compose_probe_error",
                "message": _probe_failure_reason(compose_result, "Docker Compose")
                or "Docker Compose probe failed",
                "action": "Retry Docker Compose discovery",
            }
        )
    if docker_status == "ok" and daemon_status == "error":
        if daemon_result.returncode == 124:
            gaps.append(
                {
                    "code": "docker_daemon_probe_error",
                    "message": "Docker daemon probe timed out",
                    "action": "Retry Docker discovery",
                }
            )
        else:
            gaps.append(
                {
                    "code": "docker_daemon_unreachable",
                    "message": "Docker is installed but its daemon is not reachable",
                    "action": "Start Docker or grant the current user access to its socket",
                }
            )

    probe_errors: dict[str, str] = {}
    scalar_probes = (
        ("uid", "UID", uid_result),
        ("gid", "GID", gid_result),
        ("user", "user", user_result),
        ("groups", "groups", groups_result),
        ("session_groups", "session groups", session_groups_result),
        ("timezone", "timezone", timezone_result),
        ("memory", "memory", memory_result),
        ("architecture", "architecture", architecture_result),
        ("cpu_model", "CPU", cpu_result),
    )
    for name, label, result in scalar_probes:
        reason = _probe_failure_reason(result, label)
        if reason is not None:
            probe_errors[name] = reason

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
        memory_bytes=(
            _parse_memory(memory_result.stdout) if memory_result.returncode == 0 else None
        ),
        architecture=architecture,
        cpu_model=_parse_cpu_model(cpu_result.stdout) if cpu_result.returncode == 0 else None,
        graphics=GraphicsFact(
            render_devices,
            status="ok" if graphics_reason is None else "error",
            reason=graphics_reason,
        ),
        listening_ports=_parse_ports(ports_result.stdout) if ports_reason is None else (),
        listening_ports_status="ok" if ports_reason is None else "error",
        listening_ports_reason=ports_reason,
        mounts=mounts or (),
        mounts_status="ok" if mounts_reason is None else "error",
        mounts_reason=mounts_reason,
        docker_present=docker_present,
        docker_cli_status=docker_status,
        docker_cli_reason=_probe_failure_reason(docker_result, "Docker CLI"),
        compose_present=compose_present,
        compose_status=compose_status,
        compose_reason=_compose_probe_reason(compose_result, compose_status),
        docker_daemon_reachable=daemon_reachable,
        docker_daemon_status=daemon_status,
        docker_daemon_reason=_probe_failure_reason(daemon_result, "Docker daemon"),
        host_nameservers=nameservers,
        host_search_domains=search_domains,
        host_dns_status="ok" if dns_reason is None else "error",
        host_dns_reason=dns_reason,
        ssh_context=any(
            name in _environment(runner) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
        ),
        os_codename=os_release.get("VERSION_CODENAME", ""),
        deployment_user=deployment_user,
        user_groups=user_groups,
        session_groups=session_groups,
        privilege_escalation=privilege_escalation,
        probe_errors=probe_errors,
        capability_gaps=tuple(gaps),
        refusal=refusal,
    )
