from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ollama_client import OllamaClient
from planner import Planner
from project_memory import ProjectMemory
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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


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
        self.workspace_root_path = Path(workspace_root).expanduser().resolve()

    def run(self, task: str) -> dict[str, Any]:
        memory = SessionMemory()
        memory.add(
            "system",
            (
                "You are an autonomous coding agent running a Reason→Create→Debug loop. "
                "Use tools when needed, reason step-by-step, and keep iterating until the task is truly complete. "
                "Do not only describe intended actions; call tools directly whenever work is required. "
                "Implement requested features in substantial phase-sized batches rather than tiny edits. "
                "Do not stop after basic scaffolding or placeholder output. "
                "Only return DONE after requested functionality is fully implemented and verified with validation and real tests. "
                "When finished, return a final message that starts with 'DONE:'. "
                "Never claim completion using plain prose only; completion must use the explicit 'DONE:' prefix. "
                "If execution is blocked and cannot continue, use explicit 'STOP:' with a short reason. "
                "All assistant responses must be strict JSON objects or arrays. "
                "Use typed envelopes: "
                "{\"type\":\"reason\",\"text\":\"...\"} for internal planning/reasoning, "
                "{\"type\":\"tool\",\"name\":\"tool_name\",\"arguments\":{...}} for tool calls, "
                "and {\"type\":\"chat\",\"text\":\"...\"} for user-facing chat. "
                "Keep type=reason conversational and concise; never dump raw tool JSON in reason text. "
                "Prefer structured incremental edit tools (replace_range, insert_after_marker, append_to_file) over full-file rewrites. "
                "Only use create_file full rewrite when creating a new file or when explicitly required for full replacement."
            ),
        )
        memory.add("user", task)

        events_log_path = self.workspace_root_path / ".low-cortisol-html-logs" / "orchestrator_events.log"
        project_memory = ProjectMemory(
            workspace_root=self.workspace_root_path,
            ollama_client=self.ollama_client,
            embedding_model=os.environ.get("EMBEDDING_MODEL", "nomic-embed-text"),
            events_log_path=events_log_path,
        )

        tool_trace: list[dict[str, Any]] = []
        selection_trace: list[dict[str, Any]] = []
        repair_events: list[dict[str, Any]] = []
        development_phases: list[str] = []
        iteration = 0
        max_tool_calls_per_iteration = int(os.environ.get("ORCHESTRATOR_MAX_TOOL_CALLS_PER_ITERATION", "8"))
        phase_plan_ready = False
        changed_since_validation: set[str] = set()
        changed_since_tests: set[str] = set()
        validation_runs = 0
        tests_runs = 0
        last_validation_ok = False
        validation_ever_passed = False
        consecutive_validation_deferrals = 0
        consecutive_test_deferrals = 0
        total_validation_deferrals = 0
        total_test_deferrals = 0
        max_total_deferrals = 4
        no_progress_iterations = 0
        max_no_progress_iterations = int(os.environ.get("ORCHESTRATOR_MAX_NO_PROGRESS_ITERATIONS", "6"))
        recent_tool_call_signatures: list[str] = []
        max_repeated_signatures = 3
        planning_entries: list[str] = []
        file_generation: dict[str, int] = {}
        file_read_generation: dict[str, int] = {}
        last_create_file_signature: dict[str, str] = {}
        last_structured_edit_signature: dict[str, str] = {}
        plan_loop_iterations = 0
        new_project_hint_sent = False
        started_from_empty = self._is_workspace_empty()
        substantive_edit_count = 0

        while True:
            iteration += 1
            project_memory.refresh()
            workspace_is_empty = self._is_workspace_empty()
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
                    "Implement this phase in a large coherent coding batch. "
                    f"You may use up to {max_tool_calls_per_iteration} tool calls this turn when needed. "
                    "Do not run validate_web_app or run_unit_tests after every file edit; run them after a major phase checkpoint. "
                    "Before major reasoning/planning decisions in this phase, inspect project state by calling list_directory on '.' and then read_file on relevant files you choose from that listing. "
                    "When reasoning/planning, explain decisions in type=reason envelopes."
                ),
            )
            if workspace_is_empty and not new_project_hint_sent:
                memory.add(
                    "user",
                    (
                        "Workspace state: NEW_PROJECT_EMPTY. Treat this as a brand-new project. "
                        "Start by creating a coherent initial app structure (index.html, styles.css, app.js, tests.js) "
                        "or use scaffold_web_app, then continue with phased implementation."
                    ),
                )
                new_project_hint_sent = True

            file_top_k = _env_int("ORCHESTRATOR_FILE_TOP_K", 6)
            retrieved_files = project_memory.retrieve(query=retrieval_query, top_k=file_top_k)
            retrieval_context = project_memory.build_retrieval_context(
                retrieved=retrieved_files,
                include_full_top_n=_env_int("ORCHESTRATOR_FILE_FULL_TOP_N", 2),
                max_full_chars=_env_int("ORCHESTRATOR_FILE_FULL_MAX_CHARS", 12000),
            )
            memory.add(
                "user",
                "Project memory retrieval context (file-level):\n"
                + retrieval_context,
            )
            project_memory.write_event(
                stage="file_retrieval",
                payload={
                    "iteration": iteration,
                    "query": retrieval_query,
                    "top_k": file_top_k,
                    "retrieved": [
                        {
                            "relative_path": str(item.get("relative_path", "")),
                            "score": float(item.get("score", 0.0)),
                        }
                        for item in retrieved_files
                    ],
                },
            )
            if planning_entries:
                recent_plan = planning_entries[-6:]
                memory.add(
                    "user",
                    "Planning memory context (most recent):\n"
                    + "\n".join(f"- {entry}" for entry in recent_plan),
                )
            if not phase_plan_ready:
                memory.add(
                    "user",
                    "Planner mode first: produce concrete multi-phase reasoning (type=reason) and decide files to inspect. "
                    "Do not edit files before you inspect structure and relevant files. Prefer plan_web_build only after initial reasoning.",
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

            if not phase_plan_ready:
                prioritized: list[dict[str, Any]] = []
                planning_tool_names = {"plan_web_build", "list_directory", "read_file"}
                if workspace_is_empty:
                    planning_tool_names.update({"scaffold_web_app", "create_file"})
                prioritized.extend(
                    [
                        tool
                        for tool in selected_tools
                        if str(tool.get("function", {}).get("name", "")) in planning_tool_names
                    ]
                )
                prioritized.extend(
                    [
                        tool
                        for tool in self.tools
                        if str(tool.get("function", {}).get("name", "")) in planning_tool_names
                    ]
                )

                deduped: list[dict[str, Any]] = []
                seen_names: set[str] = set()
                for tool in prioritized:
                    name = str(tool.get("function", {}).get("name", ""))
                    if not name or name in seen_names:
                        continue
                    seen_names.add(name)
                    deduped.append(tool)

                if deduped:
                    selected_tools = deduped[: max(1, min(self.top_k_tools, len(deduped)))]

                if plan_loop_iterations >= 2:
                    forced_names = ["plan_web_build", "read_file", "list_directory"]
                    if workspace_is_empty:
                        forced_names = ["plan_web_build", "scaffold_web_app", "create_file", "list_directory"]
                    forced_tools: list[dict[str, Any]] = []
                    for forced_name in forced_names:
                        for tool in self.tools:
                            name = str(tool.get("function", {}).get("name", ""))
                            if name == forced_name:
                                forced_tools.append(tool)
                                break
                    for tool in selected_tools:
                        name = str(tool.get("function", {}).get("name", ""))
                        if name not in {"plan_web_build", "read_file", "list_directory"}:
                            forced_tools.append(tool)
                    if forced_tools:
                        selected_tools = forced_tools[: max(1, min(self.top_k_tools, len(forced_tools)))]
                    memory.add(
                        "user",
                        "Planning appears stalled. Now force progression: call plan_web_build and at least one read_file for a relevant file, "
                        "then proceed with implementation planning in type=reason.",
                    )

            selected_tools = self._ensure_required_tools(
                selected_tools=selected_tools,
                phase_plan_ready=phase_plan_ready,
                workspace_is_empty=workspace_is_empty,
            )

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

            print("[status:agent] calling model...", file=sys.stderr, flush=True)
            response = self.ollama_client.chat(
                model=self.model_name,
                messages=memory.messages,
                tools=selected_tools,
                stream=False,
                num_ctx=_env_int("ORCHESTRATOR_AGENT_NUM_CTX", 32768),
                num_predict=_env_int("ORCHESTRATOR_AGENT_NUM_PREDICT", 8192),
            )
            assistant_message = self.ollama_client.extract_assistant_message(response)
            content = str(assistant_message.get("content", ""))
            tool_calls = self.ollama_client.extract_tool_calls(assistant_message)

            # Emit complete response as single block for UI reasoning display
            if content.strip():
                print(
                    f"[response:agent] {json.dumps({'content': content}, ensure_ascii=False)}",
                    file=sys.stderr,
                    flush=True,
                )

            reason_entries = self._extract_reason_entries(content)
            if reason_entries:
                planning_entries.extend(reason_entries)
            elif tool_calls:
                synthetic = self._build_tool_only_reasoning(tool_calls)
                if synthetic:
                    planning_entries.append(synthetic)
                    print(
                        f"[response:agent] {json.dumps({'content': json.dumps({'type': 'reason', 'text': synthetic}, ensure_ascii=False)}, ensure_ascii=False)}",
                        file=sys.stderr,
                        flush=True,
                    )

            # Deduplicate tool calls extracted from content (model often repeats)
            tool_calls = self._deduplicate_tool_calls(tool_calls)

            memory.add("assistant", content, tool_calls=tool_calls)

            if not tool_calls and not self._is_done_message(content):
                recovery_content, recovered_calls = self._recover_tool_calls(memory=memory, selected_tools=selected_tools)
                if recovery_content:
                    content = recovery_content
                    recovery_reason_entries = self._extract_reason_entries(recovery_content)
                    if recovery_reason_entries:
                        planning_entries.extend(recovery_reason_entries)
                if recovered_calls:
                    tool_calls = recovered_calls
                    memory.add("assistant", content, tool_calls=tool_calls)

            project_memory.write_event(
                stage="agent_response",
                payload={
                    "iteration": iteration,
                    "tool_calls": [str(item.get("name", "")) for item in tool_calls if isinstance(item, dict)],
                    "content_preview": content[:800],
                },
            )

            if not tool_calls:
                if self._is_stop_message(content):
                    return {
                        "ok": False,
                        "status": "stopped_by_agent",
                        "iterations": iteration,
                        "final_message": content,
                        "tool_trace": tool_trace,
                        "selection_trace": selection_trace,
                        "repair_trace": repair_events,
                    }
                lowered_content = content.lower()
                if "manual" in lowered_content and "verify" in lowered_content:
                    memory.add(
                        "user",
                        (
                            "Do not ask for manual verification while the task is incomplete. "
                            "Use tools to create/fix files and validate within the workspace. "
                            "Only use STOP if tooling/environment is persistently blocking progress."
                        ),
                    )
                    continue

                if self._claims_success_without_evidence(
                    content=content,
                    last_validation_ok=last_validation_ok,
                    tests_runs=tests_runs,
                ):
                    memory.add(
                        "user",
                        (
                            "Your message claims validation/tests passed but the tool trace does not yet prove completion. "
                            "Run validate_web_app and run_unit_tests (non-deferred), then continue implementation if needed. "
                            "Only claim completion after evidence and respond with DONE:."
                        ),
                    )
                    continue

                if self._is_done_message(content):
                    completion_gaps = self._completion_gaps(task=task, tool_trace=tool_trace, iteration=iteration)
                    if completion_gaps:
                        memory.add(
                            "user",
                            "Do not finish yet. Remaining completion requirements:\n"
                            + "\n".join(f"- {item}" for item in completion_gaps)
                            + "\nContinue building step-by-step and return DONE only after all are satisfied.",
                        )
                        continue
                    return {
                        "ok": True,
                        "status": "completed",
                        "iterations": iteration,
                        "final_message": content,
                        "tool_trace": tool_trace,
                        "selection_trace": selection_trace,
                        "repair_trace": repair_events,
                    }

                if self._looks_like_completion_without_done(content):
                    completion_gaps = self._completion_gaps(task=task, tool_trace=tool_trace, iteration=iteration)
                    if not completion_gaps:
                        normalized_done = (
                            f"DONE: {content.strip()}"
                            if content.strip()
                            else "DONE: Task completed and verified."
                        )
                        return {
                            "ok": True,
                            "status": "completed",
                            "iterations": iteration,
                            "final_message": normalized_done,
                            "tool_trace": tool_trace,
                            "selection_trace": selection_trace,
                            "repair_trace": repair_events,
                        }
                    memory.add(
                        "user",
                        "You indicated completion but required checks are still missing. "
                        "Do not claim completion until all gaps are resolved, then respond with exact 'DONE:'.",
                    )
                    continue

                memory.add(
                    "user",
                    (
                        "Task is not yet marked complete. Continue the Reason→Create→Debug loop, "
                        "use tools as needed, and only finish with a message that starts with 'DONE:'."
                    ),
                )
                continue

            if not phase_plan_ready:
                has_plan_call = any(
                    str(call.get("name", "")) == "plan_web_build"
                    for call in tool_calls
                    if isinstance(call, dict)
                )
                has_read_call = any(
                    str(call.get("name", "")) == "read_file"
                    for call in tool_calls
                    if isinstance(call, dict)
                )
                if has_plan_call and has_read_call:
                    phase_plan_ready = True
                    plan_loop_iterations = 0
                else:
                    plan_loop_iterations += 1
            else:
                plan_loop_iterations = 0

            iteration_had_tool_error = False
            iteration_had_non_edit_progress = False
            iteration_feedback: list[dict[str, Any]] = []
            iteration_tool_names: list[str] = []
            iteration_changed_files = 0
            edited_files_this_iteration: set[str] = set()
            max_files_per_iteration = _env_int("ORCHESTRATOR_MAX_FILES_PER_ITERATION", 4)

            for index, call in enumerate(tool_calls):
                if index >= max_tool_calls_per_iteration:
                    break
                tool_name, arguments = self._normalize_tool_call(call)

                if tool_name in {"create_file", "append_to_file", "insert_after_marker", "replace_range"}:
                    candidate_path = str(arguments.get("relative_path", "")).strip()
                    if (
                        candidate_path
                        and candidate_path not in edited_files_this_iteration
                        and len(edited_files_this_iteration) >= max_files_per_iteration
                    ):
                        tool_result = self._deferred_tool_result(
                            tool_name=tool_name,
                            reason=(
                                "Deferred edit to keep iteration focused. "
                                f"Max edited files per iteration is {max_files_per_iteration}."
                            ),
                        )
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
                        continue

                if tool_name == "validate_web_app":
                    if started_from_empty and substantive_edit_count == 0:
                        tool_result = self._deferred_tool_result(
                            tool_name="validate_web_app",
                            reason=(
                                "Project started empty and only scaffold/discovery has run so far. "
                                "Implement real UI/JS edits first, then validate."
                            ),
                        )
                        consecutive_validation_deferrals += 1
                        total_validation_deferrals += 1
                    else:
                        should_run_validation = (
                            self._should_run_validation(
                                changed_since_validation=changed_since_validation,
                                validation_runs=validation_runs,
                            )
                            or consecutive_validation_deferrals >= 2
                            or total_validation_deferrals >= max_total_deferrals
                        )
                        if not should_run_validation:
                            tool_result = self._deferred_tool_result(
                                tool_name="validate_web_app",
                                reason="Defer validation until after a major phase checkpoint with multiple edits.",
                            )
                            consecutive_validation_deferrals += 1
                            total_validation_deferrals += 1
                        else:
                            tool_result = self._call_mcp_tool(tool_name, arguments)
                            validation_runs += 1
                            consecutive_validation_deferrals = 0
                            nested = tool_result.get("result") if isinstance(tool_result, dict) else None
                            last_validation_ok = bool(isinstance(nested, dict) and nested.get("ok", False))
                            if last_validation_ok:
                                validation_ever_passed = True
                                changed_since_validation.clear()
                elif tool_name == "run_unit_tests":
                    if started_from_empty and substantive_edit_count == 0:
                        tool_result = self._deferred_tool_result(
                            tool_name="run_unit_tests",
                            reason=(
                                "Project started empty and no substantive implementation edits are present yet. "
                                "Implement app behavior and tests first, then run unit tests."
                            ),
                        )
                        consecutive_test_deferrals += 1
                        total_test_deferrals += 1
                    else:
                        should_run_tests = (
                            self._should_run_tests(
                                changed_since_tests=changed_since_tests,
                                tests_runs=tests_runs,
                                last_validation_ok=last_validation_ok,
                                validation_ever_passed=validation_ever_passed,
                            )
                            or consecutive_test_deferrals >= 2
                            or total_test_deferrals >= max_total_deferrals
                        )
                        if not should_run_tests:
                            tool_result = self._deferred_tool_result(
                                tool_name="run_unit_tests",
                                reason="Defer tests until validation passes and major implementation changes are complete.",
                            )
                            consecutive_test_deferrals += 1
                            total_test_deferrals += 1
                        else:
                            tool_result = self._call_mcp_tool(tool_name, arguments)
                            tests_runs += 1
                            consecutive_test_deferrals = 0
                            changed_since_tests.clear()
                elif tool_name == "create_file":
                    relative_path = str(arguments.get("relative_path", "")).strip()
                    content_value = str(arguments.get("content", ""))
                    signature = self._text_signature(content_value)
                    existing_file = self._resolve_workspace_file(relative_path)
                    previous_content = self._read_local_file(existing_file)

                    if relative_path and last_create_file_signature.get(relative_path) == signature:
                        tool_result = self._deferred_tool_result(
                            tool_name="create_file",
                            reason=(
                                f"Skipped redundant rewrite for '{relative_path}' because content is identical "
                                "to the previous write."
                            ),
                        )
                    else:
                        needs_sync_read = False
                        if existing_file is not None and existing_file.exists() and existing_file.is_file():
                            generation = file_generation.get(relative_path, 0)
                            read_generation = file_read_generation.get(relative_path, -1)
                            needs_sync_read = read_generation < generation or read_generation < 0

                        if needs_sync_read and relative_path:
                            sync_read_args = {"relative_path": relative_path, "max_bytes": 200000}
                            sync_read_result = self._call_mcp_tool("read_file", sync_read_args)
                            project_memory.mark_touched(relative_path)
                            tool_trace.append(
                                {
                                    "iteration": iteration,
                                    "tool": "read_file",
                                    "arguments": sync_read_args,
                                    "result": sync_read_result,
                                }
                            )
                            memory.add("tool", json.dumps(sync_read_result), name="read_file")

                            nested_sync = sync_read_result.get("result") if isinstance(sync_read_result, dict) else None
                            sync_content = ""
                            if isinstance(nested_sync, dict) and bool(nested_sync.get("ok", False)):
                                sync_content = str(nested_sync.get("content", ""))
                                file_read_generation[relative_path] = file_generation.get(relative_path, 0)

                            planning_context = "\n".join(f"- {entry}" for entry in planning_entries[-8:]) or "- (none)"
                            memory.add(
                                "user",
                                self._build_edit_sync_prompt(
                                    relative_path=relative_path,
                                    planning_context=planning_context,
                                    full_file_content=sync_content,
                                ),
                            )
                            tool_result = self._deferred_tool_result(
                                tool_name="create_file",
                                reason=(
                                    f"Deferred edit for '{relative_path}' until full file context and planning notes were injected. "
                                    "Re-issue create_file with synchronized full-file update."
                                ),
                            )
                        else:
                            tool_result = self._call_mcp_tool(tool_name, arguments)
                            nested_write = tool_result.get("result") if isinstance(tool_result, dict) else None
                            if isinstance(nested_write, dict) and bool(nested_write.get("ok", False)) and relative_path:
                                file_generation[relative_path] = file_generation.get(relative_path, 0) + 1
                                last_create_file_signature[relative_path] = signature
                                project_memory.mark_touched(relative_path)
                                reflection = self._build_diff_reflection_prompt(
                                    relative_path=relative_path,
                                    before=previous_content,
                                    after=content_value,
                                )
                                if reflection:
                                    memory.add("user", reflection)
                elif tool_name in {"append_to_file", "insert_after_marker", "replace_range"}:
                    relative_path = str(arguments.get("relative_path", "")).strip()
                    structured_signature = self._text_signature(
                        json.dumps({"tool": tool_name, "arguments": arguments}, sort_keys=True, ensure_ascii=False)
                    )

                    if relative_path and last_structured_edit_signature.get(relative_path) == structured_signature:
                        tool_result = self._deferred_tool_result(
                            tool_name=tool_name,
                            reason=(
                                f"Skipped redundant structured edit for '{relative_path}' because arguments are identical "
                                "to the previous successful structured edit."
                            ),
                        )
                    else:
                        existing_file = self._resolve_workspace_file(relative_path)
                        needs_sync_read = False
                        if existing_file is not None and existing_file.exists() and existing_file.is_file():
                            generation = file_generation.get(relative_path, 0)
                            read_generation = file_read_generation.get(relative_path, -1)
                            needs_sync_read = read_generation < generation or read_generation < 0

                        if needs_sync_read and relative_path:
                            sync_read_args = {"relative_path": relative_path, "max_bytes": 200000}
                            sync_read_result = self._call_mcp_tool("read_file", sync_read_args)
                            project_memory.mark_touched(relative_path)
                            tool_trace.append(
                                {
                                    "iteration": iteration,
                                    "tool": "read_file",
                                    "arguments": sync_read_args,
                                    "result": sync_read_result,
                                }
                            )
                            memory.add("tool", json.dumps(sync_read_result), name="read_file")

                            nested_sync = sync_read_result.get("result") if isinstance(sync_read_result, dict) else None
                            sync_content = ""
                            if isinstance(nested_sync, dict) and bool(nested_sync.get("ok", False)):
                                sync_content = str(nested_sync.get("content", ""))
                                file_read_generation[relative_path] = file_generation.get(relative_path, 0)

                            planning_context = "\n".join(f"- {entry}" for entry in planning_entries[-8:]) or "- (none)"
                            memory.add(
                                "user",
                                self._build_structured_edit_sync_prompt(
                                    tool_name=tool_name,
                                    relative_path=relative_path,
                                    planning_context=planning_context,
                                    full_file_content=sync_content,
                                ),
                            )
                            tool_result = self._deferred_tool_result(
                                tool_name=tool_name,
                                reason=(
                                    f"Deferred structured edit for '{relative_path}' until full file context was refreshed. "
                                    "Re-issue with corrected line ranges/markers based on current file contents."
                                ),
                            )
                        else:
                            tool_result = self._call_mcp_tool(tool_name, arguments)
                            nested_edit = tool_result.get("result") if isinstance(tool_result, dict) else None
                            if isinstance(nested_edit, dict) and bool(nested_edit.get("ok", False)) and relative_path:
                                file_generation[relative_path] = file_generation.get(relative_path, 0) + 1
                                last_structured_edit_signature[relative_path] = structured_signature
                                project_memory.mark_touched(relative_path)
                else:
                    tool_result = self._call_mcp_tool(tool_name, arguments)

                if tool_name == "create_file":
                    relative_path = str(arguments.get("relative_path", "")).strip()
                    nested_create = tool_result.get("result") if isinstance(tool_result, dict) else None
                    write_ok = bool(
                        isinstance(nested_create, dict)
                        and nested_create.get("ok", False)
                        and not nested_create.get("deferred", False)
                    )
                    if relative_path and write_ok:
                        changed_since_validation.add(relative_path)
                        changed_since_tests.add(relative_path)
                        iteration_changed_files += 1
                        edited_files_this_iteration.add(relative_path)
                        iteration_had_non_edit_progress = True
                        substantive_edit_count += 1
                if tool_name in {"append_to_file", "insert_after_marker", "replace_range"}:
                    relative_path = str(arguments.get("relative_path", "")).strip()
                    nested_edit = tool_result.get("result") if isinstance(tool_result, dict) else None
                    edit_ok = bool(
                        isinstance(nested_edit, dict)
                        and nested_edit.get("ok", False)
                        and not nested_edit.get("deferred", False)
                    )
                    if relative_path and edit_ok:
                        changed_since_validation.add(relative_path)
                        changed_since_tests.add(relative_path)
                        iteration_changed_files += 1
                        edited_files_this_iteration.add(relative_path)
                        project_memory.mark_touched(relative_path)
                        iteration_had_non_edit_progress = True
                        substantive_edit_count += 1
                if tool_name == "scaffold_web_app":
                    nested_scaffold = tool_result.get("result") if isinstance(tool_result, dict) else None
                    scaffold_ok = bool(isinstance(nested_scaffold, dict) and nested_scaffold.get("ok", False))
                    if scaffold_ok and isinstance(nested_scaffold, dict):
                        created = nested_scaffold.get("created_or_verified", [])
                        if isinstance(created, list):
                            for item in created:
                                path_text = str(item).strip()
                                if not path_text:
                                    continue
                                normalized_rel = self._normalize_workspace_relative_path(path_text)
                                if normalized_rel and normalized_rel != ".":
                                    changed_since_validation.add(normalized_rel)
                                    changed_since_tests.add(normalized_rel)
                                    edited_files_this_iteration.add(normalized_rel)
                                    project_memory.mark_touched(normalized_rel)
                        iteration_changed_files += 1
                        iteration_had_non_edit_progress = True
                if tool_name == "read_file":
                    relative_path = str(arguments.get("relative_path", "")).strip()
                    if relative_path:
                        file_read_generation[relative_path] = file_generation.get(relative_path, 0)
                        project_memory.mark_touched(relative_path)
                        iteration_had_non_edit_progress = True
                if tool_name == "plan_web_build":
                    nested = tool_result.get("result") if isinstance(tool_result, dict) else None
                    if isinstance(nested, dict) and nested.get("ok", False):
                        phase_plan_ready = True
                        iteration_had_non_edit_progress = True
                        phases = nested.get("phases", [])
                        if isinstance(phases, list):
                            development_phases = [str(item) for item in phases if str(item).strip()] or development_phases
                        plan_reason = self._build_plan_result_reason(arguments=arguments, result=nested)
                        if plan_reason:
                            planning_entries.append(plan_reason)
                            print(
                                f"[response:agent] {json.dumps({'content': json.dumps({'type': 'reason', 'text': plan_reason}, ensure_ascii=False)}, ensure_ascii=False)}",
                                file=sys.stderr,
                                flush=True,
                            )
                if tool_name == "list_directory":
                    nested_list = tool_result.get("result") if isinstance(tool_result, dict) else None
                    if isinstance(nested_list, dict) and nested_list.get("ok", False):
                        iteration_had_non_edit_progress = True

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
                project_memory.write_event(
                    stage="tool_result",
                    payload={
                        "iteration": iteration,
                        "tool": tool_name,
                        "arguments": arguments,
                        "ok": bool(isinstance(tool_result, dict) and tool_result.get("ok", False)),
                    },
                )

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
                    (
                        "Proceed to the next phase step. "
                        "Only run validate_web_app and run_unit_tests after meaningful milestones, not after every file edit. "
                        "If complete, respond with 'DONE:' and summary."
                    ),
                )

            if iteration_changed_files == 0 and not iteration_had_non_edit_progress:
                no_progress_iterations += 1
            else:
                no_progress_iterations = 0

            # Track tool-call signature repetition across iterations
            iteration_sig = json.dumps(
                sorted(iteration_tool_names),
                sort_keys=True,
            )
            recent_tool_call_signatures.append(iteration_sig)
            if len(recent_tool_call_signatures) > max_repeated_signatures:
                recent_tool_call_signatures = recent_tool_call_signatures[-max_repeated_signatures:]
            if (
                len(recent_tool_call_signatures) >= max_repeated_signatures
                and len(set(recent_tool_call_signatures)) == 1
                and iteration_changed_files == 0
            ):
                stop_message = (
                    "STOP: Identical tool-call pattern repeated across multiple iterations with no file changes. "
                    "The agent appears stuck in a loop; stopping to avoid wasting resources."
                )
                return {
                    "ok": False,
                    "status": "stopped_no_progress",
                    "iterations": iteration,
                    "final_message": stop_message,
                    "tool_trace": tool_trace,
                    "selection_trace": selection_trace,
                    "repair_trace": repair_events,
                }

            if no_progress_iterations >= max_no_progress_iterations:
                stop_message = (
                    "STOP: No meaningful file-change progress after repeated attempts. "
                    "Validation/testing appears blocked or cyclical; stopping to avoid infinite loop."
                )
                return {
                    "ok": False,
                    "status": "stopped_no_progress",
                    "iterations": iteration,
                    "final_message": stop_message,
                    "tool_trace": tool_trace,
                    "selection_trace": selection_trace,
                    "repair_trace": repair_events,
                }

    def _is_done_message(self, content: str) -> bool:
        normalized = content.strip().upper()
        return normalized.startswith("DONE:")

    def _is_stop_message(self, content: str) -> bool:
        normalized = content.strip().upper()
        return normalized.startswith("STOP:")

    def _looks_like_completion_without_done(self, content: str) -> bool:
        normalized = content.strip()
        if not normalized:
            return False
        upper = normalized.upper()
        if upper.startswith("DONE:") or upper.startswith("STOP:"):
            return False

        lowered = normalized.lower()
        completion_markers = [
            "task has been completed",
            "task is complete",
            "project is complete",
            "project is now complete",
            "no further actions are necessary",
            "no further changes are required",
            "all unit tests have passed",
            "fully developed and tested",
            "all checks have passed",
        ]
        score = sum(1 for marker in completion_markers if marker in lowered)
        return score >= 1

    def _claims_success_without_evidence(self, *, content: str, last_validation_ok: bool, tests_runs: int) -> bool:
        lowered = content.strip().lower()
        if not lowered:
            return False
        mentions_validation = any(
            token in lowered
            for token in [
                "validated",
                "validation passed",
                "all checks passed",
                "tests have passed",
                "unit tests passed",
            ]
        )
        if not mentions_validation:
            return False
        has_evidence = last_validation_ok and tests_runs > 0
        return not has_evidence

    def _extract_tool_feedback(self, *, tool_name: str, tool_result: dict[str, Any]) -> dict[str, Any] | None:
        if tool_name not in {
            "create_file",
            "append_to_file",
            "insert_after_marker",
            "replace_range",
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
        structured_errors: list[dict[str, Any]] = []
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

            structured_errors.extend(self._parse_structured_errors(tool=tool, text=stderr or stdout or error))

        if structured_errors:
            lines.append("Structured errors (normalized JSON):")
            lines.append(json.dumps(structured_errors[:30], ensure_ascii=False))

        lines.append("Apply a fix, run the appropriate tools again, and only finish with 'DONE:' when verified.")
        return "\n".join(lines)

    def _parse_structured_errors(self, *, tool: str, text: str) -> list[dict[str, Any]]:
        if not text.strip():
            return []

        items: list[dict[str, Any]] = []
        pattern = re.compile(r"(?P<file>[^\s:]+):( ?(?P<line>\d+))?(:(?P<col>\d+))?\s*(?P<msg>.+)")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = pattern.match(line)
            if match:
                file_name = str(match.group("file") or "")
                line_no = match.group("line")
                items.append(
                    {
                        "tool": tool,
                        "file": file_name,
                        "line": int(line_no) if line_no and line_no.isdigit() else None,
                        "error_type": "diagnostic",
                        "message": str(match.group("msg") or "").strip(),
                    }
                )
            else:
                items.append(
                    {
                        "tool": tool,
                        "file": "",
                        "line": None,
                        "error_type": "diagnostic",
                        "message": line[:500],
                    }
                )
        return items

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
            elif "append" in lowered:
                canonical = "append_to_file"
            elif "insert" in lowered:
                canonical = "insert_after_marker"
            elif "replace" in lowered and "range" in lowered:
                canonical = "replace_range"
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
        if canonical in {"append_to_file", "insert_after_marker", "replace_range"}:
            if "file_path" in arguments and "relative_path" not in arguments:
                arguments["relative_path"] = arguments["file_path"]
        if canonical == "replace_range" and "replacement_text" in arguments and "content" not in arguments:
            arguments["content"] = arguments["replacement_text"]
        if canonical == "read_file" and "file_path" in arguments and "relative_path" not in arguments:
            arguments["relative_path"] = arguments["file_path"]
        if canonical == "list_directory":
            rel_dir = arguments.get("relative_path")
            if isinstance(rel_dir, str):
                normalized = self._normalize_workspace_relative_path(rel_dir)
                if normalized:
                    arguments["relative_path"] = normalized
            elif "relative_path" not in arguments:
                arguments["relative_path"] = "."
        if canonical == "validate_web_app":
            app_dir = arguments.get("app_dir")
            if isinstance(app_dir, str):
                normalized = self._normalize_workspace_relative_path(app_dir)
                if normalized:
                    arguments["app_dir"] = normalized
        if canonical == "scaffold_web_app":
            app_dir = arguments.get("app_dir")
            if isinstance(app_dir, str):
                normalized = self._normalize_workspace_relative_path(app_dir)
                if normalized:
                    arguments["app_dir"] = normalized
        if canonical == "run_unit_tests":
            test_file = arguments.get("test_file")
            if isinstance(test_file, str):
                normalized = self._normalize_workspace_relative_path(test_file)
                if normalized:
                    arguments["test_file"] = normalized
        if canonical in {"append_to_file", "insert_after_marker", "replace_range"}:
            relative_path = arguments.get("relative_path")
            if isinstance(relative_path, str):
                normalized = self._normalize_workspace_relative_path(relative_path)
                if normalized:
                    arguments["relative_path"] = normalized
        return canonical, arguments

    def _normalize_workspace_relative_path(self, raw_path: str) -> str:
        candidate = raw_path.strip()
        if not candidate:
            return candidate
        path_obj = Path(candidate)
        if not path_obj.is_absolute():
            return candidate

        resolved = path_obj.expanduser().resolve()
        try:
            relative = resolved.relative_to(self.workspace_root_path)
        except ValueError:
            return "."
        return str(relative) if str(relative) else "."

    def _merge_tools_by_name(
        self,
        selected_tools: list[dict[str, Any]],
        *,
        required_names: set[str],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        for tool in selected_tools:
            name = str(tool.get("function", {}).get("name", ""))
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            merged.append(tool)

        for tool in self.tools:
            name = str(tool.get("function", {}).get("name", ""))
            if not name or name in seen_names:
                continue
            if name in required_names:
                seen_names.add(name)
                merged.append(tool)

        return merged

    def _ensure_required_tools(
        self,
        *,
        selected_tools: list[dict[str, Any]],
        phase_plan_ready: bool,
        workspace_is_empty: bool,
    ) -> list[dict[str, Any]]:
        required_names: set[str] = {
            "list_directory",
            "read_file",
            "create_file",
            "append_to_file",
            "insert_after_marker",
            "replace_range",
        }
        if not phase_plan_ready:
            required_names.add("plan_web_build")
            if workspace_is_empty:
                required_names.add("scaffold_web_app")

        merged = self._merge_tools_by_name(selected_tools, required_names=required_names)
        limit = max(self.top_k_tools, len(required_names))
        return merged[: max(1, min(limit, len(merged)))]

    def _should_run_validation(self, *, changed_since_validation: set[str], validation_runs: int) -> bool:
        if validation_runs == 0:
            has_html = any(path.lower().endswith(".html") for path in changed_since_validation)
            has_css = any(path.lower().endswith(".css") for path in changed_since_validation)
            has_js = any(path.lower().endswith(".js") for path in changed_since_validation)
            return has_html and has_css and has_js
        return len(changed_since_validation) >= 1

    def _should_run_tests(
        self,
        *,
        changed_since_tests: set[str],
        tests_runs: int,
        last_validation_ok: bool,
        validation_ever_passed: bool = False,
    ) -> bool:
        if not last_validation_ok and not validation_ever_passed:
            return False
        has_test_file_change = any(re.search(r"(test|spec)s?\.js$", path.lower()) for path in changed_since_tests)
        if tests_runs == 0:
            return has_test_file_change and len(changed_since_tests) >= 2
        return len(changed_since_tests) >= 1

    def _deferred_tool_result(self, *, tool_name: str, reason: str) -> dict[str, Any]:
        return {
            "ok": True,
            "result": {
                "ok": True,
                "deferred": True,
                "stdout": f"{tool_name} deferred: {reason}",
                "stderr": "",
            },
        }

    def _completion_gaps(self, *, task: str, tool_trace: list[dict[str, Any]], iteration: int) -> list[str]:
        gaps: list[str] = []
        min_iterations = int(os.environ.get("ORCHESTRATOR_MIN_BUILD_ITERATIONS", "4"))
        if iteration < min_iterations:
            gaps.append(f"run more phased steps (minimum {min_iterations}, current {iteration})")

        created_files: list[str] = []
        validated_ok = False
        tests_ok_files: list[str] = []

        for item in tool_trace:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool", ""))
            arguments = item.get("arguments", {})
            result = item.get("result", {})
            nested = result.get("result") if isinstance(result, dict) else None
            nested_ok = bool(nested.get("ok", False)) if isinstance(nested, dict) else False

            if tool_name == "create_file" and isinstance(arguments, dict):
                rel = str(arguments.get("relative_path", "")).strip()
                if rel:
                    created_files.append(rel)

            if tool_name == "validate_web_app" and nested_ok:
                is_deferred = bool(isinstance(nested, dict) and nested.get("deferred", False))
                if not is_deferred:
                    validated_ok = True

            if tool_name == "run_unit_tests" and nested_ok and isinstance(arguments, dict):
                test_file = str(arguments.get("test_file", "")).strip().lower()
                if test_file:
                    tests_ok_files.append(test_file)

        lowered = [path.lower() for path in created_files]
        has_html = any(path.endswith(".html") for path in lowered)
        has_css = any(path.endswith(".css") for path in lowered)
        has_js = any(path.endswith(".js") for path in lowered)
        has_test_file = any(re.search(r"(test|spec)s?\.js$", path) for path in lowered)

        if not has_html:
            gaps.append("create/update HTML UI files")
        if not has_css:
            gaps.append("create/update CSS styling files")
        if not has_js:
            gaps.append("create/update JavaScript behavior files")
        if not has_test_file:
            gaps.append("create a real unit test file (e.g., tests.js or *.test.js)")
        if not validated_ok:
            gaps.append("run validate_web_app successfully")

        has_real_test_run = any(re.search(r"(test|spec)s?\.js$", path) for path in tests_ok_files)
        if not has_real_test_run:
            gaps.append("run run_unit_tests successfully on a real test file")

        if "note" in task.lower():
            requested_keywords = ["create", "edit", "delete"]
            detected_keywords = {key: False for key in requested_keywords}
            for item in tool_trace:
                tool_name = str(item.get("tool", ""))
                if tool_name not in {"create_file", "append_to_file", "insert_after_marker", "replace_range"}:
                    continue
                arguments = item.get("arguments", {})
                if not isinstance(arguments, dict):
                    continue
                content = str(arguments.get("content", arguments.get("replacement_text", ""))).lower()
                for key in requested_keywords:
                    if key in content:
                        detected_keywords[key] = True
            for key, present in detected_keywords.items():
                if not present:
                    gaps.append(f"implement note {key} behavior in app code")

        return gaps

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
            "If and only if everything is already complete, respond with 'DONE:' and a short summary. "
            "Do not claim completion without the exact DONE prefix. "
            "If execution is persistently blocked by environment/tooling after repeated retries, respond with 'STOP:' and a short reason."
        )

        recovery_messages = [*memory.messages, {"role": "user", "content": recovery_prompt}]
        print("[status:recovery] calling model for recovery...", file=sys.stderr, flush=True)
        recovery_tools = self._merge_tools_by_name(
            selected_tools,
            required_names={
                "list_directory",
                "read_file",
                "create_file",
                "append_to_file",
                "insert_after_marker",
                "replace_range",
                "scaffold_web_app",
                "plan_web_build",
                "validate_web_app",
                "run_unit_tests",
            },
        )
        response = self.ollama_client.chat(
            model=self.model_name,
            messages=recovery_messages,
            tools=recovery_tools,
            stream=False,
            num_ctx=_env_int("ORCHESTRATOR_AGENT_NUM_CTX", 32768),
            num_predict=_env_int("ORCHESTRATOR_AGENT_NUM_PREDICT", 8192),
        )
        assistant_message = self.ollama_client.extract_assistant_message(response)
        content = str(assistant_message.get("content", ""))
        tool_calls = self.ollama_client.extract_tool_calls(assistant_message)
        if content.strip():
            print(
                f"[response:recovery] {json.dumps({'content': content}, ensure_ascii=False)}",
                file=sys.stderr,
                flush=True,
            )
        tool_calls = self._deduplicate_tool_calls(tool_calls)
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

    def _extract_reason_entries(self, content: str) -> list[str]:
        entries: list[str] = []
        if not content.strip():
            return entries

        payloads: list[Any] = []
        stripped = content.strip()
        try:
            payloads.append(json.loads(stripped))
        except json.JSONDecodeError:
            pass

        marker = "```"
        cursor = 0
        while True:
            start = content.find(marker, cursor)
            if start == -1:
                break
            end = content.find(marker, start + len(marker))
            if end == -1:
                break
            block = content[start + len(marker) : end].strip()
            if block.lower().startswith("json"):
                block = block[4:].strip()
            if block:
                try:
                    payloads.append(json.loads(block))
                except json.JSONDecodeError:
                    pass
            cursor = end + len(marker)

        def collect(payload: Any) -> None:
            if isinstance(payload, list):
                for item in payload:
                    collect(item)
                return
            if not isinstance(payload, dict):
                return
            entry_type = str(payload.get("type", "")).strip().lower()
            if entry_type == "reason":
                text = str(payload.get("text", payload.get("message", payload.get("content", "")))).strip()
                if text:
                    entries.append(text)

        for payload in payloads:
            collect(payload)

        return entries

    def _text_signature(self, value: str) -> str:
        return str(abs(hash(value)))

    def _resolve_workspace_file(self, relative_path: str) -> Path | None:
        rel = relative_path.strip()
        if not rel:
            return None
        candidate = (self.workspace_root_path / rel).resolve()
        try:
            candidate.relative_to(self.workspace_root_path)
        except ValueError:
            return None
        return candidate

    def _build_edit_sync_prompt(self, *, relative_path: str, planning_context: str, full_file_content: str) -> str:
        return (
            f"Before editing '{relative_path}', use synchronized context below.\n"
            "Planning entries currently in memory:\n"
            f"{planning_context}\n\n"
            f"Complete current file content for '{relative_path}':\n"
            f"{full_file_content}\n\n"
            "Now produce an updated full-file create_file tool call for this same path that preserves cross-file consistency."
        )

    def _build_structured_edit_sync_prompt(
        self,
        *,
        tool_name: str,
        relative_path: str,
        planning_context: str,
        full_file_content: str,
    ) -> str:
        return (
            f"Before running {tool_name} on '{relative_path}', use synchronized context below.\n"
            "Planning entries currently in memory:\n"
            f"{planning_context}\n\n"
            f"Complete current file content for '{relative_path}':\n"
            f"{full_file_content}\n\n"
            f"Now produce a corrected {tool_name} tool call for '{relative_path}' with precise parameters based on this latest content."
        )

    def _read_local_file(self, path: Path | None, *, max_chars: int = 200000) -> str:
        if path is None or not path.exists() or not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if len(text) > max_chars:
            return text[:max_chars]
        return text

    def _build_diff_reflection_prompt(self, *, relative_path: str, before: str, after: str) -> str:
        if before == after:
            return ""
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        diff_lines = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"{relative_path}:before",
                tofile=f"{relative_path}:after",
                lineterm="",
            )
        )
        if not diff_lines:
            return ""
        preview = "\n".join(diff_lines[:220])
        return (
            f"Diff reflection for {relative_path}:\n"
            f"{preview}\n\n"
            "Reflect briefly in type=reason: does this fully resolve the intended change and any edge cases?"
        )

    def _build_tool_only_reasoning(self, tool_calls: list[dict[str, Any]]) -> str:
        names = [str(item.get("name", "")).strip() for item in tool_calls if isinstance(item, dict)]
        names = [name for name in names if name]
        if not names:
            return ""

        plan_calls = [
            item for item in tool_calls if isinstance(item, dict) and str(item.get("name", "")).strip() == "plan_web_build"
        ]
        if plan_calls:
            latest_plan_call = plan_calls[-1]
            args = latest_plan_call.get("arguments", {})
            if not isinstance(args, dict):
                args = {}
            summary = str(args.get("summary", "")).strip()
            features = args.get("prompt_features", [])
            feature_list: list[str] = []
            if isinstance(features, list):
                feature_list = [str(item).strip() for item in features if str(item).strip()]
            feature_text = ", ".join(feature_list[:8]) if feature_list else "(no explicit feature list)"
            if summary:
                return (
                    "Planning update: "
                    f"summary = {summary}. "
                    f"Requested focus features = {feature_text}. "
                    "Next I will refine concrete implementation phases and map them to specific files."
                )
            return (
                "Planning update: plan_web_build was requested to produce concrete development phases. "
                f"Requested focus features = {feature_text}. "
                "Next I will use the generated phases to drive file-by-file implementation."
            )

        unique = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            unique.append(name)
        listing = ", ".join(unique)
        return (
            "I am taking an action-only step this turn to gather state and progress the plan. "
            f"I selected: {listing}. After these results return, I will reason over them and continue with the next concrete edits."
        )

    def _build_plan_result_reason(self, *, arguments: dict[str, Any], result: dict[str, Any]) -> str:
        summary = str(arguments.get("summary", "")).strip()
        phases = result.get("phases", [])
        phase_items: list[str] = []
        if isinstance(phases, list):
            phase_items = [str(item).strip() for item in phases if str(item).strip()]
        if not phase_items and not summary:
            return ""

        phase_preview = " | ".join(phase_items[:6]) if phase_items else "(no phases returned)"
        if summary:
            return (
                "Plan generated successfully. "
                f"Summary: {summary}. "
                f"Phases: {phase_preview}. "
                "I will now execute these phases in order using focused edits and verification checkpoints."
            )
        return (
            "Plan generated successfully. "
            f"Phases: {phase_preview}. "
            "I will now execute these phases in order using focused edits and verification checkpoints."
        )

    def _is_workspace_empty(self) -> bool:
        ignored_roots = {
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "dist",
            "build",
            "coverage",
            "__pycache__",
            ".low-cortisol-html-logs",
        }
        for path in self.workspace_root_path.rglob("*"):
            try:
                rel = path.relative_to(self.workspace_root_path)
            except ValueError:
                continue
            if not rel.parts:
                continue
            if any(part in ignored_roots or part.startswith(".") for part in rel.parts):
                continue
            return False
        return True

    def _deduplicate_tool_calls(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove duplicate tool calls (model often repeats the same call in content)."""
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for call in tool_calls:
            name = str(call.get("name", ""))
            args = call.get("arguments", {})
            key = json.dumps({"name": name, "arguments": args}, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            unique.append(call)
        return unique
