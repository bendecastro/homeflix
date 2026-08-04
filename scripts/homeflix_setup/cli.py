"""Argument parsing and rendering for the Homeflix setup CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .command import CommandRunner
from .discover import HostFacts, discover_host
from .state import SetupState


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="homeflix",
        description="Inspect and configure a Homeflix deployment.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit one JSON object")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "status",
        help="show non-secret local setup status",
        description="Show setup status. Invalid state returns exit status 1.",
    )
    subparsers.add_parser(
        "discover",
        help="inspect the local host without changing it",
        description="Discover Debian/Ubuntu host capabilities without persisting private facts.",
    )
    return parser


def _status(repository_root: Path) -> dict[str, object]:
    state_path = repository_root / ".homeflix" / "setup.json"
    exists = state_path.exists()
    state = SetupState.load(state_path)
    return {
        "schema_version": state.schema_version,
        "state_exists": exists,
        "checkpoints": state.checkpoints,
        "host_facts": state.host_facts,
    }


def main(argv: Sequence[str] | None = None, *, repository_root: Path | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    root = repository_root or Path(__file__).resolve().parents[2]

    discovered: HostFacts | None = None
    if arguments.command == "status":
        try:
            result = _status(root)
        except (OSError, ValueError) as error:
            if arguments.json_output:
                print(
                    json.dumps(
                        {"error": {"code": "invalid_state", "message": str(error)}},
                        sort_keys=True,
                    )
                )
            else:
                print(f"homeflix: invalid setup state: {error}", file=sys.stderr)
            return 1
    elif arguments.command == "discover":
        discovered = discover_host(CommandRunner())
        result = discovered.to_dict()
    else:  # pragma: no cover - argparse limits command values
        raise AssertionError(f"unhandled command {arguments.command}")

    if arguments.json_output:
        print(json.dumps(result, sort_keys=True))
    elif discovered is not None:
        if discovered.refusal:
            print(f"homeflix: {discovered.refusal['message']}", file=sys.stderr)
            print(f"Action: {discovered.refusal['action']}", file=sys.stderr)
        else:
            docker = "ready" if discovered.docker_daemon_reachable else "not ready"
            print(
                f"Host: {discovered.os_pretty_name} ({discovered.architecture or 'unknown architecture'})"
            )
            mounts = (
                str(len(discovered.mounts))
                if discovered.mounts_status == "ok"
                else f"unavailable ({discovered.mounts_reason})"
            )
            ports = (
                str(len(discovered.listening_ports))
                if discovered.listening_ports_status == "ok"
                else f"unavailable ({discovered.listening_ports_reason})"
            )
            graphics = (
                str(len(discovered.graphics.render_devices))
                if discovered.graphics.status == "ok"
                else f"unavailable ({discovered.graphics.reason})"
            )
            print(
                f"Docker: {docker}; mounts: {mounts}; listening ports: {ports}; "
                f"graphics devices: {graphics}"
            )
            if discovered.host_dns_status != "ok":
                print(f"Host DNS: unavailable ({discovered.host_dns_reason})")
            for gap in discovered.capability_gaps:
                print(f"Capability gap: {gap['message']}. Action: {gap['action']}")
    else:
        state_description = "present" if result["state_exists"] else "not created"
        print(f"Setup state: {state_description} (schema {result['schema_version']})")
    return 1 if discovered is not None and not discovered.supported else 0
