"""Race-resistant reads beneath one configured application-data root."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Sequence

_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


def _verify(metadata: os.stat_result, expected_uid: int, *, directory: bool) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        raise ValueError("application configuration object has an unsafe type")
    if metadata.st_uid not in {0, expected_uid}:
        raise ValueError("application configuration owner is unsafe")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("application configuration permissions are unsafe")


def read_config_file(
    config_root: str | Path,
    components: Sequence[str],
    expected_uid: int,
    *,
    limit: int = 1024 * 1024,
) -> bytes:
    """Open fixed components with openat/no-follow and verify root, dirs, and file."""

    root = Path(config_root)
    if not root.is_absolute() or root == Path("/") or type(expected_uid) is not int or expected_uid < 0:
        raise ValueError("application configuration root is invalid")
    if not components or any(not isinstance(part, str) or not _COMPONENT.fullmatch(part) for part in components):
        raise ValueError("application configuration component is invalid")
    descriptor = os.open("/", _DIR_FLAGS)
    try:
        parts = root.parts[1:]
        for index, part in enumerate(parts):
            if part in {"", ".", ".."} or any(ord(character) < 32 or ord(character) == 127 for character in part):
                raise ValueError("application configuration root is invalid")
            child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if index == len(parts) - 1:
                _verify(os.fstat(descriptor), expected_uid, directory=True)
        for part in components[:-1]:
            child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            _verify(os.fstat(descriptor), expected_uid, directory=True)
        file_descriptor = os.open(components[-1], _FILE_FLAGS, dir_fd=descriptor)
        try:
            metadata = os.fstat(file_descriptor)
            _verify(metadata, expected_uid, directory=False)
            if metadata.st_size > limit:
                raise ValueError("application configuration file is too large")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(file_descriptor, 65536):
                total += len(chunk)
                if total > limit:
                    raise ValueError("application configuration file is too large")
                chunks.append(chunk)
            after = os.fstat(file_descriptor)
            if (
                metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
                metadata.st_size, metadata.st_mtime_ns,
            ) != (
                after.st_dev, after.st_ino, after.st_uid, after.st_mode,
                after.st_size, after.st_mtime_ns,
            ):
                raise ValueError("application configuration changed while being read")
            return b"".join(chunks)
        finally:
            os.close(file_descriptor)
    except OSError:
        raise ValueError("application configuration cannot be opened safely") from None
    finally:
        os.close(descriptor)
