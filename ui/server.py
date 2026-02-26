from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = Path(__file__).resolve().parent
WORKSPACE_PREFIX = "lch_"


def _default_workspaces_root() -> Path:
    return (Path.home() / "Desktop" / "lch_workspaces").resolve()


def _is_container_runtime() -> bool:
    if Path("/.dockerenv").exists():
        return True
    container_env = os.environ.get("container", "").strip().lower()
    return container_env in {"docker", "podman", "container"}


def folder_chooser_capability() -> dict[str, str | bool]:
    if _is_container_runtime() and not shutil.which("osascript"):
        return {
            "available": False,
            "reason": "Folder chooser is unavailable in container runtime (no host GUI bridge). Paste an absolute path.",
        }

    if shutil.which("osascript"):
        return {"available": True, "reason": ""}

    if shutil.which("powershell"):
        return {"available": True, "reason": ""}

    if shutil.which("zenity"):
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            return {"available": True, "reason": ""}
        return {
            "available": False,
            "reason": "zenity is installed but no GUI display is available. Paste an absolute path.",
        }

    if shutil.which("kdialog"):
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            return {"available": True, "reason": ""}
        return {
            "available": False,
            "reason": "kdialog is installed but no GUI display is available. Paste an absolute path.",
        }

    return {
        "available": False,
        "reason": "No supported folder chooser is installed in this runtime. Paste an absolute path.",
    }


@dataclass
class AppState:
    lock: Lock = field(default_factory=Lock)
    workspaces_root: Path = field(default_factory=_default_workspaces_root)
    current_project: Path | None = None
    project_structure_summary: str = ""
    chat_history: list[dict[str, str]] = field(default_factory=list)
    active_process: subprocess.Popen[str] | None = None
    stop_requested: bool = False

    def clear_chat_memory(self) -> None:
        self.chat_history.clear()


STATE = AppState()
STATE.workspaces_root.mkdir(parents=True, exist_ok=True)


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def ndjson_event(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    handler.wfile.write(line)
    handler.wfile.flush()


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(content_length) if content_length > 0 else b"{}"
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("JSON body must be an object")
    return parsed


def validate_absolute_dir(path_text: str) -> Path:
    candidate = Path(path_text).expanduser().resolve()
    if not candidate.is_absolute():
        raise ValueError("Path must be absolute")
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError("Path must exist and be a directory")
    return candidate


def ensure_workspace_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise ValueError("Workspace name is required")
    if "/" in trimmed or "\\" in trimmed:
        raise ValueError("Workspace name must not include path separators")
    if not trimmed.startswith(WORKSPACE_PREFIX):
        raise ValueError("Warning: Workspace directory must start with 'lch_'")
    return trimmed


def ensure_prefixed_directory_name(path_value: Path, *, label: str) -> None:
    if not path_value.name.startswith(WORKSPACE_PREFIX):
        raise ValueError(f"Warning: {label} must start with 'lch_'")


def summarize_structure(root: Path, *, max_entries: int = 250) -> str:
    rows: list[str] = []
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= max_entries:
            rows.append("- ... (truncated)")
            break
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.is_dir():
            rows.append(f"- {rel}/")
        else:
            rows.append(f"- {rel}")
        count += 1
    return "\n".join(rows) if rows else "- (empty project)"


def resolve_main_html(project_root: Path) -> Path | None:
    candidates = [project_root / "index.html", project_root / "main.html"]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def choose_folder_dialog() -> Path:
    capability = folder_chooser_capability()
    if not bool(capability.get("available", False)):
        raise RuntimeError(str(capability.get("reason", "Folder chooser unavailable")))

    attempts: list[str] = []

    if shutil.which("osascript"):
        script = 'POSIX path of (choose folder with prompt "Choose a workspace parent directory")'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return validate_absolute_dir(result.stdout.strip())
        attempts.append(result.stderr.strip() or "osascript chooser unavailable")

    if shutil.which("powershell"):
        command = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$dialog.Description = 'Choose a workspace parent directory';"
            "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {"
            "  $dialog.SelectedPath"
            "}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return validate_absolute_dir(result.stdout.strip())
        attempts.append(result.stderr.strip() or "powershell chooser unavailable")

    if shutil.which("zenity"):
        result = subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Choose a workspace parent directory"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return validate_absolute_dir(result.stdout.strip())
        attempts.append(result.stderr.strip() or "zenity chooser unavailable")

    if shutil.which("kdialog"):
        result = subprocess.run(
            ["kdialog", "--getexistingdirectory", str(Path.home())],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return validate_absolute_dir(result.stdout.strip())
        attempts.append(result.stderr.strip() or "kdialog chooser unavailable")

    detail = " | ".join(item for item in attempts if item)[:600]
    if detail:
        raise RuntimeError(
            "Folder chooser is unavailable in this runtime. Paste an absolute path manually. "
            f"Details: {detail}"
        )
    raise RuntimeError("Folder chooser is unavailable in this runtime. Paste an absolute path manually.")


def _normalize_tool_token(value: str) -> str:
    compact = value.strip()
    compact = re.sub(r"\s*_\s*", "_", compact)
    compact = re.sub(r"\s+", "", compact)
    return compact


def _normalize_mapping_keys(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key_text = str(raw_key).strip()
            key_text = re.sub(r"\s*_\s*", "_", key_text)
            key_text = re.sub(r"\s+", " ", key_text)
            normalized[key_text] = _normalize_mapping_keys(raw_value)
        return normalized
    if isinstance(value, list):
        return [_normalize_mapping_keys(item) for item in value]
    return value


def _is_live_action_ready(tool_name: str, arguments: dict[str, Any]) -> bool:
    required_args: dict[str, list[str]] = {
        "create_file": ["relative_path", "content"],
        "append_to_file": ["relative_path", "content"],
        "insert_after_marker": ["relative_path", "marker", "content"],
        "replace_range": ["relative_path", "start_line", "end_line", "content"],
        "read_file": ["relative_path"],
        "validate_web_app": ["app_dir"],
        "run_unit_tests": ["test_file"],
        "plan_web_build": ["summary"],
        "scaffold_web_app": ["app_dir"],
    }
    required = required_args.get(tool_name)
    if not required:
        return True
    return all(str(arguments.get(key, "")).strip() for key in required)


def _normalize_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_mapping_keys(arguments) if isinstance(arguments, dict) else {}
    if tool_name in {"create_file", "read_file", "append_to_file", "replace_range", "insert_after_marker"}:
        if "file_path" in normalized and "relative_path" not in normalized:
            normalized["relative_path"] = normalized.get("file_path")
    if tool_name == "replace_range" and "replacement_text" in normalized and "content" not in normalized:
        normalized["content"] = normalized.get("replacement_text")
    return normalized


def _extract_json_payloads(text: str) -> list[Any]:
    payloads: list[Any] = []
    decoder = json.JSONDecoder()

    marker = "```"
    blocks: list[str] = []
    cursor = 0
    while True:
        start = text.find(marker, cursor)
        if start == -1:
            break
        end = text.find(marker, start + len(marker))
        if end == -1:
            break
        block = text[start + len(marker) : end].strip()
        if block.lower().startswith("json"):
            block = block[4:].strip()
        if block:
            blocks.append(block)
        cursor = end + len(marker)

    raw = text.strip()
    candidates = [raw] if raw else []
    candidates.extend(blocks)

    for candidate in candidates:
        index = 0
        length = len(candidate)
        while index < length:
            while index < length and candidate[index].isspace():
                index += 1
            if index >= length:
                break
            try:
                payload, end_index = decoder.raw_decode(candidate, index)
            except json.JSONDecodeError:
                break
            payloads.append(payload)
            index = end_index

    return payloads


def _extract_all_tool_calls_from_text(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Extract all unique tool calls from a complete agent response text.

    Parses JSON code blocks and raw JSON objects for tool-call-shaped payloads.
    Returns deduplicated list of (tool_name, arguments) tuples.
    """
    results: list[tuple[str, dict[str, Any]]] = []
    seen_keys: set[str] = set()

    for parsed in _extract_json_payloads(text):
        if not isinstance(parsed, dict):
            continue
        raw_name = str(parsed.get("name", "")).strip()
        tool_name = _normalize_tool_token(raw_name)
        if not tool_name:
            continue
        arguments = _normalize_tool_arguments(tool_name, parsed.get("arguments", {}))

        if not _is_live_action_ready(tool_name, arguments):
            continue

        key = json.dumps({"tool": tool_name, "arguments": arguments}, sort_keys=True)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        results.append((tool_name, arguments))

    return results


def _extract_response_envelopes(text: str) -> dict[str, Any]:
    reasons: list[str] = []
    chats: list[str] = []
    tools: list[tuple[str, dict[str, Any]]] = []
    seen_tools: set[str] = set()

    stripped = text.strip()

    def consume_payload(payload: Any) -> None:
        if isinstance(payload, list):
            for item in payload:
                consume_payload(item)
            return
        if not isinstance(payload, dict):
            return

        payload_type = str(payload.get("type", "")).strip().lower()
        if payload_type == "reason":
            reason_text = str(payload.get("text", payload.get("message", payload.get("content", "")))).strip()
            if reason_text:
                reasons.append(reason_text)
            return

        if payload_type == "chat":
            chat_text = str(payload.get("text", payload.get("message", payload.get("content", "")))).strip()
            if chat_text:
                chats.append(chat_text)
            return

        if payload_type == "tool":
            nested_tool = payload.get("tool")
            if isinstance(nested_tool, dict):
                name = _normalize_tool_token(str(nested_tool.get("name", "")).strip())
                args = nested_tool.get("arguments", nested_tool.get("args", {}))
            else:
                name = _normalize_tool_token(str(payload.get("name", "")).strip())
                args = payload.get("arguments", payload.get("args", {}))

            if not isinstance(args, dict):
                args = {}
            args = _normalize_tool_arguments(name, args)
            if name and _is_live_action_ready(name, args):
                key = json.dumps({"tool": name, "arguments": args}, sort_keys=True)
                if key not in seen_tools:
                    seen_tools.add(key)
                    tools.append((name, args))
            return

        # Fallback non-typed tool shape
        name = _normalize_tool_token(str(payload.get("name", "")).strip())
        args = payload.get("arguments", {})
        if name:
            if not isinstance(args, dict):
                args = {}
            args = _normalize_tool_arguments(name, args)
            if _is_live_action_ready(name, args):
                key = json.dumps({"tool": name, "arguments": args}, sort_keys=True)
                if key not in seen_tools:
                    seen_tools.add(key)
                    tools.append((name, args))

    for parsed in _extract_json_payloads(text):
        consume_payload(parsed)

    # Fallback: if no explicit envelopes were parsed and there are no tool calls,
    # keep conversational reasoning text only when it's not an obvious code fence marker.
    if not tools:
        tools = _extract_all_tool_calls_from_text(text)

    if not reasons and not chats and not tools and stripped and stripped not in {"```", "```json"}:
        reasons.append(stripped)

    return {
        "reasons": reasons,
        "chats": chats,
        "tools": tools,
    }


def _unwrap_response_payload(raw_text: str) -> str:
    payload = raw_text.strip()
    if not payload:
        return ""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if isinstance(parsed, dict) and "content" in parsed:
        return str(parsed.get("content", ""))
    if isinstance(parsed, str):
        return parsed
    return payload


def _extract_chat_text_for_ui(final_message: str) -> str:
    envelopes = _extract_response_envelopes(final_message)
    chats = envelopes.get("chats", [])
    if isinstance(chats, list):
        merged = "\n\n".join(str(item).strip() for item in chats if str(item).strip())
        if merged:
            return merged
    return final_message


def build_task_with_context(user_message: str) -> str:
    with STATE.lock:
        project = STATE.current_project
        structure = STATE.project_structure_summary
        history = list(STATE.chat_history[-8:])

    if project is None:
        raise ValueError("No project is currently open")

    main_html = resolve_main_html(project)
    landing_line = (
        f"Landing page file detected: {main_html.name}."
        if main_html is not None
        else "Landing page convention: use index.html if present, otherwise main.html as fallback."
    )

    history_lines = []
    for item in history:
        role = item.get("role", "unknown")
        content = item.get("content", "")[:500]
        history_lines.append(f"- {role}: {content}")
    history_text = "\n".join(history_lines) if history_lines else "- none"

    return (
        "Project context follows. Learn and use this workspace structure for all edits.\n"
        f"Workspace absolute path: {project}\n"
        f"{landing_line}\n"
        "Workspace structure:\n"
        f"{structure}\n\n"
        "Conversation memory (ephemeral for current app session):\n"
        f"{history_text}\n\n"
        "Build policy:\n"
        "- Work in major development phases with substantial code batches.\n"
        "- Start with a concrete multi-phase plan, then execute one phase in larger chunks before moving on.\n"
        "- Do validation/testing at phase checkpoints, not after every minor file change.\n"
        "- Do not finish after scaffold/placeholder output.\n"
        "- For note apps, implement create/edit/delete/manage flows with functional UI + behavior.\n"
        "- Add a real JS test file (tests.js or *.test.js) and run it before DONE.\n\n"
        "Response protocol (STRICT JSON only):\n"
        "- Every assistant response must be JSON (object or array of objects).\n"
        "- Use envelope objects with field `type`:\n"
        "  - {\"type\":\"reason\",\"text\":\"...\"} for reasoning/planning/debug thoughts.\n"
        "  - {\"type\":\"tool\",\"name\":\"tool_name\",\"arguments\":{...}} for tool calls.\n"
        "  - {\"type\":\"chat\",\"text\":\"...\"} for user-facing chat summaries.\n"
        "- During planning/reasoning phases, always inspect workspace first:\n"
        "  1) call list_directory on '.' (and relevant subdirs),\n"
        "  2) decide which files matter,\n"
        "  3) call read_file for those files before implementing.\n"
        "- Keep reasoning conversational English in `type=reason` (not raw tool JSON).\n"
        "- Emit `type=chat` only when run is finishing or when blockers/errors must be reported to user.\n\n"
        "User request:\n"
        f"{user_message}\n\n"
        "Return phased progress through tool usage and finish with DONE: when complete."
    )


class UiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._serve_static("index.html", "text/html; charset=utf-8")
        if parsed.path == "/style.css":
            return self._serve_static("style.css", "text/css; charset=utf-8")
        if parsed.path == "/script.js":
            return self._serve_static("script.js", "application/javascript; charset=utf-8")
        if parsed.path == "/api/status":
            chooser = folder_chooser_capability()
            with STATE.lock:
                payload = {
                    "ok": True,
                    "workspaces_root": str(STATE.workspaces_root),
                    "current_project": str(STATE.current_project) if STATE.current_project else None,
                    "main_html": str(resolve_main_html(STATE.current_project)) if STATE.current_project else None,
                    "folder_chooser_available": bool(chooser.get("available", False)),
                    "folder_chooser_reason": str(chooser.get("reason", "")),
                }
            return json_response(self, HTTPStatus.OK, payload)
        if parsed.path.startswith("/workspace/"):
            relative = parsed.path.removeprefix("/workspace/")
            return self._serve_workspace_file(relative)

        return json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        try:
            if parsed.path == "/api/set-workspaces-root":
                payload = read_json(self)
                requested = str(payload.get("path", "")).strip()
                target = Path(requested).expanduser().resolve()
                if not target.is_absolute():
                    raise ValueError("Path must be absolute")
                target.mkdir(parents=True, exist_ok=True)
                validated = validate_absolute_dir(str(target))
                ensure_prefixed_directory_name(validated, label="Workspace parent directory")
                with STATE.lock:
                    STATE.workspaces_root = validated
                return json_response(self, HTTPStatus.OK, {"ok": True, "workspaces_root": str(validated)})

            if parsed.path == "/api/choose-folder":
                selected = choose_folder_dialog()
                return json_response(self, HTTPStatus.OK, {"ok": True, "path": str(selected)})

            if parsed.path == "/api/create-project":
                payload = read_json(self)
                parent_dir = str(payload.get("parentDir", "")).strip()
                workspace_name = ensure_workspace_name(str(payload.get("workspaceName", "")))
                parent = validate_absolute_dir(parent_dir)
                ensure_prefixed_directory_name(parent, label="Parent directory")
                project = (parent / workspace_name).resolve()
                if project.exists():
                    raise ValueError("Project folder already exists")
                project.mkdir(parents=False, exist_ok=False)
                with STATE.lock:
                    STATE.current_project = project
                    STATE.project_structure_summary = summarize_structure(project)
                    STATE.clear_chat_memory()
                return json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "project": str(project),
                        "main_html": str(resolve_main_html(project)) if resolve_main_html(project) else None,
                    },
                )

            if parsed.path == "/api/open-project":
                payload = read_json(self)
                requested = validate_absolute_dir(str(payload.get("projectPath", "")).strip())
                name = requested.name
                if not name.startswith(WORKSPACE_PREFIX):
                    raise ValueError("Warning: Only folders starting with 'lch_' can be opened")
                with STATE.lock:
                    STATE.current_project = requested
                    STATE.project_structure_summary = summarize_structure(requested)
                    STATE.clear_chat_memory()
                main_html = resolve_main_html(requested)
                return json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "project": str(requested),
                        "main_html": str(main_html) if main_html else None,
                    },
                )

            if parsed.path == "/api/open-main-html":
                with STATE.lock:
                    project = STATE.current_project
                if project is None:
                    raise ValueError("No open project")
                main_html = resolve_main_html(project)
                if main_html is None:
                    raise ValueError("No index.html or main.html found in current project")
                relative = main_html.relative_to(project).as_posix()
                return json_response(
                    self,
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "main_html": str(main_html),
                        "workspace_url": f"/workspace/{relative}",
                    },
                )

            if parsed.path == "/api/clear-chat":
                with STATE.lock:
                    STATE.clear_chat_memory()
                return json_response(self, HTTPStatus.OK, {"ok": True})

            if parsed.path == "/api/stop":
                with STATE.lock:
                    process = STATE.active_process
                    if process is None or process.poll() is not None:
                        STATE.active_process = None
                        STATE.stop_requested = False
                        return json_response(self, HTTPStatus.OK, {"ok": True, "stopped": False})
                    STATE.stop_requested = True

                process.terminate()
                return json_response(self, HTTPStatus.OK, {"ok": True, "stopped": True})

            if parsed.path == "/api/chat":
                payload = read_json(self)
                user_message = str(payload.get("message", "")).strip()
                if not user_message:
                    raise ValueError("Message is required")

                with STATE.lock:
                    if STATE.current_project is None:
                        raise ValueError("Open or create a project before chatting")
                    if STATE.active_process is not None and STATE.active_process.poll() is None:
                        raise ValueError("A model run is already in progress")
                    STATE.chat_history.append({"role": "user", "content": user_message})

                task = build_task_with_context(user_message)
                command = [
                    sys.executable,
                    "orchestrator/main_orchestrator.py",
                    "--workspace-root",
                    str(STATE.current_project),
                    "--task",
                    task,
                ]

                env = os.environ.copy()
                env["ORCHESTRATOR_FAST_MODE"] = "1"

                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                ndjson_event(self, {"type": "status", "state": "thinking", "label": "thinking..."})

                process = subprocess.Popen(
                    command,
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

                with STATE.lock:
                    STATE.active_process = process
                    STATE.stop_requested = False

                streamed_action_keys: set[str] = set()

                assert process.stderr is not None
                stderr_lines: list[str] = []
                while True:
                    line = process.stderr.readline()
                    if not line:
                        if process.poll() is not None:
                            break
                        continue
                    stderr_lines.append(line)
                    stripped = line.strip()
                    if "[stream:planner]" in stripped:
                        text = stripped.replace("[stream:planner]", "").strip()
                        ndjson_event(self, {"type": "reasoning", "stage": "planner", "text": text})
                        ndjson_event(self, {"type": "status", "state": "thinking", "label": "thinking..."})
                    elif "[stream:reranker]" in stripped:
                        text = stripped.replace("[stream:reranker]", "").strip()
                        ndjson_event(self, {"type": "reasoning", "stage": "reranker", "text": text})
                        ndjson_event(self, {"type": "status", "state": "tools", "label": "getting tools..."})
                    elif "[status:agent]" in stripped or "[status:recovery]" in stripped:
                        ndjson_event(self, {"type": "status", "state": "working", "label": "working..."})
                    elif "[response:recovery]" in stripped:
                        text = _unwrap_response_payload(stripped.replace("[response:recovery]", "").strip())
                        if text:
                            envelopes = _extract_response_envelopes(text)
                            for reason_text in envelopes.get("reasons", []):
                                ndjson_event(self, {"type": "reasoning", "stage": "recovery", "text": str(reason_text)})
                        ndjson_event(self, {"type": "status", "state": "working", "label": "working..."})
                    elif "[response:agent]" in stripped:
                        text = _unwrap_response_payload(stripped.replace("[response:agent]", "").strip())
                        envelopes: dict[str, Any] = {"reasons": [], "tools": []}
                        if text:
                            envelopes = _extract_response_envelopes(text)
                            for reason_text in envelopes.get("reasons", []):
                                ndjson_event(self, {"type": "reasoning", "stage": "agent", "text": str(reason_text)})
                        ndjson_event(self, {"type": "status", "state": "working", "label": "working..."})
                        # Parse tool calls from complete typed response text
                        for tc_name, tc_args in envelopes.get("tools", []):
                            event_key = json.dumps(
                                {"tool": tc_name, "arguments": tc_args},
                                sort_keys=True,
                            )
                            if event_key not in streamed_action_keys:
                                streamed_action_keys.add(event_key)
                                ndjson_event(
                                    self,
                                    {
                                        "type": "action",
                                        "tool": tc_name,
                                        "arguments": tc_args,
                                        "live": True,
                                    },
                                )
                                if tc_name == "create_file" and isinstance(tc_args, dict):
                                    rel = str(tc_args.get("relative_path", "")).strip()
                                    if rel:
                                        ndjson_event(
                                            self,
                                            {
                                                "type": "action",
                                                "tool": "file_edit",
                                                "arguments": {"relative_path": rel},
                                                "live": True,
                                            },
                                        )

                assert process.stdout is not None
                stdout_raw = process.stdout.read().strip()
                process.wait(timeout=5)

                with STATE.lock:
                    stopped_by_user = STATE.stop_requested
                    STATE.active_process = None
                    STATE.stop_requested = False

                parsed_result: dict[str, Any] | None = None
                if stdout_raw:
                    try:
                        parsed_result = json.loads(stdout_raw)
                    except json.JSONDecodeError:
                        parsed_result = None

                if parsed_result is None:
                    if stopped_by_user:
                        ndjson_event(self, {"type": "stopped", "message": "Execution stopped by user."})
                        ndjson_event(self, {"type": "status", "state": "idle", "label": "stopped"})
                        ndjson_event(self, {"type": "done"})
                        return
                    ndjson_event(
                        self,
                        {
                            "type": "error",
                            "message": "Unable to parse orchestrator result",
                            "detail": stdout_raw[-1000:] if stdout_raw else "",
                        },
                    )
                    ndjson_event(self, {"type": "done"})
                    return

                result = parsed_result.get("orchestrator_result", {})
                status = str(result.get("status", ""))
                tool_trace = result.get("tool_trace", [])
                terminal_line_keys: set[str] = set()
                if isinstance(tool_trace, list):
                    for item in tool_trace:
                        if not isinstance(item, dict):
                            continue
                        tool_name = str(item.get("tool", ""))
                        arguments = item.get("arguments", {})
                        replay_key = json.dumps(
                            {
                                "tool": tool_name,
                                "arguments": arguments,
                            },
                            sort_keys=True,
                        )
                        if replay_key in streamed_action_keys:
                            continue
                        ndjson_event(
                            self,
                            {
                                "type": "action",
                                "tool": tool_name,
                                "arguments": arguments,
                            },
                        )

                        if tool_name in {"validate_web_app", "run_unit_tests"}:
                            result_payload = item.get("result", {}) if isinstance(item, dict) else {}
                            nested = result_payload.get("result") if isinstance(result_payload, dict) else None
                            if isinstance(nested, dict):
                                terminal_lines: list[str] = []
                                stdout_text = str(nested.get("stdout", "")).strip()
                                stderr_text = str(nested.get("stderr", "")).strip()
                                error_payload = nested.get("error")
                                error_message = ""
                                if isinstance(error_payload, dict):
                                    error_message = str(error_payload.get("message", "")).strip()
                                elif isinstance(error_payload, str):
                                    error_message = error_payload.strip()

                                if stdout_text:
                                    terminal_lines.append(stdout_text)
                                if stderr_text:
                                    terminal_lines.append(stderr_text)
                                if error_message:
                                    terminal_lines.append(error_message)

                                for block in terminal_lines:
                                    for line in block.splitlines():
                                        text = line.strip()
                                        if text:
                                            terminal_text = text if text.startswith("[terminal]") else f"[terminal] {text[:400]}"
                                            dedupe_key = f"{tool_name}:{terminal_text}"
                                            if dedupe_key in terminal_line_keys:
                                                continue
                                            terminal_line_keys.add(dedupe_key)
                                            ndjson_event(
                                                self,
                                                {
                                                    "type": "reasoning",
                                                    "stage": "terminal",
                                                    "text": terminal_text,
                                                },
                                            )

                        if tool_name == "create_file" and isinstance(arguments, dict):
                            rel = str(arguments.get("relative_path", "")).strip()
                            if rel:
                                ndjson_event(
                                    self,
                                    {
                                        "type": "action",
                                        "tool": "file_edit",
                                        "arguments": {"relative_path": rel},
                                    },
                                )

                final_message_raw = str(result.get("final_message", "")).strip()
                final_message = _extract_chat_text_for_ui(final_message_raw).strip()
                if not final_message:
                    final_message = "No final response returned."

                if status in {"stopped_no_progress", "stopped_by_agent"}:
                    ndjson_event(
                        self,
                        {
                            "type": "stopped",
                            "message": final_message,
                        },
                    )
                    ndjson_event(self, {"type": "status", "state": "idle", "label": "stopped"})

                words = final_message.split(" ")
                chunk = ""
                for word in words:
                    if chunk:
                        chunk = f"{chunk} {word}"
                    else:
                        chunk = word
                    ndjson_event(self, {"type": "chat_chunk", "text": chunk})

                with STATE.lock:
                    STATE.chat_history.append({"role": "assistant", "content": final_message})
                    STATE.active_process = None
                    STATE.stop_requested = False

                ndjson_event(self, {"type": "chat_final", "text": final_message})
                final_label = "stopped" if status in {"stopped_no_progress", "stopped_by_agent"} else "done"
                ndjson_event(self, {"type": "status", "state": "idle", "label": final_label})
                ndjson_event(self, {"type": "done"})
                return

            return json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

        except Exception as error:  # noqa: BLE001
            return json_response(
                self,
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error": {
                        "type": error.__class__.__name__,
                        "message": str(error),
                    },
                },
            )

    def _serve_static(self, file_name: str, content_type: str) -> None:
        target = (UI_DIR / file_name).resolve()
        if not target.exists() or not target.is_file() or target.parent != UI_DIR:
            return json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "File not found"})
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_workspace_file(self, relative_path: str) -> None:
        with STATE.lock:
            project = STATE.current_project

        if project is None:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "No open project"})

        cleaned = relative_path.strip().lstrip("/")
        target = (project / cleaned).resolve()
        try:
            target.relative_to(project)
        except ValueError:
            return json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Path escapes project"})

        if not target.exists() or not target.is_file():
            return json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "File not found"})

        mime_type, _ = mimetypes.guess_type(str(target))
        content_type = mime_type or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    host = os.environ.get("UI_HOST", "0.0.0.0")
    port = int(os.environ.get("UI_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), UiHandler)
    print(f"UI server running on http://{host}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
