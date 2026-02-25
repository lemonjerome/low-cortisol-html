from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .sandbox import resolve_path_in_workspace, run_safe_command, validate_relative_path


ALLOWED_CFLAGS_PREFIXES = ("-I", "-D", "-O", "-g", "-std=")
ALLOWED_CFLAGS_EXACT = {
    "-Wall",
    "-Wextra",
    "-Werror",
    "-pedantic",
    "-fPIC",
    "-pipe",
}


def compile_c_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    source_files = arguments.get("source_files", [])
    output_binary = str(arguments.get("output_binary", "build/a.out"))
    cflags = arguments.get("cflags", ["-Wall", "-Wextra", "-std=c11"])
    timeout_seconds = int(arguments.get("timeout_seconds", 60))

    if not isinstance(source_files, list) or not source_files:
        raise ValueError("source_files must be a non-empty array")
    if not isinstance(cflags, list):
        raise ValueError("cflags must be an array")

    source_paths: list[Path] = []
    for value in source_files:
        relative = str(value)
        validate_relative_path(relative)
        source_path = resolve_path_in_workspace(workspace_root, relative)
        if not source_path.exists() or not source_path.is_file():
            raise ValueError(f"Source file does not exist: {relative}")
        source_paths.append(source_path)

    safe_cflags: list[str] = []
    for flag_value in cflags:
        flag = str(flag_value)
        if flag in ALLOWED_CFLAGS_EXACT or flag.startswith(ALLOWED_CFLAGS_PREFIXES):
            safe_cflags.append(flag)
            continue
        raise ValueError(f"Disallowed compiler flag: {flag}")

    validate_relative_path(output_binary)
    output_path = resolve_path_in_workspace(workspace_root, output_binary)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = ["cc", *safe_cflags, *[str(path) for path in source_paths], "-o", str(output_path)]
    result = run_safe_command(argv=command, cwd=workspace_root, timeout_seconds=timeout_seconds)
    result["output_binary"] = str(output_path)
    return result


def run_binary_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    binary_path = str(arguments.get("binary_path", "")).strip()
    binary_args = arguments.get("args", [])
    timeout_seconds = int(arguments.get("timeout_seconds", 10))

    validate_relative_path(binary_path)
    target = resolve_path_in_workspace(workspace_root, binary_path)
    if not target.exists() or not target.is_file():
        raise ValueError("Binary file does not exist")
    if not isinstance(binary_args, list):
        raise ValueError("args must be an array")

    command = [str(target), *[str(item) for item in binary_args]]
    result = run_safe_command(argv=command, cwd=workspace_root, timeout_seconds=timeout_seconds)
    result["binary_path"] = str(target)
    return result


def clean_build_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    targets = arguments.get("targets", ["build"])
    if not isinstance(targets, list):
        raise ValueError("targets must be an array")

    removed: list[dict[str, Any]] = []
    for value in targets:
        relative = str(value)
        validate_relative_path(relative)
        target = resolve_path_in_workspace(workspace_root, relative)
        if not target.exists():
            removed.append({"target": str(target), "removed": False, "reason": "not_found"})
            continue
        if target.is_dir():
            shutil.rmtree(target)
            removed.append({"target": str(target), "removed": True, "type": "directory"})
        else:
            target.unlink()
            removed.append({"target": str(target), "removed": True, "type": "file"})

    return {
        "ok": True,
        "removed": removed,
    }
