from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ollama_client import OllamaClient
from planner import Planner
from reranker import ToolReranker
from session_memory import SessionMemory
from tool_pruner import ToolPruner


class LoopController:
    def __init__(
        self,
        *,
        project_root: Path,
        workspace_root: str,
        ollama_client: OllamaClient,
        model_name: str,
        tools: list[dict[str, Any]],
        planner: Planner,
        reranker: ToolReranker,
        tool_pruner: ToolPruner,
        top_k_tools: int,
        candidate_pool_size: int,
        max_loops: int,
    ) -> None:
        self.project_root = project_root
        self.workspace_root = workspace_root
        self.ollama_client = ollama_client
        self.model_name = model_name
        self.tools = tools
        self.planner = planner
        self.reranker = reranker
        self.tool_pruner = tool_pruner
        self.top_k_tools = top_k_tools
        self.candidate_pool_size = candidate_pool_size
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
        selection_trace: list[dict[str, Any]] = []

        for iteration in range(1, self.max_loops + 1):
            plan = self.planner.plan_step(task=task, iteration=iteration, recent_messages=memory.messages)
            retrieval = self.tool_pruner.retrieve_candidates(
                query=plan.get("retrieval_query", task),
                tools=self.tools,
                top_n=self.candidate_pool_size,
            )
            reranked = self.reranker.rerank(
                task=task,
                plan=plan,
                candidates=retrieval["candidates"],
                top_k=self.top_k_tools,
            )
            selected_items = reranked["selected"]
            selected_tools = [item["tool"] for item in selected_items]
            if not selected_tools:
                selected_tools = self.tools[: max(1, min(self.top_k_tools, len(self.tools)))]

            selection_event = {
                "iteration": iteration,
                "plan": plan,
                "retrieval": retrieval["report"],
                "rerank": reranked["report"],
                "selected_tools": [
                    str(item.get("function", {}).get("name", ""))
                    for item in selected_tools
                    if isinstance(item, dict)
                ],
            }
            selection_trace.append(selection_event)
            self.tool_pruner.log_event(stage="selection", payload=selection_event)

            response = self.ollama_client.chat(
                model=self.model_name,
                messages=memory.messages,
                tools=selected_tools,
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
                    "selection_trace": selection_trace,
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
            "selection_trace": selection_trace,
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
