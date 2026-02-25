from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


MAX_FILE_BYTES = 1_000_000
MAX_TOOL_TIMEOUT_SECONDS = 120
MAX_TOOL_ARGUMENT_LENGTH = 1024


def resolve_workspace_root(workspace_root: str) -> Path:
    if not workspace_root:
        raise ValueError("WORKSPACE_ROOT is required")
    root = Path(workspace_root).expanduser().resolve()
    if not root.is_absolute():
        raise ValueError("WORKSPACE_ROOT must be an absolute path")
    if not root.exists() or not root.is_dir():
        raise ValueError("WORKSPACE_ROOT must exist and be a directory")
    return root


def resolve_path_in_workspace(workspace_root: Path, relative_path: str) -> Path:
    validate_relative_path(relative_path)
    candidate = (workspace_root / relative_path).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError as error:
        raise ValueError("Path escapes workspace sandbox") from error
    return candidate


def validate_relative_path(relative_path: str) -> None:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Path must be a non-empty string")
    if len(relative_path) > MAX_TOOL_ARGUMENT_LENGTH:
        raise ValueError("Path argument too long")
    if "\x00" in relative_path:
        raise ValueError("Path contains null byte")
    path = Path(relative_path)
    if path.is_absolute():
        raise ValueError("Absolute paths are not allowed")


def validate_timeout_seconds(timeout_seconds: int) -> int:
    if not isinstance(timeout_seconds, int):
        raise ValueError("Timeout must be an integer")
    if timeout_seconds < 1 or timeout_seconds > MAX_TOOL_TIMEOUT_SECONDS:
        raise ValueError(f"Timeout must be between 1 and {MAX_TOOL_TIMEOUT_SECONDS} seconds")
    return timeout_seconds


def ensure_text_size_within_limit(text: str) -> None:
    data = text.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"File content exceeds max allowed size ({MAX_FILE_BYTES} bytes)")


def sanitize_cli_arguments(args: list[str]) -> list[str]:
    sanitized: list[str] = []
    for value in args:
        if not isinstance(value, str):
            raise ValueError("Tool arguments must be strings")
        if len(value) > MAX_TOOL_ARGUMENT_LENGTH:
            raise ValueError("CLI argument too long")
        if "\x00" in value:
            raise ValueError("CLI argument contains null byte")
        sanitized.append(value)
    return sanitized


def run_safe_command(
    *,
    argv: list[str],
    cwd: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    safe_timeout = validate_timeout_seconds(timeout_seconds)
    safe_argv = sanitize_cli_arguments(argv)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }

    completed = subprocess.run(
        safe_argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=safe_timeout,
        check=False,
        env=env,
    )

    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "command": safe_argv,
        "timeout_seconds": safe_timeout,
    }
