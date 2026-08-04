"""Argument parsing and rendering for the Homeflix setup CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .state import SetupState


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="homeflix",
        description="Inspect and configure a Homeflix deployment.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit one JSON object")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="show non-secret local setup status")
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

    if arguments.command == "status":
        result = _status(root)
    else:  # pragma: no cover - argparse limits command values
        raise AssertionError(f"unhandled command {arguments.command}")

    if arguments.json_output:
        print(json.dumps(result, sort_keys=True))
    else:
        state_description = "present" if result["state_exists"] else "not created"
        print(f"Setup state: {state_description} (schema {result['schema_version']})")
    return 0
