"""Guarded Docker Engine preparation for supported Debian and Ubuntu hosts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import subprocess
from typing import Protocol, Sequence

from .discover import HostFacts, discover_host


DOCKER_PACKAGES = (
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
)
ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64", "amd64": "amd64"}
KEYRING_PATH = "/etc/apt/keyrings/docker.asc"
SOURCE_PATH = "/etc/apt/sources.list.d/docker.list"


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class HostPreparationPlan:
    os_id: str
    os_version_id: str
    os_codename: str
    architecture: str
    deployment_user: str
    repository_url: str
    repository_key_url: str
    repository_codename: str
    repository_architecture: str
    packages: tuple[str, ...] = ()
    repository_packages: tuple[str, ...] = ()
    mutations: tuple[dict[str, str], ...] = ()
    verification: tuple[dict[str, str], ...] = ()
    reconnect_required: bool = False
    refusal: dict[str, str] | None = None
    applied: bool = False
    commands_completed: int = 0

    @property
    def requires_apply(self) -> bool:
        return bool(self.mutations)

    def to_dict(self) -> dict[str, object]:
        return {
            "host_identity": {
                "os_id": self.os_id,
                "os_version_id": self.os_version_id,
                "os_codename": self.os_codename,
                "architecture": self.architecture,
                "deployment_user": self.deployment_user,
            },
            "repository": {
                "url": self.repository_url,
                "key_url": self.repository_key_url,
                "keyring": KEYRING_PATH,
                "source_file": SOURCE_PATH,
                "codename": self.repository_codename,
                "architecture": self.repository_architecture,
                "signed": True,
                "prerequisite_packages": list(self.repository_packages),
            },
            "packages": list(self.packages),
            "service": "docker",
            "mutations": list(self.mutations),
            "verification": list(self.verification),
            "requires_apply": self.requires_apply,
            "reconnect_required": self.reconnect_required,
            "refusal": self.refusal,
            "applied": self.applied,
            "commands_completed": self.commands_completed,
        }


def _refusal(facts: HostFacts, code: str, message: str, action: str) -> HostPreparationPlan:
    os_id = facts.os_id
    return HostPreparationPlan(
        os_id=os_id,
        os_version_id=facts.os_version_id,
        os_codename=facts.os_codename,
        architecture=facts.architecture or "",
        deployment_user=facts.deployment_user or "",
        repository_url=f"https://download.docker.com/linux/{os_id}" if os_id in {"debian", "ubuntu"} else "",
        repository_key_url=f"https://download.docker.com/linux/{os_id}/gpg" if os_id in {"debian", "ubuntu"} else "",
        repository_codename=facts.os_codename,
        repository_architecture=ARCHITECTURES.get(facts.architecture or "", ""),
        refusal={"code": code, "message": message, "action": action},
    )


def plan_host_preparation(facts: HostFacts) -> HostPreparationPlan:
    """Build an exact, read-only Docker preparation plan from discovered facts."""

    if not facts.supported or facts.os_id not in {"debian", "ubuntu"}:
        return _refusal(facts, "unsupported_distribution", "Automated Docker preparation supports only Debian and Ubuntu", "Use a supported host or the manual quickstart")
    if not facts.os_codename:
        return _refusal(facts, "repository_identity_incomplete", "The OS release codename could not be discovered", "Repair /etc/os-release before preparing Docker")
    repository_architecture = ARCHITECTURES.get(facts.architecture or "")
    if repository_architecture is None:
        return _refusal(facts, "unsupported_architecture", f"Docker repository architecture is unknown for {facts.architecture!r}", "Use a Docker-supported amd64 or arm64 host")
    if facts.deployment_user is None:
        return _refusal(facts, "identity_incomplete", "The deployment user could not be discovered", "Repair local identity discovery before preparing Docker")

    packages: tuple[str, ...] = ()
    if facts.docker_present is False:
        packages = DOCKER_PACKAGES
    elif facts.docker_present is None:
        return _refusal(facts, "docker_state_unknown", "Docker presence could not be determined safely", "Retry host discovery")
    elif facts.compose_present is False:
        packages = ("docker-compose-plugin",)
    elif facts.compose_present is None:
        return _refusal(facts, "compose_state_unknown", "Docker Compose presence could not be determined safely", "Retry host discovery")

    mutations: list[dict[str, str]] = []
    if packages:
        mutations.append({"kind": "repository", "action": "configure_signed_apt_repository"})
        mutations.append({"kind": "packages", "action": "install", "names": ",".join(packages)})
    if facts.docker_daemon_reachable is not True:
        mutations.append({"kind": "service", "service": "docker", "action": "enable_and_start"})
    needs_group = "docker" not in facts.user_groups and facts.uid != 0
    reconnect_pending = "docker" in facts.user_groups and "docker" not in facts.session_groups and facts.uid != 0
    if needs_group:
        mutations.append({"kind": "group", "group": "docker", "user": facts.deployment_user, "action": "add_user"})

    if mutations and facts.uid != 0 and facts.privilege_escalation not in {"sudo_noninteractive"}:
        code = "privilege_escalation_unavailable" if facts.privilege_escalation == "missing" else "privilege_escalation_authorization_required"
        action = "Install and authorize sudo" if facts.privilege_escalation == "missing" else "Authorize sudo in a controlling terminal, then re-run discovery"
        return _refusal(facts, code, "Docker preparation requires root privileges that are not currently available", action)

    verification_via = "sudo" if (needs_group or reconnect_pending) and facts.uid != 0 else "current_user"
    verification = (
        {"kind": "docker_cli", "command": "docker --version", "via": verification_via},
        {"kind": "compose", "command": "docker compose version", "via": verification_via},
        {"kind": "daemon", "command": "docker info", "via": verification_via},
    )
    base_url = f"https://download.docker.com/linux/{facts.os_id}"
    return HostPreparationPlan(
        os_id=facts.os_id,
        os_version_id=facts.os_version_id,
        os_codename=facts.os_codename,
        architecture=facts.architecture or "",
        deployment_user=facts.deployment_user,
        repository_url=base_url,
        repository_key_url=f"{base_url}/gpg",
        repository_codename=facts.os_codename,
        repository_architecture=repository_architecture,
        packages=packages,
        repository_packages=("ca-certificates", "curl") if packages else (),
        mutations=tuple(mutations),
        verification=verification,
        reconnect_required=needs_group or reconnect_pending,
    )


def _same_identity(plan: HostPreparationPlan, facts: HostFacts) -> bool:
    return (
        plan.os_id,
        plan.os_version_id,
        plan.os_codename,
        plan.architecture,
        plan.deployment_user,
    ) == (
        facts.os_id,
        facts.os_version_id,
        facts.os_codename,
        facts.architecture or "",
        facts.deployment_user or "",
    )


def apply_host_preparation(plan: HostPreparationPlan, runner: Runner) -> HostPreparationPlan:
    """Revalidate host identity, apply a previously rendered plan, and verify Docker."""

    if plan.refusal is not None:
        return plan
    current = discover_host(runner)
    if not _same_identity(plan, current):
        return replace(
            plan,
            refusal={
                "code": "host_identity_changed",
                "message": "OS or deployment identity changed since the plan was created",
                "action": "Create and review a new host preparation plan",
            },
        )
    if current.privilege_escalation not in {"root", "sudo_noninteractive"} and plan.mutations:
        return replace(
            plan,
            refusal={
                "code": "privilege_escalation_unavailable",
                "message": "Privilege escalation is no longer authorized",
                "action": "Re-authorize sudo and create a new plan",
            },
        )

    prefix: tuple[str, ...] = () if current.uid == 0 else ("sudo",)
    commands: list[tuple[tuple[str, ...], str | None]] = []
    if plan.packages:
        source = (
            f"deb [arch={plan.repository_architecture} signed-by={KEYRING_PATH}] "
            f"{plan.repository_url} {plan.repository_codename} stable\n"
        )
        commands.extend(
            [
                (prefix + ("apt-get", "update"), None),
                (prefix + ("apt-get", "install", "-y", *plan.repository_packages), None),
                (prefix + ("install", "-m", "0755", "-d", "/etc/apt/keyrings"), None),
                (prefix + ("curl", "-fsSL", plan.repository_key_url, "-o", KEYRING_PATH), None),
                (prefix + ("chmod", "a+r", KEYRING_PATH), None),
                (prefix + ("tee", SOURCE_PATH), source),
                (prefix + ("apt-get", "update"), None),
                (prefix + ("apt-get", "install", "-y", *plan.packages), None),
            ]
        )
    if any(item.get("kind") == "service" for item in plan.mutations):
        commands.append((prefix + ("systemctl", "enable", "--now", "docker"), None))
    if any(item.get("kind") == "group" for item in plan.mutations):
        commands.append((prefix + ("usermod", "-aG", "docker", plan.deployment_user), None))

    verify_prefix = ("sudo",) if plan.reconnect_required and current.uid != 0 else ()
    commands.extend(
        [
            (verify_prefix + ("docker", "--version"), None),
            (verify_prefix + ("docker", "compose", "version"), None),
            (verify_prefix + ("docker", "info"), None),
        ]
    )
    completed = 0
    for argv, input_text in commands:
        result = runner.run(argv, input_text=input_text, check=False)
        if result.returncode:
            return replace(
                plan,
                commands_completed=completed,
                refusal={
                    "code": "host_preparation_command_failed",
                    "message": f"Host preparation command failed with status {result.returncode}",
                    "action": "Inspect the host, then create and review a new plan",
                },
            )
        completed += 1
    return replace(plan, applied=True, commands_completed=completed)
