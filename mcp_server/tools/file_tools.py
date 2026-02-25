from __future__ import annotations

from pathlib import Path
from typing import Any

from .sandbox import ensure_text_size_within_limit, resolve_path_in_workspace, validate_relative_path


def create_file_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    relative_path = str(arguments.get("relative_path", "")).strip()
    content = str(arguments.get("content", ""))
    overwrite = bool(arguments.get("overwrite", False))

    validate_relative_path(relative_path)
    ensure_text_size_within_limit(content)
    target = resolve_path_in_workspace(workspace_root, relative_path)

    if target.exists() and target.is_dir():
        raise ValueError("Target path is a directory")
    if target.exists() and not overwrite:
        raise ValueError("File already exists; set overwrite=true to replace")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return {
        "ok": True,
        "path": str(target),
        "relative_path": relative_path,
        "bytes_written": len(content.encode("utf-8")),
        "overwritten": overwrite and target.exists(),
    }


def read_file_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    relative_path = str(arguments.get("relative_path", "")).strip()
    max_bytes = int(arguments.get("max_bytes", 65536))

    validate_relative_path(relative_path)
    if max_bytes < 1 or max_bytes > 200000:
        raise ValueError("max_bytes must be between 1 and 200000")

    target = resolve_path_in_workspace(workspace_root, relative_path)
    if not target.exists() or not target.is_file():
        raise ValueError("Requested file does not exist")

    raw = target.read_bytes()
    chunk = raw[:max_bytes]
    return {
        "ok": True,
        "path": str(target),
        "relative_path": relative_path,
        "truncated": len(raw) > max_bytes,
        "size_bytes": len(raw),
        "content": chunk.decode("utf-8", errors="replace"),
    }


def list_directory_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    relative_path = str(arguments.get("relative_path", ".")).strip() or "."
    include_hidden = bool(arguments.get("include_hidden", False))

    validate_relative_path(relative_path)
    target = resolve_path_in_workspace(workspace_root, relative_path)
    if not target.exists() or not target.is_dir():
        raise ValueError("Requested directory does not exist")

    entries: list[dict[str, Any]] = []
    for item in sorted(target.iterdir(), key=lambda value: value.name):
        if not include_hidden and item.name.startswith("."):
            continue
        entries.append(
            {
                "name": item.name,
                "is_dir": item.is_dir(),
                "is_file": item.is_file(),
            }
        )

    return {
        "ok": True,
        "path": str(target),
        "relative_path": relative_path,
        "entries": entries,
        "count": len(entries),
    }
