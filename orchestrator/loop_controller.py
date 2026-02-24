from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ollama_client import OllamaClient
from session_memory import SessionMemory


class LoopController:
    def __init__(
        self,
        *,
        project_root: Path,
        workspace_root: str,
        ollama_client: OllamaClient,
        model_name: str,
        tools: list[dict[str, Any]],
        max_loops: int,
    ) -> None:
        self.project_root = project_root
        self.workspace_root = workspace_root
        self.ollama_client = ollama_client
        self.model_name = model_name
        self.tools = tools
        self.max_loops = max_loops

    def run(self, task: str) -> dict[str, Any]:
        memory = SessionMemory()
        memory.add(
            "system",
            (
                "You are an autonomous coding agent. Use tools when needed, reason step-by-step, "
                "and return DONE when the objective is complete."
            ),
        )
        memory.add("user", task)

        tool_trace: list[dict[str, Any]] = []

        for iteration in range(1, self.max_loops + 1):
            response = self.ollama_client.chat(
                model=self.model_name,
                messages=memory.messages,
                tools=self.tools,
            )
            assistant_message = self.ollama_client.extract_assistant_message(response)
            content = str(assistant_message.get("content", ""))
            tool_calls = self.ollama_client.extract_tool_calls(assistant_message)

            memory.add("assistant", content, tool_calls=tool_calls)

            if not tool_calls:
                return {
                    "ok": True,
                    "status": "completed",
                    "iterations": iteration,
                    "final_message": content,
                    "tool_trace": tool_trace,
                }

            for call in tool_calls:
                tool_name = call["name"]
                arguments = call["arguments"]
                tool_result = self._call_mcp_tool(tool_name, arguments)
                tool_trace.append(
                    {
                        "iteration": iteration,
                        "tool": tool_name,
                        "arguments": arguments,
                        "result": tool_result,
                    }
                )
                memory.add("tool", json.dumps(tool_result), name=tool_name)

        return {
            "ok": False,
            "status": "max_loops_reached",
            "iterations": self.max_loops,
            "final_message": "Loop stopped before completion",
            "tool_trace": tool_trace,
        }

    def _call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request = {
            "action": "call_tool",
            "tool": tool_name,
            "arguments": arguments,
        }
        env = os.environ.copy()
        env["WORKSPACE_ROOT"] = self.workspace_root

        result = subprocess.run(
            [sys.executable, "mcp_server/server.py"],
            cwd=str(self.project_root),
            input=json.dumps(request),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        output = result.stdout.strip() or result.stderr.strip()
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = {
                "ok": False,
                "error": {
                    "type": "InvalidJSON",
                    "message": output,
                },
            }
        return parsed
