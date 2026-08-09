"""Generated service credentials and explicit terminal-only retrieval."""

from __future__ import annotations

import os
from pathlib import Path
import secrets

from .envfile import EnvDocument, update_env


JELLYFIN_USER_KEY = "JELLYFIN_ADMIN_USER"
JELLYFIN_PASSWORD_KEY = "JELLYFIN_ADMIN_PASSWORD"


def ensure_service_credentials(path: str | os.PathLike[str]) -> dict[str, object]:
    """Create absent Jellyfin credentials without rotating existing values."""

    env_path = Path(path)
    document = EnvDocument.load(env_path) if env_path.exists() else EnvDocument([])
    updates: dict[str, str] = {}
    statuses: list[dict[str, object]] = []
    username = document.get(JELLYFIN_USER_KEY)
    if not username:
        updates[JELLYFIN_USER_KEY] = "admin"
        statuses.append({"name": JELLYFIN_USER_KEY, "status": "generated", "secret": False})
    else:
        statuses.append({"name": JELLYFIN_USER_KEY, "status": "preserved", "secret": False})
    password = document.get(JELLYFIN_PASSWORD_KEY)
    if not password:
        updates[JELLYFIN_PASSWORD_KEY] = secrets.token_urlsafe(32)
        statuses.append({"name": JELLYFIN_PASSWORD_KEY, "status": "generated", "secret": True})
    else:
        statuses.append({"name": JELLYFIN_PASSWORD_KEY, "status": "preserved", "secret": True})
    if updates:
        update_env(env_path, updates, {JELLYFIN_PASSWORD_KEY} & updates.keys())
    return {"credentials": statuses}


def reveal_jellyfin(path: str | os.PathLike[str], *, tty_path: str = "/dev/tty") -> None:
    """Write Jellyfin credentials only to an actual controlling terminal."""

    document = EnvDocument.load(path)
    username = document.get(JELLYFIN_USER_KEY)
    password = document.get(JELLYFIN_PASSWORD_KEY)
    if not username or not password:
        raise ValueError("Jellyfin credentials have not been generated")
    try:
        descriptor = os.open(tty_path, os.O_WRONLY | getattr(os, "O_NOCTTY", 0))
    except OSError as error:
        raise RuntimeError("a controlling terminal is required to reveal credentials") from error
    try:
        if not os.isatty(descriptor):
            raise RuntimeError("a controlling terminal is required to reveal credentials")
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as terminal:
            terminal.write(f"Jellyfin administrator: {username}\nJellyfin password: {password}\n")
            terminal.flush()
    finally:
        os.close(descriptor)
