from __future__ import annotations

from pathlib import Path


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
    candidate = (workspace_root / relative_path).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError as error:
        raise ValueError("Path escapes workspace sandbox") from error
    return candidate
