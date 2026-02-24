from __future__ import annotations

from pathlib import Path
from typing import Any

from .sandbox import resolve_path_in_workspace


def sandbox_echo_path(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    relative_path = str(arguments.get("relative_path", "."))
    target_path = resolve_path_in_workspace(workspace_root, relative_path)

    payload: dict[str, Any] = {
        "workspace_root": str(workspace_root),
        "requested_relative_path": relative_path,
        "resolved_path": str(target_path),
        "exists": target_path.exists(),
        "is_dir": target_path.is_dir(),
    }

    if target_path.is_dir():
        payload["children"] = sorted(child.name for child in target_path.iterdir())[:50]

    return payload
