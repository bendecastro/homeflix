"""Guarded Docker Engine preparation for supported Debian and Ubuntu hosts."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
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
KEYRING_STAGE_PATH = "/etc/apt/keyrings/.docker.asc.homeflix.tmp"
SOURCE_STAGE_PATH = "/etc/apt/sources.list.d/.docker.list.homeflix.tmp"
KILL_AFTER = "--kill-after=10s"
OUTER_TIMEOUT_MARGIN = 15.0


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
    uid: int | None
    gid: int | None
    configured_groups: tuple[str, ...]
    session_groups: tuple[str, ...]
    configured_groups_status: str
    session_groups_status: str
    privilege_escalation: str
    docker_present: bool | None
    compose_present: bool | None
    docker_daemon_reachable: bool | None
    docker_service_enabled: bool | None
    docker_service_status: str
    repository_url: str
    repository_key_url: str
    repository_codename: str
    repository_architecture: str
    packages: tuple[str, ...] = ()
    repository_packages: tuple[str, ...] = ()
    conflicting_packages: tuple[str, ...] = ()
    conflicting_packages_status: str = "unknown"
    mutations: tuple[dict[str, str], ...] = ()
    verification: tuple[dict[str, str], ...] = ()
    reconnect_required: bool = False
    refusal: dict[str, str] | None = None
    applied: bool = False
    commands_completed: int = 0

    @property
    def requires_apply(self) -> bool:
        return bool(self.mutations)

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            "host_identity": {
                "os_id": self.os_id,
                "os_version_id": self.os_version_id,
                "os_codename": self.os_codename,
                "architecture": self.architecture,
                "deployment_user": self.deployment_user,
                "uid": self.uid,
                "gid": self.gid,
                "configured_groups": list(self.configured_groups),
                "session_groups": list(self.session_groups),
                "configured_groups_status": self.configured_groups_status,
                "session_groups_status": self.session_groups_status,
                "privilege_escalation": self.privilege_escalation,
            },
            "repository": {
                "url": self.repository_url,
                "key_url": self.repository_key_url,
                "keyring": KEYRING_PATH,
                "source_file": SOURCE_PATH,
                "keyring_stage": KEYRING_STAGE_PATH,
                "source_stage": SOURCE_STAGE_PATH,
                "codename": self.repository_codename,
                "architecture": self.repository_architecture,
                "signed": True,
                "prerequisite_packages": list(self.repository_packages),
            },
            "packages": list(self.packages),
            "conflicting_packages": (
                list(self.conflicting_packages)
                if self.conflicting_packages_status == "ok"
                else None
            ),
            "conflicting_packages_status": self.conflicting_packages_status,
            "service": {
                "name": "docker",
                "docker_present": self.docker_present,
                "compose_present": self.compose_present,
                "daemon_reachable": self.docker_daemon_reachable,
                "enabled": self.docker_service_enabled,
                "status": self.docker_service_status,
            },
            "mutations": list(self.mutations),
            "verification": list(self.verification),
            "reconnect_required": self.reconnect_required,
        }

    @property
    def plan_fingerprint(self) -> str:
        canonical = json.dumps(
            self._fingerprint_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_dict(self) -> dict[str, object]:
        result = self._fingerprint_payload()
        result.update(
            {
                "plan_fingerprint": self.plan_fingerprint,
                "requires_apply": self.requires_apply,
                "refusal": self.refusal,
                "applied": self.applied,
                "commands_completed": self.commands_completed,
            }
        )
        return result


@dataclass(frozen=True)
class _Operation:
    operation_id: str
    argv: tuple[str, ...]
    timeout_seconds: int
    privileged: bool = True
    input_text: str | None = None


def _refusal(facts: HostFacts, code: str, message: str, action: str) -> HostPreparationPlan:
    os_id = facts.os_id
    return HostPreparationPlan(
        os_id=os_id,
        os_version_id=facts.os_version_id,
        os_codename=facts.os_codename,
        architecture=facts.architecture or "",
        deployment_user=facts.deployment_user or "",
        uid=facts.uid,
        gid=facts.gid,
        configured_groups=facts.user_groups,
        session_groups=facts.session_groups,
        configured_groups_status=facts.configured_groups_status,
        session_groups_status=facts.session_groups_status,
        privilege_escalation=facts.privilege_escalation,
        docker_present=facts.docker_present,
        compose_present=facts.compose_present,
        docker_daemon_reachable=facts.docker_daemon_reachable,
        docker_service_enabled=facts.docker_service_enabled,
        docker_service_status=facts.docker_service_status,
        repository_url=f"https://download.docker.com/linux/{os_id}" if os_id in {"debian", "ubuntu"} else "",
        repository_key_url=f"https://download.docker.com/linux/{os_id}/gpg" if os_id in {"debian", "ubuntu"} else "",
        repository_codename=facts.os_codename,
        repository_architecture=ARCHITECTURES.get(facts.architecture or "", ""),
        conflicting_packages=facts.conflicting_packages,
        conflicting_packages_status=facts.conflicting_packages_status,
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
    if (
        facts.uid is None
        or facts.gid is None
        or facts.deployment_user is None
        or facts.configured_groups_status != "ok"
        or facts.session_groups_status != "ok"
    ):
        return _refusal(facts, "identity_incomplete", "UID, GID, user, or group membership could not be discovered reliably", "Repair identity discovery before preparing Docker")

    packages: tuple[str, ...] = ()
    if facts.docker_present is False:
        packages = DOCKER_PACKAGES
    elif facts.docker_present is None:
        return _refusal(facts, "docker_state_unknown", "Docker presence could not be determined safely", "Retry host discovery")
    elif facts.compose_present is False:
        packages = ("docker-compose-plugin",)
    elif facts.compose_present is None:
        return _refusal(facts, "compose_state_unknown", "Docker Compose presence could not be determined safely", "Retry host discovery")

    if packages and facts.conflicting_packages_status != "ok":
        return _refusal(facts, "conflicting_packages_unknown", "Installed conflicting Docker packages could not be determined safely", "Repair dpkg-query discovery before configuring Docker's repository")
    if packages and facts.conflicting_packages:
        names = ", ".join(facts.conflicting_packages)
        return _refusal(facts, "conflicting_packages_installed", f"Conflicting packages are installed: {names}", "Remove conflicting packages manually, then create a new plan")

    installing_engine = facts.docker_present is False
    if facts.docker_service_enabled is None and not (
        installing_engine and facts.docker_service_status == "not_found"
    ):
        return _refusal(facts, "docker_service_state_unknown", "Docker service enablement could not be determined safely", "Repair systemd service discovery before preparing Docker")

    mutations: list[dict[str, str]] = []
    if packages:
        mutations.append({"kind": "repository", "action": "configure_signed_apt_repository"})
        mutations.append({"kind": "packages", "action": "install", "names": ",".join(packages)})
    if facts.docker_daemon_reachable is not True or facts.docker_service_enabled is not True:
        mutations.append({"kind": "service", "service": "docker", "action": "enable_and_start"})
    needs_group = "docker" not in facts.user_groups and facts.uid != 0
    reconnect_pending = "docker" in facts.user_groups and "docker" not in facts.session_groups and facts.uid != 0
    if needs_group:
        mutations.append({"kind": "group", "group": "docker", "user": facts.deployment_user, "action": "add_user"})

    if mutations and facts.uid != 0 and facts.privilege_escalation != "sudo_noninteractive":
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
        uid=facts.uid,
        gid=facts.gid,
        configured_groups=facts.user_groups,
        session_groups=facts.session_groups,
        configured_groups_status=facts.configured_groups_status,
        session_groups_status=facts.session_groups_status,
        privilege_escalation=facts.privilege_escalation,
        docker_present=facts.docker_present,
        compose_present=facts.compose_present,
        docker_daemon_reachable=facts.docker_daemon_reachable,
        docker_service_enabled=facts.docker_service_enabled,
        docker_service_status=facts.docker_service_status,
        repository_url=base_url,
        repository_key_url=f"{base_url}/gpg",
        repository_codename=facts.os_codename,
        repository_architecture=repository_architecture,
        packages=packages,
        repository_packages=("ca-certificates", "curl") if packages else (),
        conflicting_packages=facts.conflicting_packages,
        conflicting_packages_status=facts.conflicting_packages_status,
        mutations=tuple(mutations),
        verification=verification,
        reconnect_required=needs_group or reconnect_pending,
    )


def _bounded_argv(operation: _Operation, *, root: bool) -> tuple[str, ...]:
    boundary = ("timeout", "--foreground", KILL_AFTER, f"{operation.timeout_seconds}s")
    if operation.privileged and not root:
        return ("sudo", "-n") + boundary + operation.argv
    return boundary + operation.argv


def _operations(plan: HostPreparationPlan, *, root: bool) -> tuple[_Operation, ...]:
    operations: list[_Operation] = []
    if plan.packages:
        source = (
            f"deb [arch={plan.repository_architecture} signed-by={KEYRING_PATH}] "
            f"{plan.repository_url} {plan.repository_codename} stable\n"
        )
        operations.extend(
            [
                _Operation("apt_metadata_before_repository", ("apt-get", "update"), 300),
                _Operation("install_repository_prerequisites", ("apt-get", "install", "-y", *plan.repository_packages), 300),
                _Operation("create_keyring_directory", ("install", "-m", "0755", "-d", "/etc/apt/keyrings"), 30),
                _Operation("create_sources_directory", ("install", "-m", "0755", "-d", "/etc/apt/sources.list.d"), 30),
                _Operation("cleanup_repository_stage_before", ("rm", "-f", KEYRING_STAGE_PATH, SOURCE_STAGE_PATH), 30),
                _Operation("download_repository_key", ("curl", "-fsSL", plan.repository_key_url, "-o", KEYRING_STAGE_PATH), 60),
                _Operation("validate_repository_key_nonempty", ("test", "-s", KEYRING_STAGE_PATH), 30),
                _Operation("validate_repository_key_format", ("grep", "-q", "-m", "1", "^-----BEGIN PGP PUBLIC KEY BLOCK-----$", KEYRING_STAGE_PATH), 30),
                _Operation("set_repository_key_mode", ("chmod", "0644", KEYRING_STAGE_PATH), 30),
                _Operation("stage_repository_source", ("tee", SOURCE_STAGE_PATH), 30, input_text=source),
                _Operation("validate_repository_source_nonempty", ("test", "-s", SOURCE_STAGE_PATH), 30),
                _Operation("validate_repository_source_content", ("grep", "-Fqx", source.rstrip("\n"), SOURCE_STAGE_PATH), 30),
                _Operation("set_repository_source_mode", ("chmod", "0644", SOURCE_STAGE_PATH), 30),
                _Operation("publish_repository_key", ("mv", "-f", KEYRING_STAGE_PATH, KEYRING_PATH), 30),
                _Operation("publish_repository_source", ("mv", "-f", SOURCE_STAGE_PATH, SOURCE_PATH), 30),
                _Operation("apt_metadata_after_repository", ("apt-get", "update"), 300),
                _Operation("install_docker_packages", ("apt-get", "install", "-y", *plan.packages), 600),
            ]
        )
    if any(item.get("kind") == "service" for item in plan.mutations):
        operations.append(_Operation("enable_and_start_docker", ("systemctl", "enable", "--now", "docker"), 90))
    if any(item.get("kind") == "group" for item in plan.mutations):
        operations.append(_Operation("add_deployment_user_to_docker_group", ("usermod", "-aG", "docker", plan.deployment_user), 30))

    verify_as_root = plan.reconnect_required and not root
    operations.extend(
        [
            _Operation("verify_docker_cli", ("docker", "--version"), 30, privileged=verify_as_root),
            _Operation("verify_docker_compose", ("docker", "compose", "version"), 30, privileged=verify_as_root),
            _Operation("verify_docker_daemon", ("docker", "info"), 30, privileged=verify_as_root),
        ]
    )
    return tuple(operations)


def _failure(plan: HostPreparationPlan, operation_id: str, completed: int) -> HostPreparationPlan:
    return replace(
        plan,
        commands_completed=completed,
        refusal={
            "code": "host_preparation_operation_failed",
            "operation": operation_id,
            "message": "A bounded host preparation operation failed",
            "action": "Inspect the host, then create and review a new plan",
        },
    )


def _cleanup_staged_files(runner: Runner, *, root: bool) -> None:
    cleanup = _Operation(
        "cleanup_repository_stage_after",
        ("rm", "-f", KEYRING_STAGE_PATH, SOURCE_STAGE_PATH),
        30,
    )
    try:
        runner.run(
            _bounded_argv(cleanup, root=root),
            check=False,
            timeout=cleanup.timeout_seconds + OUTER_TIMEOUT_MARGIN,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


def apply_host_preparation(
    plan: HostPreparationPlan,
    runner: Runner,
    *,
    confirm_plan: str,
) -> HostPreparationPlan:
    """Rebuild and bind an approved plan before bounded, atomic mutations."""

    if plan.refusal is not None:
        return plan
    if confirm_plan != plan.plan_fingerprint:
        return replace(
            plan,
            refusal={
                "code": "plan_confirmation_mismatch",
                "message": "The supplied plan fingerprint does not match the reviewed plan",
                "action": "Review a fresh plan and provide its exact fingerprint",
            },
        )

    current = discover_host(runner)
    rebuilt = plan_host_preparation(current)
    if rebuilt.plan_fingerprint != plan.plan_fingerprint or rebuilt._fingerprint_payload() != plan._fingerprint_payload():
        return replace(
            plan,
            refusal={
                "code": "plan_changed",
                "message": "Host state or the complete mutation plan changed after review",
                "action": "Review and confirm a newly discovered plan",
            },
        )
    if rebuilt.refusal is not None:
        return replace(plan, refusal=rebuilt.refusal)

    root = current.uid == 0
    completed = 0
    cleanup_needed = bool(plan.packages)
    try:
        for operation in _operations(rebuilt, root=root):
            try:
                result = runner.run(
                    _bounded_argv(operation, root=root),
                    input_text=operation.input_text,
                    check=False,
                    timeout=operation.timeout_seconds + OUTER_TIMEOUT_MARGIN,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                return _failure(plan, operation.operation_id, completed)
            if result.returncode:
                return _failure(plan, operation.operation_id, completed)
            completed += 1
    finally:
        if cleanup_needed:
            _cleanup_staged_files(runner, root=root)
    return replace(plan, applied=True, commands_completed=completed)
