"""Generated service credentials and explicit terminal-only retrieval."""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import secrets
from typing import Callable

from .envfile import EnvDocument, update_env


JELLYFIN_USER_KEY = "JELLYFIN_ADMIN_USER"
JELLYFIN_PASSWORD_KEY = "JELLYFIN_ADMIN_PASSWORD"
GLUETUN_WIKI = "https://github.com/qdm12/gluetun-wiki"
SUPPORTED_VPN_SECRETS: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("protonvpn", "openvpn"): (
        ("VPN_USER", "VPN username: "),
        ("VPN_PASSWORD", "VPN password: "),
    ),
}
TtyReader = Callable[..., str]


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


def _normalize_vpn_choice(value: str | None) -> str:
    return (value or "").strip().casefold()


def required_vpn_secret_keys(provider: str, vpn_type: str) -> tuple[str, ...]:
    """Return the secret keys required for a supported provider/type pair."""

    schema = SUPPORTED_VPN_SECRETS.get((_normalize_vpn_choice(provider), _normalize_vpn_choice(vpn_type)))
    if schema is None:
        raise ValueError(
            f"unsupported VPN provider/type {provider}/{vpn_type}; "
            f"see {GLUETUN_WIKI} for current provider requirements"
        )
    return tuple(name for name, _prompt in schema)


def read_from_tty(prompt: str, *, confirm: bool = False, tty_path: str = "/dev/tty") -> str:
    """Read one secret from the controlling terminal, optionally confirming it."""

    try:
        descriptor = os.open(tty_path, os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except OSError as error:
        raise RuntimeError("a controlling terminal is required to enter credentials") from error
    try:
        if not os.isatty(descriptor):
            raise RuntimeError("a controlling terminal is required to enter credentials")
        with os.fdopen(descriptor, "w+", encoding="utf-8", closefd=False) as terminal:
            first = getpass.getpass(prompt, stream=terminal)
            if confirm:
                second = getpass.getpass("Confirm: ", stream=terminal)
                if first != second:
                    raise ValueError("entered values did not match")
            return first
    finally:
        os.close(descriptor)


def set_vpn_secrets(
    path: str | os.PathLike[str],
    *,
    provider: str | None = None,
    vpn_type: str | None = None,
    reader: TtyReader = read_from_tty,
) -> dict[str, object]:
    """Collect provider-specific VPN secrets from a tty and write only key status."""

    env_path = Path(path)
    document = EnvDocument.load(env_path) if env_path.exists() else EnvDocument([])
    resolved_provider = provider if provider is not None else document.get("VPN_SERVICE_PROVIDER") or "protonvpn"
    resolved_type = vpn_type if vpn_type is not None else document.get("VPN_TYPE") or "openvpn"
    schema = SUPPORTED_VPN_SECRETS.get(
        (_normalize_vpn_choice(resolved_provider), _normalize_vpn_choice(resolved_type))
    )
    if schema is None:
        raise ValueError(
            f"unsupported VPN provider/type {resolved_provider}/{resolved_type}; "
            f"see {GLUETUN_WIKI} for current provider requirements"
        )
    updates: dict[str, str] = {}
    for name, prompt in schema:
        value = reader(prompt, confirm=True)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must not be empty")
        updates[name] = value
    return update_env(env_path, updates, updates.keys())


USENET_SECRET_FIELDS = (
    ("USENET_HOST", "News server host: "),
    ("USENET_PORT", "News server port: "),
    ("USENET_USER", "News server username: "),
    ("USENET_PASSWORD", "News server password: "),
)


def set_usenet_secrets(
    path: str | os.PathLike[str],
    *,
    reader: TtyReader = read_from_tty,
) -> dict[str, object]:
    """Collect generic news-server secrets from a tty and write only key status."""

    env_path = Path(path)
    updates: dict[str, str] = {}
    for name, prompt in USENET_SECRET_FIELDS:
        value = reader(prompt, confirm=name == "USENET_PASSWORD")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")
        updates[name] = value.strip()
    port = updates["USENET_PORT"]
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("USENET_PORT is invalid")
    return update_env(env_path, updates, {"USENET_PASSWORD"})
