"""Small subprocess boundary used by setup primitives."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence


_REDACTION = "[REDACTED]"


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, _REDACTION)
    return value


class CommandRunner:
    """Run a command while capturing text output and redacting sensitive values."""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = False,
        redact: Sequence[str] = (),
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        secrets = tuple(redact)
        safe_command = [_redact(argument, secrets) for argument in command]
        result = subprocess.CompletedProcess(
            safe_command,
            completed.returncode,
            _redact(completed.stdout or "", secrets),
            _redact(completed.stderr or "", secrets),
        )
        if check and result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result
