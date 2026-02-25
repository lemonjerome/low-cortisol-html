from __future__ import annotations

import json
import os
import platform
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


def _default_workspaces_root() -> Path:
    env_value = os.environ.get("DEFAULT_WORKSPACES_ROOT", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (Path.home() / "Desktop" / "lch_workspaces").resolve()


@dataclass
class AppState:
    lock: Lock = field(default_factory=Lock)
    workspaces_root: Path = field(default_factory=_default_workspaces_root)
    current_project: Path | None = None
    project_structure_summary: str = ""
    chat_history: list[dict[str, str]] = field(default_factory=list)

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
    if not trimmed.startswith("lch_"):
        raise ValueError("Workspace name must start with 'lch_'")
    return trimmed


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


def choose_folder_macos() -> Path:
    script = 'POSIX path of (choose folder with prompt "Choose a workspace parent directory")'
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "Unable to open Finder picker"
        raise RuntimeError(detail)
    selected = result.stdout.strip()
    if not selected:
        raise RuntimeError("No folder selected")
    return validate_absolute_dir(selected)


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
            with STATE.lock:
                payload = {
                    "ok": True,
                    "workspaces_root": str(STATE.workspaces_root),
                    "current_project": str(STATE.current_project) if STATE.current_project else None,
                    "main_html": str(resolve_main_html(STATE.current_project)) if STATE.current_project else None,
                }
            return json_response(self, HTTPStatus.OK, payload)

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
                with STATE.lock:
                    STATE.workspaces_root = validated
                return json_response(self, HTTPStatus.OK, {"ok": True, "workspaces_root": str(validated)})

            if parsed.path == "/api/choose-folder":
                if platform.system().lower() != "darwin":
                    raise RuntimeError("Finder folder chooser is only available on macOS host")
                selected = choose_folder_macos()
                return json_response(self, HTTPStatus.OK, {"ok": True, "path": str(selected)})

            if parsed.path == "/api/create-project":
                payload = read_json(self)
                parent_dir = str(payload.get("parentDir", "")).strip()
                workspace_name = ensure_workspace_name(str(payload.get("workspaceName", "lch_new_project")))
                parent = validate_absolute_dir(parent_dir)
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
                if not name.startswith("lch_"):
                    raise ValueError("Only folders starting with 'lch_' can be opened")
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

                if platform.system().lower() == "darwin":
                    cmd = ["open", str(main_html)]
                elif platform.system().lower() == "windows":
                    cmd = ["cmd", "/c", "start", "", str(main_html)]
                else:
                    cmd = ["xdg-open", str(main_html)]
                subprocess.run(cmd, check=False)
                return json_response(self, HTTPStatus.OK, {"ok": True, "main_html": str(main_html)})

            if parsed.path == "/api/clear-chat":
                with STATE.lock:
                    STATE.clear_chat_memory()
                return json_response(self, HTTPStatus.OK, {"ok": True})

            if parsed.path == "/api/chat":
                payload = read_json(self)
                user_message = str(payload.get("message", "")).strip()
                if not user_message:
                    raise ValueError("Message is required")

                with STATE.lock:
                    if STATE.current_project is None:
                        raise ValueError("Open or create a project before chatting")
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
                env["ORCHESTRATOR_LOOP_CONTINUE"] = "no"

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
                    elif "[stream:agent-recovery]" in stripped:
                        text = stripped.replace("[stream:agent-recovery]", "").strip()
                        ndjson_event(self, {"type": "reasoning", "stage": "recovery", "text": text})
                        ndjson_event(self, {"type": "status", "state": "working", "label": "working..."})
                    elif "[stream:agent]" in stripped:
                        text = stripped.replace("[stream:agent]", "").strip()
                        ndjson_event(self, {"type": "reasoning", "stage": "agent", "text": text})
                        ndjson_event(self, {"type": "status", "state": "working", "label": "working..."})

                assert process.stdout is not None
                stdout_raw = process.stdout.read().strip()
                process.wait(timeout=5)

                parsed_result: dict[str, Any] | None = None
                if stdout_raw:
                    try:
                        parsed_result = json.loads(stdout_raw)
                    except json.JSONDecodeError:
                        parsed_result = None

                if parsed_result is None:
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
                tool_trace = result.get("tool_trace", [])
                if isinstance(tool_trace, list):
                    for item in tool_trace:
                        if not isinstance(item, dict):
                            continue
                        tool_name = str(item.get("tool", ""))
                        arguments = item.get("arguments", {})
                        ndjson_event(
                            self,
                            {
                                "type": "action",
                                "tool": tool_name,
                                "arguments": arguments,
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

                final_message = str(result.get("final_message", "")).strip()
                if not final_message:
                    final_message = "No final response returned."

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

                ndjson_event(self, {"type": "chat_final", "text": final_message})
                ndjson_event(self, {"type": "status", "state": "idle", "label": "done"})
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


def main() -> int:
    host = os.environ.get("UI_HOST", "0.0.0.0")
    port = int(os.environ.get("UI_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), UiHandler)
    print(f"UI server running on http://{host}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
