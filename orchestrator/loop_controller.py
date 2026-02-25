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


TOOL_NAME_ALIASES: dict[str, str] = {
    "open_file": "read_file",
    "view_file": "read_file",
    "check_code": "read_file",
    "edit_file": "create_file",
    "write_file": "create_file",
    "save_file": "create_file",
    "list_files": "list_directory",
}


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
                "You are an autonomous coding agent running a Reason→Create→Debug loop. "
                "Use tools when needed, reason step-by-step, and keep iterating until the task is truly complete. "
                "Do not only describe intended actions; call tools directly whenever work is required. "
                "When finished, return a final message that starts with 'DONE:'."
            ),
        )
        memory.add("user", task)

        tool_trace: list[dict[str, Any]] = []
        selection_trace: list[dict[str, Any]] = []
        repair_events: list[dict[str, Any]] = []
        development_phases: list[str] = []
        extensions_used = 0
        max_allowed_loops = self.max_loops
        iteration = 0

        while True:
            if iteration >= max_allowed_loops:
                if self._should_extend_loops(max_allowed_loops=max_allowed_loops):
                    max_allowed_loops += 5
                    extensions_used += 1
                    continue
                break

            iteration += 1
            plan = self.planner.plan_step(task=task, iteration=iteration, recent_messages=memory.messages)
            retrieval_query = str(plan.get("retrieval_query", "")).strip() or task
            if not development_phases:
                candidate_phases = plan.get("development_phases", [])
                if isinstance(candidate_phases, list):
                    development_phases = [str(item) for item in candidate_phases if str(item).strip()]

            active_phase = self._select_active_phase(iteration=iteration, plan=plan, development_phases=development_phases)
            memory.add(
                "user",
                (
                    f"Current build phase: {active_phase}. "
                    "Perform a small, concrete step for this phase. Prefer exactly one tool call this turn."
                ),
            )

            retrieval = self.tool_pruner.retrieve_candidates(
                query=retrieval_query,
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
                stream=True,
                stream_label="agent",
            )
            assistant_message = self.ollama_client.extract_assistant_message(response)
            content = str(assistant_message.get("content", ""))
            tool_calls = self.ollama_client.extract_tool_calls(assistant_message)

            memory.add("assistant", content, tool_calls=tool_calls)

            if not tool_calls and not self._is_done_message(content):
                recovery_content, recovered_calls = self._recover_tool_calls(memory=memory, selected_tools=selected_tools)
                if recovery_content:
                    content = recovery_content
                if recovered_calls:
                    tool_calls = recovered_calls
                    memory.add("assistant", content, tool_calls=tool_calls)

            if not tool_calls:
                if self._is_done_message(content):
                    return {
                        "ok": True,
                        "status": "completed",
                        "iterations": iteration,
                        "final_message": content,
                        "tool_trace": tool_trace,
                        "selection_trace": selection_trace,
                        "repair_trace": repair_events,
                    }

                memory.add(
                    "user",
                    (
                        "Task is not yet marked complete. Continue the Reason→Create→Debug loop, "
                        "use tools as needed, and only finish with a message that starts with 'DONE:'."
                    ),
                )
                continue

            iteration_had_tool_error = False
            iteration_feedback: list[dict[str, Any]] = []
            iteration_tool_names: list[str] = []

            for index, call in enumerate(tool_calls):
                if index > 0:
                    break
                tool_name, arguments = self._normalize_tool_call(call)
                tool_result = self._call_mcp_tool(tool_name, arguments)
                iteration_tool_names.append(tool_name)
                tool_trace.append(
                    {
                        "iteration": iteration,
                        "tool": tool_name,
                        "arguments": arguments,
                        "result": tool_result,
                    }
                )
                memory.add("tool", json.dumps(tool_result), name=tool_name)

                tool_feedback = self._extract_tool_feedback(tool_name=tool_name, tool_result=tool_result)
                if tool_feedback is not None:
                    iteration_had_tool_error = True
                    iteration_feedback.append(tool_feedback)

            if iteration_feedback:
                feedback_text = self._build_tool_feedback_prompt(iteration_feedback)
                memory.add("user", feedback_text)
                repair_events.append(
                    {
                        "iteration": iteration,
                        "tool_names": iteration_tool_names,
                        "tool_feedback": iteration_feedback,
                    }
                )

            if not iteration_had_tool_error:
                memory.add(
                    "user",
                    "Proceed to the next Reason→Create→Debug step. If complete, respond with 'DONE:' and summary.",
                )

        return {
            "ok": False,
            "status": "max_loops_reached",
            "iterations": iteration,
            "max_allowed_loops": max_allowed_loops,
            "extensions_used": extensions_used,
            "final_message": "Loop stopped before completion",
            "tool_trace": tool_trace,
            "selection_trace": selection_trace,
            "repair_trace": repair_events,
        }

    def _should_extend_loops(self, *, max_allowed_loops: int) -> bool:
        override = os.environ.get("ORCHESTRATOR_LOOP_CONTINUE", "").strip().lower()
        if override in {"1", "true", "yes", "y"}:
            return True
        if override in {"0", "false", "no", "n"}:
            return False

        if not sys.stdin.isatty():
            return False

        prompt = f"Reached max loops ({max_allowed_loops}). Continue with +5 more loops? [y/N]: "
        try:
            response = input(prompt).strip().lower()
        except EOFError:
            return False
        return response in {"y", "yes"}

    def _is_done_message(self, content: str) -> bool:
        normalized = content.strip().upper()
        return normalized.startswith("DONE:")

    def _extract_tool_feedback(self, *, tool_name: str, tool_result: dict[str, Any]) -> dict[str, Any] | None:
        if tool_name not in {
            "create_file",
            "read_file",
            "list_directory",
            "scaffold_web_app",
            "validate_web_app",
            "run_unit_tests",
            "plan_web_build",
        }:
            return None

        nested = tool_result.get("result") if isinstance(tool_result, dict) else None
        if not isinstance(nested, dict):
            return {
                "tool": tool_name,
                "error": "Invalid tool response format",
                "stdout": "",
                "stderr": json.dumps(tool_result),
            }

        ok = bool(nested.get("ok", False))
        stderr = str(nested.get("stderr", ""))
        stdout = str(nested.get("stdout", ""))
        if ok:
            return None

        error_payload = nested.get("error")
        error_message = ""
        if isinstance(error_payload, dict):
            error_message = str(error_payload.get("message", ""))
        elif isinstance(error_payload, str):
            error_message = error_payload

        return {
            "tool": tool_name,
            "error": error_message,
            "stdout": stdout,
            "stderr": stderr,
        }

    def _build_tool_feedback_prompt(self, feedback_items: list[dict[str, Any]]) -> str:
        lines = [
            "Tool diagnostics were returned. Repair the implementation before continuing:",
        ]
        for item in feedback_items:
            tool = str(item.get("tool", "unknown"))
            error = str(item.get("error", "")).strip()
            stdout = str(item.get("stdout", "")).strip()
            stderr = str(item.get("stderr", "")).strip()

            lines.append(f"- tool: {tool}")
            if error:
                lines.append(f"  error: {error}")
            if stderr:
                lines.append(f"  stderr: {stderr[:3000]}")
            if stdout:
                lines.append(f"  stdout: {stdout[:1000]}")

        lines.append("Apply a fix, run the appropriate tools again, and only finish with 'DONE:' when verified.")
        return "\n".join(lines)

    def _select_active_phase(self, *, iteration: int, plan: dict[str, Any], development_phases: list[str]) -> str:
        active_phase = str(plan.get("active_phase", "")).strip()
        if active_phase:
            return active_phase

        if development_phases:
            index = min(max(iteration - 1, 0), len(development_phases) - 1)
            return development_phases[index]

        return f"Iteration {iteration}"

    def _normalize_tool_call(self, call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        tool_name = str(call.get("name", "")).strip()
        arguments = call.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}

        canonical = TOOL_NAME_ALIASES.get(tool_name, tool_name)
        if canonical == tool_name:
            lowered = canonical.lower()
            if "edit" in lowered or "write" in lowered or "save" in lowered:
                canonical = "create_file"
            elif "read" in lowered or "open" in lowered or "view" in lowered:
                canonical = "read_file"
            elif "list" in lowered or lowered == "ls":
                canonical = "list_directory"
            elif "test" in lowered:
                canonical = "run_unit_tests"
            elif "valid" in lowered or "check" in lowered:
                canonical = "validate_web_app"
            elif "scaffold" in lowered or "bootstrap" in lowered:
                canonical = "scaffold_web_app"
            elif "plan" in lowered:
                canonical = "plan_web_build"

        if canonical == "create_file":
            if "file_path" in arguments and "relative_path" not in arguments:
                arguments["relative_path"] = arguments["file_path"]
            arguments.setdefault("overwrite", True)
        if canonical == "read_file" and "file_path" in arguments and "relative_path" not in arguments:
            arguments["relative_path"] = arguments["file_path"]
        return canonical, arguments

    def _recover_tool_calls(
        self,
        *,
        memory: SessionMemory,
        selected_tools: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        available_names = [
            str(item.get("function", {}).get("name", ""))
            for item in selected_tools
            if isinstance(item, dict)
        ]
        available_text = ", ".join(name for name in available_names if name)
        recovery_prompt = (
            "You returned analysis without a tool call. "
            "If more work is needed, respond with exactly one tool call using available tools now. "
            f"Available tools this turn: {available_text}. "
            "If and only if everything is already complete, respond with 'DONE:' and a short summary."
        )

        recovery_messages = [*memory.messages, {"role": "user", "content": recovery_prompt}]
        response = self.ollama_client.chat(
            model=self.model_name,
            messages=recovery_messages,
            tools=selected_tools,
            stream=True,
            stream_label="agent-recovery",
        )
        assistant_message = self.ollama_client.extract_assistant_message(response)
        content = str(assistant_message.get("content", ""))
        tool_calls = self.ollama_client.extract_tool_calls(assistant_message)
        return content, tool_calls

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
