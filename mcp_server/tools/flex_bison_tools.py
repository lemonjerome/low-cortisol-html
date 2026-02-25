from __future__ import annotations

from pathlib import Path
from typing import Any

from .sandbox import resolve_path_in_workspace, run_safe_command, validate_relative_path


ALLOWED_LINK_FLAGS_PREFIXES = ("-l", "-L", "-Wl,")
ALLOWED_LINK_FLAGS_EXACT = {"-lm", "-lfl"}


def generate_lexer_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    lex_file = str(arguments.get("lex_file", "")).strip()
    output_c = str(arguments.get("output_c", "build/lex.yy.c"))
    timeout_seconds = int(arguments.get("timeout_seconds", 60))

    validate_relative_path(lex_file)
    validate_relative_path(output_c)

    lex_path = resolve_path_in_workspace(workspace_root, lex_file)
    output_path = resolve_path_in_workspace(workspace_root, output_c)
    if not lex_path.exists() or not lex_path.is_file():
        raise ValueError("lex_file does not exist")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = ["flex", "-o", str(output_path), str(lex_path)]
    result = run_safe_command(argv=command, cwd=workspace_root, timeout_seconds=timeout_seconds)
    result["generated_file"] = str(output_path)
    return result


def generate_parser_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    grammar_file = str(arguments.get("grammar_file", "")).strip()
    output_c = str(arguments.get("output_c", "build/parser.tab.c"))
    output_h = str(arguments.get("output_h", "build/parser.tab.h"))
    timeout_seconds = int(arguments.get("timeout_seconds", 60))

    validate_relative_path(grammar_file)
    validate_relative_path(output_c)
    validate_relative_path(output_h)

    grammar_path = resolve_path_in_workspace(workspace_root, grammar_file)
    output_c_path = resolve_path_in_workspace(workspace_root, output_c)
    output_h_path = resolve_path_in_workspace(workspace_root, output_h)
    if not grammar_path.exists() or not grammar_path.is_file():
        raise ValueError("grammar_file does not exist")

    output_c_path.parent.mkdir(parents=True, exist_ok=True)
    output_h_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "bison",
        "-d",
        f"--defines={output_h_path}",
        "-o",
        str(output_c_path),
        str(grammar_path),
    ]
    result = run_safe_command(argv=command, cwd=workspace_root, timeout_seconds=timeout_seconds)
    result["generated_c"] = str(output_c_path)
    result["generated_h"] = str(output_h_path)
    return result


def link_compiler_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    source_files = arguments.get("source_files", [])
    output_binary = str(arguments.get("output_binary", "build/compiler"))
    extra_flags = arguments.get("extra_flags", [])
    timeout_seconds = int(arguments.get("timeout_seconds", 60))

    if not isinstance(source_files, list) or not source_files:
        raise ValueError("source_files must be a non-empty array")
    if not isinstance(extra_flags, list):
        raise ValueError("extra_flags must be an array")

    sources: list[Path] = []
    for value in source_files:
        relative = str(value)
        validate_relative_path(relative)
        path = resolve_path_in_workspace(workspace_root, relative)
        if not path.exists() or not path.is_file():
            raise ValueError(f"Source file does not exist: {relative}")
        sources.append(path)

    validate_relative_path(output_binary)
    output_path = resolve_path_in_workspace(workspace_root, output_binary)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    safe_flags: list[str] = []
    for value in extra_flags:
        flag = str(value)
        if flag in ALLOWED_LINK_FLAGS_EXACT or flag.startswith(ALLOWED_LINK_FLAGS_PREFIXES):
            safe_flags.append(flag)
            continue
        raise ValueError(f"Disallowed linker flag: {flag}")

    command = ["cc", *[str(path) for path in sources], *safe_flags, "-o", str(output_path)]
    result = run_safe_command(argv=command, cwd=workspace_root, timeout_seconds=timeout_seconds)
    result["output_binary"] = str(output_path)
    return result
