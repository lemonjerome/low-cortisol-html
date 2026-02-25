from __future__ import annotations

import json
import os
import sys
from typing import Any

from tool_registry import ToolDefinition, ToolRegistry
from tools.action_logger import log_tool_action
from tools.dummy_tools import sandbox_echo_path
from tools.file_tools import create_file_tool, list_directory_tool, read_file_tool
from tools.sandbox import resolve_workspace_root
from tools.web_tools import plan_web_build_tool, run_unit_tests_tool, scaffold_web_app_tool, validate_web_app_tool


def _build_registry(workspace_root: str) -> ToolRegistry:
    resolved_workspace = resolve_workspace_root(workspace_root)
    registry = ToolRegistry()

    def with_logging(tool_name: str, handler: Any) -> Any:
        def wrapped(arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                result = handler(arguments)
                log_tool_action(
                    workspace_root=resolved_workspace,
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                )
                return result
            except Exception as error:  # noqa: BLE001
                log_tool_action(
                    workspace_root=resolved_workspace,
                    tool_name=tool_name,
                    arguments=arguments,
                    result={
                        "ok": False,
                        "error": {
                            "type": error.__class__.__name__,
                            "message": str(error),
                        },
                    },
                )
                raise

        return wrapped

    registry.register(
        ToolDefinition(
            name="create_file",
            description="Create or overwrite a text file inside WORKSPACE_ROOT.",
            input_schema={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["relative_path", "content"],
                "additionalProperties": False,
            },
            handler=with_logging("create_file", lambda arguments: create_file_tool(arguments, resolved_workspace)),
        )
    )

    registry.register(
        ToolDefinition(
            name="read_file",
            description="Read a text file from WORKSPACE_ROOT with size limits.",
            input_schema={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                },
                "required": ["relative_path"],
                "additionalProperties": False,
            },
            handler=with_logging("read_file", lambda arguments: read_file_tool(arguments, resolved_workspace)),
        )
    )

    registry.register(
        ToolDefinition(
            name="list_directory",
            description="List files/directories in a workspace-relative directory.",
            input_schema={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "include_hidden": {"type": "boolean"},
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=with_logging(
                "list_directory",
                lambda arguments: list_directory_tool(arguments, resolved_workspace),
            ),
        )
    )

    registry.register(
        ToolDefinition(
            name="scaffold_web_app",
            description="Create a minimal HTML/CSS/JS app scaffold for a concept prototype.",
            input_schema={
                "type": "object",
                "properties": {
                    "app_dir": {"type": "string"},
                    "app_title": {"type": "string"},
                },
                "required": ["app_dir"],
                "additionalProperties": False,
            },
            handler=with_logging(
                "scaffold_web_app",
                lambda arguments: scaffold_web_app_tool(arguments, resolved_workspace),
            ),
        )
    )

    registry.register(
        ToolDefinition(
            name="validate_web_app",
            description="Validate required HTML/CSS/JS files and link references for local browser run.",
            input_schema={
                "type": "object",
                "properties": {
                    "app_dir": {"type": "string"},
                },
                "required": ["app_dir"],
                "additionalProperties": False,
            },
            handler=with_logging(
                "validate_web_app",
                lambda arguments: validate_web_app_tool(arguments, resolved_workspace),
            ),
        )
    )

    registry.register(
        ToolDefinition(
            name="run_unit_tests",
            description="Run plain JavaScript unit tests (Node.js) for the generated web concept.",
            input_schema={
                "type": "object",
                "properties": {
                    "test_file": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                },
                "required": ["test_file"],
                "additionalProperties": False,
            },
            handler=with_logging(
                "run_unit_tests",
                lambda arguments: run_unit_tests_tool(arguments, resolved_workspace),
            ),
        )
    )

    registry.register(
        ToolDefinition(
            name="plan_web_build",
            description="Generate a concrete phased development plan for the HTML/CSS/JS concept app.",
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "prompt_features": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
            handler=with_logging(
                "plan_web_build",
                lambda arguments: plan_web_build_tool(arguments, resolved_workspace),
            ),
        )
    )

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
            handler=with_logging(
                "dummy_sandbox_echo",
                lambda arguments: sandbox_echo_path(arguments, resolved_workspace),
            ),
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
