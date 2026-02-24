from __future__ import annotations

import json
import os
import sys
from typing import Any

from tool_registry import ToolDefinition, ToolRegistry
from tools.dummy_tools import sandbox_echo_path
from tools.sandbox import resolve_workspace_root


def _build_registry(workspace_root: str) -> ToolRegistry:
    resolved_workspace = resolve_workspace_root(workspace_root)
    registry = ToolRegistry()

    registry.register(
        ToolDefinition(
            name="dummy_sandbox_echo",
            description="Returns metadata for a workspace-relative path while enforcing sandbox boundaries.",
            input_schema={
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "Path relative to WORKSPACE_ROOT",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=lambda arguments: sandbox_echo_path(arguments, resolved_workspace),
        )
    )
    return registry


def _handle_request(registry: ToolRegistry, request: dict[str, Any]) -> dict[str, Any]:
    action = request.get("action")
    if action == "list_tools":
        return {
            "ok": True,
            "action": "list_tools",
            "result": registry.list_tools(),
        }

    if action == "call_tool":
        tool_name = request.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("'tool' must be a non-empty string for call_tool")
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("'arguments' must be an object")

        return {
            "ok": True,
            "action": "call_tool",
            "tool": tool_name,
            "result": registry.call_tool(tool_name, arguments),
        }

    raise ValueError("Unsupported action. Use 'list_tools' or 'call_tool'.")


def main() -> int:
    workspace_root = os.environ.get("WORKSPACE_ROOT", "")
    try:
        registry = _build_registry(workspace_root)
    except Exception as error:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": error.__class__.__name__,
                        "message": str(error),
                    },
                }
            )
        )
        return 1

    raw_input = sys.stdin.read().strip()
    if not raw_input:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "ValueError",
                        "message": "No JSON request provided on stdin",
                    },
                }
            )
        )
        return 1

    try:
        request = json.loads(raw_input)
        if not isinstance(request, dict):
            raise ValueError("Request must be a JSON object")

        response = _handle_request(registry, request)
        print(json.dumps(response))
        return 0
    except Exception as error:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": error.__class__.__name__,
                        "message": str(error),
                    },
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
