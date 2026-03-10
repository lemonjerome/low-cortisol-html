"""Multi-stage pipeline agent for building HTML/CSS/JS apps.

Pipeline:
    1. Detect workspace state (empty vs populated)
    2. feature_plan — General plan + features (context: user prompt + existing files)
    3. html_code   — Create HTML (context: general plan + html.md skill + existing html)
    4. js_code     — Create JS  (context: general plan + js.md skill + completed html + existing js)
    5. css_code    — Create CSS (context: general plan + css.md skill + completed html + existing css)
    6. validate    — Syntax and error validation
    7. summary     — Working summary (displayed in chat column with markdown)
    8. Done
"""

from __future__ import annotations

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

# Ordered pipeline stages
STAGES: list[tuple[str, str]] = [
    ("feature_plan", "Plan the general app concept, features, and file structure"),
    ("html_code", "Write the complete index.html file"),
    ("js_code", "Write the complete script.js file"),
    ("css_code", "Write the complete styles.css file"),
]

# Tools allowed per stage
STAGE_TOOLS: dict[str, list[str]] = {
    "feature_plan": ["plan_web_build", "read_file", "list_directory"],
    "html_code": ["create_file", "read_file"],
    "js_code": ["create_file", "read_file"],
    "css_code": ["create_file", "read_file"],
}

SYSTEM_PROMPT = (
    "You are a frontend coding agent that builds HTML/CSS/JS web apps.\n"
    "You work in sequential stages: plan features, write HTML, write JS, write CSS.\n"
    "Each stage focuses on ONE task. Follow the skill guides provided.\n"
    "\n"
    "CRITICAL RULES:\n"
    "- Use RELATIVE paths only (e.g. 'index.html', 'styles.css', 'script.js').\n"
    "  NEVER use absolute paths like /root/Desktop/... or /home/user/...\n"
    "- The planning stage: reason about what to build. Be thorough.\n"
    "- Coding stages: use the create_file tool to write COMPLETE file contents.\n"
    "  Each coding stage writes exactly ONE file. The file must be complete.\n"
    "- Always generate code that matches the planned features exactly.\n"
    "- Keep reasoning in plain text, no JSON envelopes.\n"
    "- Do NOT prefix lines with 'type=reason' or 'type=signal'.\n"
    "- Use only the tools provided for each stage.\n"
    "- For create_file: always write the full file, not partial snippets.\n"
    "\n"
    "CROSS-FILE CLASS NAME CONTRACT (Critical):\n"
    "- HTML is the source of truth for all IDs and class names.\n"
    "- JS must reference ONLY IDs and classes that exist in the HTML.\n"
    "- CSS must style ONLY IDs and classes that exist in the HTML.\n"
    "- The ONLY state classes for toggling visibility are: hidden, active, disabled.\n"
    "- NEVER use: is-open, is-hidden, is-visible, show, visible, open, closed.\n"
    "- Modals start with class='hidden' in HTML. JS removes 'hidden' to show.\n"
    "- CSS must always define: .hidden { display: none !important; }\n"
    "- When JS creates dynamic elements, it must use class names that CSS styles.\n"
    "- All three files must agree on every class name. No mismatches.\n"
)


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
    """Multi-stage pipeline controller.

    Detects whether the workspace is empty (new project) or populated
    (existing project), then runs:
      feature_plan -> html_code -> js_code -> css_code -> validate -> summary -> Done.

    Skill files (skills/html.md, skills/js.md, skills/css.md) are injected
    as context for each coding stage.
    """

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

        # Project memory for file-level semantic retrieval
        events_log = project_root / "logs" / "project_memory.log"
        self.project_memory = ProjectMemory(
            workspace_root=self.workspace_root_path,
            ollama_client=ollama_client,
            embedding_model=os.environ.get("EMBEDDING_MODEL", "nomic-embed-text"),
            events_log_path=events_log,
        )

        # Index tools by name for quick lookup
        self._tools_by_name: dict[str, dict[str, Any]] = {}
        for tool in tools:
            name = str(tool.get("function", {}).get("name", ""))
            if name:
                self._tools_by_name[name] = tool

    # ------------------------------------------------------------------
    # Workspace detection
    # ------------------------------------------------------------------

    def _detect_workspace_state(self) -> dict[str, Any]:
        """Detect whether the workspace is empty or populated.

        Returns:
            {
                "is_empty": bool,
                "files": list[str],        -- relative paths of existing files
                "file_contents": dict,      -- {rel_path: content} for key files
            }
        """
        ignored = {
            ".git", ".venv", "venv", "node_modules", "__pycache__",
            ".low-cortisol-html-logs", ".DS_Store",
        }
        files: list[str] = []
        for path in sorted(self.workspace_root_path.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(self.workspace_root_path).as_posix())
            if any(
                part.startswith(".") or part in ignored
                for part in rel.split("/")
            ):
                continue
            files.append(rel)

        is_empty = len(files) == 0

        # For populated workspaces, read key file contents
        file_contents: dict[str, str] = {}
        if not is_empty:
            code_extensions = {".html", ".css", ".js", ".json", ".md", ".txt"}
            for rel in files[:30]:  # cap to avoid huge context
                ext = Path(rel).suffix.lower()
                if ext not in code_extensions:
                    continue
                fpath = self.workspace_root_path / rel
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    if len(content) > 10000:
                        content = content[:10000] + "\n... (truncated)"
                    file_contents[rel] = content
                except OSError:
                    continue

        return {
            "is_empty": is_empty,
            "files": files,
            "file_contents": file_contents,
        }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, task: str) -> dict[str, Any]:
        memory = SessionMemory()
        memory.add("system", SYSTEM_PROMPT)
        memory.add("user", f"Task: {task}")

        tool_trace: list[dict[str, Any]] = []
        iteration = 0
        created_files: set[str] = set()
        max_tool_calls = _env_int("ORCHESTRATOR_MAX_TOOL_CALLS_PER_ITERATION", 12)

        # --- Load skill files ---
        skill_texts: dict[str, str] = {}
        for skill_name in ("html", "js", "css"):
            skill_path = self.project_root / "skills" / f"{skill_name}.md"
            try:
                skill_texts[skill_name] = skill_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skill_texts[skill_name] = ""

        # --- Detect workspace state ---
        workspace_state = self._detect_workspace_state()
        is_empty = workspace_state["is_empty"]

        if is_empty:
            memory.add("user",
                "Workspace is empty. This is a new project. "
                "All files need to be created from scratch."
            )
            self._emit_reasoning_raw("system", "Detected: empty workspace — new project")
        else:
            file_list = "\n".join(f"- {f}" for f in workspace_state["files"])
            memory.add("user", f"Current workspace files:\n{file_list}")
            self._emit_reasoning_raw("system",
                f"Detected: populated workspace — {len(workspace_state['files'])} existing file(s)"
            )
            # Inject existing file contents into context
            if workspace_state["file_contents"]:
                parts: list[str] = []
                for rel, content in workspace_state["file_contents"].items():
                    parts.append(f"--- {rel} ---\n{content}\n--- end {rel} ---")
                    created_files.add(rel)  # track as known files
                memory.add("user",
                    "=== EXISTING FILE CONTENTS ===\n"
                    + "\n\n".join(parts)
                    + "\n=== END EXISTING FILE CONTENTS ==="
                )

        # --- Run planner for initial retrieval query ---
        planner_result: dict[str, Any] = {}
        try:
            print("[status:agent] running planner...", file=sys.stderr, flush=True)
            planner_result = self.planner.plan_step(
                task=task,
                iteration=1,
                recent_messages=memory.messages[-4:],
            )
            retrieval_query = str(planner_result.get("retrieval_query", task))
            rationale = str(planner_result.get("rationale", "")).strip()
            if rationale:
                self._emit_reasoning_raw("planner", f"Plan: {rationale}")
        except Exception:
            retrieval_query = task
            planner_result = {}

        self._current_retrieval_query = retrieval_query
        self._current_plan = planner_result

        # Track the general plan text for downstream stages
        general_plan_text: str = ""

        # --- Execute stages ---
        for stage_name, stage_desc in STAGES:
            iteration += 1
            print(f"[status:agent] stage: {stage_name}", file=sys.stderr, flush=True)
            stage_label = stage_name.replace("_", " ").title()
            self._emit_reasoning(stage_name, f"Starting stage: {stage_label}")

            # Build stage prompt with appropriate context
            stage_prompt = self._build_stage_prompt(
                stage_name=stage_name,
                stage_desc=stage_desc,
                task=task,
                created_files=created_files,
                workspace_state=workspace_state,
                general_plan=general_plan_text,
                skill_texts=skill_texts,
            )
            memory.add("user", stage_prompt)

            # Get tools for this stage
            stage_tools = self._get_pruned_tools(
                query=f"{stage_desc} for task: {task}",
                stage_name=stage_name,
            )

            # Refresh project memory
            try:
                self.project_memory.refresh()
            except Exception:
                pass

            is_code_stage = stage_name.endswith("_code")
            num_predict = _env_int("ORCHESTRATOR_AGENT_NUM_PREDICT", 8192)
            if is_code_stage:
                num_predict = _env_int("ORCHESTRATOR_CODE_NUM_PREDICT", 16384)

            # Call LLM with robust error handling
            try:
                print("[status:agent] calling model...", file=sys.stderr, flush=True)
                response = self.ollama_client.chat(
                    model=self.model_name,
                    messages=memory.messages,
                    tools=stage_tools,
                    stream=False,
                    num_ctx=_env_int("ORCHESTRATOR_AGENT_NUM_CTX", 40000),
                    num_predict=num_predict,
                )
                message = self.ollama_client.extract_assistant_message(response)
                content = str(message.get("content", ""))
                tool_calls = self.ollama_client.extract_tool_calls(message)
            except RuntimeError as err:
                err_msg = str(err)
                if "XML syntax error" in err_msg or "unexpected end element" in err_msg:
                    self._emit_reasoning(stage_name, f"Model returned malformed XML, retrying without tools...")
                    # Retry without tools to avoid XML parsing issue
                    try:
                        response = self.ollama_client.chat(
                            model=self.model_name,
                            messages=memory.messages,
                            tools=[],
                            stream=False,
                            num_ctx=_env_int("ORCHESTRATOR_AGENT_NUM_CTX", 40000),
                            num_predict=num_predict,
                        )
                        message = self.ollama_client.extract_assistant_message(response)
                        content = str(message.get("content", ""))
                        tool_calls = []
                        # Try to extract tool calls from the text content
                        if content.strip() and is_code_stage:
                            tool_calls = self._extract_tool_calls_from_text(content)
                    except RuntimeError:
                        self._emit_reasoning(stage_name, f"Retry also failed. Skipping stage.")
                        continue
                else:
                    raise

            # Normalize and deduplicate tool calls
            tool_calls = [
                {"name": n, "arguments": a}
                for n, a in (self._normalize_tool_call(tc) for tc in tool_calls)
            ]
            tool_calls = self._deduplicate_tool_calls(tool_calls)

            # Adaptive: extract tool calls from text if model wrote them inline
            if not tool_calls and content.strip() and is_code_stage:
                inline_calls = self._extract_tool_calls_from_text(content)
                if inline_calls:
                    tool_calls = inline_calls
                    self._emit_reasoning(
                        stage_name,
                        f"Extracted {len(inline_calls)} tool call(s) from model text output",
                    )

            # Adaptive: retry on empty response
            if not content.strip() and not tool_calls:
                self._emit_reasoning(stage_name, "Empty model response, retrying...")
                nudge = (
                    f"You did not produce any output for stage {stage_name}. "
                    "Please complete this stage now. "
                    "Use the tools provided as instructed."
                )
                memory.add("user", nudge)
                response = self.ollama_client.chat(
                    model=self.model_name,
                    messages=memory.messages,
                    tools=stage_tools,
                    stream=False,
                    num_ctx=_env_int("ORCHESTRATOR_AGENT_NUM_CTX", 40000),
                    num_predict=num_predict,
                )
                message = self.ollama_client.extract_assistant_message(response)
                content = str(message.get("content", ""))
                tool_calls = self.ollama_client.extract_tool_calls(message)
                tool_calls = [
                    {"name": n, "arguments": a}
                    for n, a in (self._normalize_tool_call(tc) for tc in tool_calls)
                ]
                tool_calls = self._deduplicate_tool_calls(tool_calls)
                if not tool_calls and content.strip() and is_code_stage:
                    tool_calls = self._extract_tool_calls_from_text(content)

            # Emit reasoning to UI
            if content.strip():
                self._emit_reasoning(stage_name, content)
            elif tool_calls and is_code_stage:
                file_names = []
                for tc in tool_calls:
                    tc_name = str(tc.get("name", ""))
                    tc_args = tc.get("arguments", {})
                    if tc_name in ("create_file", "write_file", "edit_file") and isinstance(tc_args, dict):
                        rel = str(tc_args.get("relative_path", tc_args.get("file_path", ""))).strip()
                        if rel:
                            file_names.append(rel)
                if file_names:
                    self._emit_reasoning(stage_name, f"Writing files: {', '.join(file_names)}")

            # Capture general plan text for downstream stages
            clean_content = self._strip_think_tags(content).strip()
            if stage_name == "feature_plan":
                general_plan_text = clean_content

            # Execute tool calls
            allowed = set(STAGE_TOOLS.get(stage_name, []))
            executed_count = 0

            for call in tool_calls:
                if executed_count >= max_tool_calls:
                    break

                name = str(call.get("name", "")).strip()
                args = call.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}

                if name not in allowed:
                    continue

                # Skip empty file writes
                if name == "create_file":
                    rel = str(args.get("relative_path", "")).strip()
                    content_val = str(args.get("content", ""))
                    if not rel or not content_val.strip():
                        continue

                self._emit_tool_call_event(tool_name=name, arguments=args)
                result = self._call_mcp_tool(name, args)

                result_reasoning = self._format_tool_result_reasoning(name=name, result=result)
                if result_reasoning:
                    self._emit_reasoning(stage_name, result_reasoning)

                # Emit file content as a code block for create_file
                if name == "create_file":
                    rel = str(args.get("relative_path", "")).strip()
                    file_content = str(args.get("content", ""))
                    if rel and file_content.strip():
                        self._emit_code_block(rel, file_content)

                tool_trace.append({
                    "iteration": iteration,
                    "stage": stage_name,
                    "tool": name,
                    "arguments": args,
                    "result": result,
                })
                memory.add("tool", json.dumps(result), name=name)
                executed_count += 1

                # Track created files
                if name == "create_file":
                    rel = str(args.get("relative_path", "")).strip()
                    nested = result.get("result") if isinstance(result, dict) else None
                    if rel and isinstance(nested, dict) and nested.get("ok", False):
                        created_files.add(rel)
                        self.project_memory.mark_touched(rel)

            memory.add("assistant", content, tool_calls=tool_calls)
            self._compact_memory(memory)

        # --- Run validation at the end ---
        self._run_validation(tool_trace=tool_trace, memory=memory, iteration=iteration + 1)

        # --- Generate summary (displayed in chat column with markdown) ---
        summary = self._generate_summary(task=task, tool_trace=tool_trace)

        return {
            "ok": True,
            "status": "completed",
            "iterations": iteration,
            "final_message": self._as_chat_envelope(summary),
            "tool_trace": tool_trace,
            "selection_trace": [],
            "repair_trace": [],
        }

    # ------------------------------------------------------------------
    # Stage prompt builder
    # ------------------------------------------------------------------

    def _build_stage_prompt(
        self,
        *,
        stage_name: str,
        stage_desc: str,
        task: str,
        created_files: set[str],
        workspace_state: dict[str, Any],
        general_plan: str = "",
        skill_texts: dict[str, str] | None = None,
    ) -> str:
        is_empty = workspace_state["is_empty"]
        skill_texts = skill_texts or {}
        lines = [f"=== STAGE: {stage_name} ===", stage_desc, ""]

        if created_files:
            lines.append("Known files: " + ", ".join(sorted(created_files)))
            lines.append("")

        # ------ FEATURE_PLAN stage ------
        if stage_name == "feature_plan":
            if is_empty:
                lines.extend(self._build_new_project_feature_plan_prompt(task))
            else:
                lines.extend(self._build_existing_project_feature_plan_prompt(task, workspace_state))

        # ------ HTML_CODE stage ------
        elif stage_name == "html_code":
            lines.extend(self._build_html_code_prompt(
                task, general_plan, created_files, workspace_state, skill_texts.get("html", ""),
            ))

        # ------ JS_CODE stage ------
        elif stage_name == "js_code":
            lines.extend(self._build_js_code_prompt(
                task, general_plan, created_files, workspace_state, skill_texts.get("js", ""),
            ))

        # ------ CSS_CODE stage ------
        elif stage_name == "css_code":
            lines.extend(self._build_css_code_prompt(
                task, general_plan, created_files, workspace_state, skill_texts.get("css", ""),
            ))

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Feature plan prompts
    # ------------------------------------------------------------------

    def _build_new_project_feature_plan_prompt(self, task: str) -> list[str]:
        """Build a comprehensive feature planning prompt for a fresh/empty project."""
        file_context = self._get_relevant_file_context(task)
        lines: list[str] = []
        if file_context:
            lines.extend([
                "=== WORKSPACE FILE CONTEXT ===",
                file_context,
                "=== END WORKSPACE FILE CONTEXT ===",
                "",
            ])

        lines.extend([
            f"Original request: {task}",
            "",
            "This is a NEW, EMPTY project. You must plan everything from scratch.",
            "",
            "Create a high-level plan covering:",
            "",
            "1. FEATURES — List every feature the app needs. Be specific:",
            "   - What does the user see on load?",
            "   - What interactions are available (forms, buttons, lists)?",
            "   - What data does the app manage?",
            "   - What happens on each user action?",
            "",
            "2. UI FLOW — For each action, describe the COMPLETE user flow:",
            "   - Where is the button/trigger? (must be ALWAYS visible, not hidden in conditional sections)",
            "   - What happens when clicked? (modal opens, form appears, etc.)",
            "   - How does the user complete the action? (submit, cancel, etc.)",
            "   - What state needs tracking? (e.g., which item is being edited)",
            "   CRITICAL: The primary 'Add/Create' button must be in the header or toolbar,",
            "   NOT only inside a welcome/empty-state message that disappears when items exist.",
            "",
            "3. FILE STRUCTURE:",
            "   - index.html — the main HTML file",
            "   - styles.css — all styling",
            "   - script.js — all JavaScript logic",
            "",
            "4. CONNECTIONS — How files reference each other:",
            "   - HTML -> CSS: which classes/IDs CSS targets",
            "   - HTML -> JS: which IDs JS queries",
            "   - JS -> HTML: which elements JS creates or modifies dynamically",
            "",
            "5. CLASS NAME CONTRACT — List the exact class names that will be shared:",
            "   - State classes: hidden (for toggling visibility), active, disabled",
            "   - Button classes: btn, btn-primary, btn-secondary, btn-danger, btn-edit, btn-delete",
            "   - Dynamic element classes: e.g., note-card, note-card-header, note-card-body, note-card-actions",
            "   - All three files (HTML, JS, CSS) MUST use these exact class names",
            "",
            "Be THOROUGH. All subsequent coding stages rely on this plan.",
            "Use plan_web_build to register the plan when done.",
            "Do NOT create any files yet.",
        ])
        return lines

    def _build_existing_project_feature_plan_prompt(
        self, task: str, workspace_state: dict[str, Any],
    ) -> list[str]:
        """Build a feature planning prompt for an existing/populated project."""
        lines: list[str] = []
        lines.extend([
            f"Original request: {task}",
            "",
            "This is an EXISTING project with files already in place.",
            "The existing file contents have been provided above in the conversation.",
            "",
            "Your job: study the existing code, understand the current state,",
            "then plan what changes or additions are needed.",
            "",
            "Create a plan covering:",
            "",
            "1. CURRENT STATE ANALYSIS:",
            "   - What does the app currently do?",
            "   - What files exist and what is in them?",
            "   - What is working, what is missing, what is broken?",
            "",
            "2. REQUIRED CHANGES — For each file that needs modification:",
            "   - File name",
            "   - What specifically needs to change",
            "   - New elements, functions, or styles to add",
            "",
            "3. NEW FILES — If any new files are needed",
            "",
            "4. CONNECTIONS — How changes affect other files",
            "",
            "Be THOROUGH. All subsequent coding stages rely on this plan.",
            "Every file written later must be COMPLETE (not just changed parts).",
            "Use plan_web_build to register the plan when done.",
            "Do NOT create any files yet.",
        ])
        return lines

    # ------------------------------------------------------------------
    # HTML code prompt
    # ------------------------------------------------------------------

    def _build_html_code_prompt(
        self,
        task: str,
        general_plan: str,
        created_files: set[str],
        workspace_state: dict[str, Any],
        skill_text: str,
    ) -> list[str]:
        """Build the HTML coding prompt with general plan + skill guide + existing HTML."""
        lines: list[str] = []

        # Inject general plan
        if general_plan:
            lines.extend([
                "=== GENERAL PLAN (from previous stage) ===",
                general_plan,
                "=== END GENERAL PLAN ===",
                "",
            ])

        # Inject HTML skill guide
        if skill_text:
            lines.extend([
                "=== HTML SKILL GUIDE ===",
                skill_text,
                "=== END HTML SKILL GUIDE ===",
                "",
            ])

        # For existing projects, include current HTML for reference
        if not workspace_state["is_empty"]:
            html_content = self._read_created_files(
                created_files, extensions={".html"},
            )
            if html_content:
                lines.extend([
                    "=== CURRENT HTML (existing file — rewrite completely) ===",
                    html_content,
                    "=== END CURRENT HTML ===",
                    "",
                ])

        lines.extend([
            f"Task: {task}",
            "",
            "Write the COMPLETE index.html file.",
            "Use create_file to write the full file.",
            "",
            "REQUIREMENTS:",
            "- Follow the HTML Skill Guide above for structure and naming conventions",
            "- Every interactive element MUST have a unique, descriptive id (kebab-case)",
            "- Use semantic HTML5 (<header>, <main>, <section>, <form>, <footer>)",
            "- Include <link rel=\"stylesheet\" href=\"styles.css\"> in <head>",
            "- Include <script src=\"script.js\"></script> before </body>",
            "- Write the COMPLETE file, not partial snippets",
            "- Implement ALL features from the general plan",
            "",
            "CROSS-FILE CONTRACT (Critical):",
            "- Elements that start hidden MUST have class='hidden' (e.g., modals: class='modal-overlay hidden')",
            "- Use standard button classes: btn, btn-primary, btn-secondary, btn-danger",
            "- Add an HTML comment documenting the dynamic element template (class names JS will use)",
            "- All class names defined here will be referenced by JS and styled by CSS",
            "- The primary Add/Create button MUST be in the header or a persistent toolbar,",
            "  NOT only inside a welcome/empty-state section that disappears when items exist",
            "",
            "Call create_file with relative_path='index.html' and the full HTML content.",
        ])
        return lines

    # ------------------------------------------------------------------
    # JS code prompt
    # ------------------------------------------------------------------

    def _build_js_code_prompt(
        self,
        task: str,
        general_plan: str,
        created_files: set[str],
        workspace_state: dict[str, Any],
        skill_text: str,
    ) -> list[str]:
        """Build the JS coding prompt with general plan + skill guide + completed HTML + existing JS."""
        lines: list[str] = []

        # Inject general plan
        if general_plan:
            lines.extend([
                "=== GENERAL PLAN (from previous stage) ===",
                general_plan,
                "=== END GENERAL PLAN ===",
                "",
            ])

        # Inject JS skill guide
        if skill_text:
            lines.extend([
                "=== JAVASCRIPT SKILL GUIDE ===",
                skill_text,
                "=== END JAVASCRIPT SKILL GUIDE ===",
                "",
            ])

        # Include completed HTML so JS references exact IDs
        html_content = self._read_created_files(
            created_files, extensions={".html"},
        )
        if html_content:
            lines.extend([
                "=== COMPLETED HTML (reference element IDs from this) ===",
                html_content,
                "=== END COMPLETED HTML ===",
                "",
            ])

        # For existing projects, include current JS for reference
        if not workspace_state["is_empty"]:
            js_content = self._read_created_files(
                created_files, extensions={".js"},
            )
            if js_content:
                lines.extend([
                    "=== CURRENT JS (existing file — rewrite completely) ===",
                    js_content,
                    "=== END CURRENT JS ===",
                    "",
                ])

        lines.extend([
            f"Task: {task}",
            "",
            "Write the COMPLETE script.js file.",
            "Use create_file to write the full file.",
            "",
            "REQUIREMENTS:",
            "- Follow the JavaScript Skill Guide above",
            "- Reference the EXACT element IDs from the completed HTML above",
            "- Separate pure logic into named functions",
            "- Check elements exist before using them (null safety)",
            "- Use DOMContentLoaded event listener",
            "- Write the COMPLETE file, not partial snippets",
            "",
            "CROSS-FILE CONTRACT (Critical):",
            "- Use ONLY 'hidden' class for visibility toggling (classList.add/remove)",
            "- NEVER use 'is-open', 'show', 'visible', or any other toggle class",
            "- When creating dynamic elements, use EXACT class names from the HTML template",
            "- Use standard button classes: btn, btn-primary, btn-secondary, btn-danger, btn-edit, btn-delete",
            "- Do NOT invent new class names that are not in the HTML",
            "- For edit flows: track which item is being edited with a module-level variable (let currentEditId = null)",
            "  Do NOT rely on form.dataset unless you explicitly set it when opening the edit modal",
            "- Do NOT use escapeHtml() when setting input.value or textarea.value (plain text, not HTML)",
            "  Only use escapeHtml() inside innerHTML or template literals",
            "",
            "Call create_file with relative_path='script.js' and the full JS content.",
        ])
        return lines

    # ------------------------------------------------------------------
    # CSS code prompt
    # ------------------------------------------------------------------

    def _build_css_code_prompt(
        self,
        task: str,
        general_plan: str,
        created_files: set[str],
        workspace_state: dict[str, Any],
        skill_text: str,
    ) -> list[str]:
        """Build the CSS coding prompt with general plan + skill guide + completed HTML + completed JS + existing CSS."""
        lines: list[str] = []

        # Inject general plan
        if general_plan:
            lines.extend([
                "=== GENERAL PLAN (from previous stage) ===",
                general_plan,
                "=== END GENERAL PLAN ===",
                "",
            ])

        # Inject CSS skill guide
        if skill_text:
            lines.extend([
                "=== CSS SKILL GUIDE ===",
                skill_text,
                "=== END CSS SKILL GUIDE ===",
                "",
            ])

        # Include completed HTML so CSS targets exact elements
        html_content = self._read_created_files(
            created_files, extensions={".html"},
        )
        if html_content:
            lines.extend([
                "=== COMPLETED HTML (target selectors from this) ===",
                html_content,
                "=== END COMPLETED HTML ===",
                "",
            ])

        # Include completed JS so CSS can see what classes JS toggles and creates
        js_content = self._read_created_files(
            created_files, extensions={".js"},
        )
        if js_content:
            lines.extend([
                "=== COMPLETED JS (style every class name used here) ===",
                js_content,
                "=== END COMPLETED JS ===",
                "",
            ])

        # For existing projects, include current CSS for reference
        if not workspace_state["is_empty"]:
            css_content = self._read_created_files(
                created_files, extensions={".css"},
            )
            if css_content:
                lines.extend([
                    "=== CURRENT CSS (existing file — rewrite completely) ===",
                    css_content,
                    "=== END CURRENT CSS ===",
                    "",
                ])

        lines.extend([
            f"Task: {task}",
            "",
            "Write the COMPLETE styles.css file.",
            "Use create_file to write the full file.",
            "",
            "REQUIREMENTS:",
            "- Follow the CSS Skill Guide above",
            "- Target the EXACT IDs, classes, and elements from the completed HTML",
            "- Style EVERY class name that appears in the completed JS (dynamic elements)",
            "- Do NOT invent selectors for elements that don't exist",
            "- Include responsive design and clean typography",
            "- Use flexbox/grid for layout",
            "- Default should be fun and colorful design unless tone is specified",
            "- Write the COMPLETE file, not partial snippets",
            "",
            "CROSS-FILE CONTRACT (Critical):",
            "- MUST define: .hidden { display: none !important; }",
            "- MUST style all button classes: .btn, .btn-primary, .btn-secondary, .btn-danger, .btn-edit, .btn-delete",
            "- MUST style all dynamic element classes from JS (e.g., .note-card, .note-card-header, etc.)",
            "- MUST style .modal-overlay and .modal-content",
            "- Do NOT define .is-open, .is-hidden, or any non-standard toggle classes",
            "- Every class in HTML and JS must have a corresponding CSS rule",
            "",
            "Call create_file with relative_path='styles.css' and the full CSS content.",
        ])
        return lines

    # ------------------------------------------------------------------
    # Validation (informational — runs after code stage)
    # ------------------------------------------------------------------

    def _run_validation(
        self,
        *,
        tool_trace: list[dict[str, Any]],
        memory: SessionMemory,
        iteration: int,
    ) -> None:
        """Run validate_web_app to check the output. Informational only."""
        print("[status:agent] running validation...", file=sys.stderr, flush=True)
        val_args: dict[str, Any] = {"app_dir": "."}
        self._emit_tool_call_event(tool_name="validate_web_app", arguments=val_args)
        val_result = self._call_mcp_tool("validate_web_app", val_args)
        self._emit_terminal_logs("validate_web_app", val_result)
        tool_trace.append({
            "iteration": iteration,
            "stage": "validate",
            "tool": "validate_web_app",
            "arguments": val_args,
            "result": val_result,
        })
        memory.add("tool", json.dumps(val_result), name="validate_web_app")

        val_nested = val_result.get("result") if isinstance(val_result, dict) else None
        val_ok = bool(isinstance(val_nested, dict) and val_nested.get("ok", False))
        if val_ok:
            self._emit_reasoning("validate", "Validation passed — all files look good.")
        else:
            details = self._extract_error_details(val_result)
            self._emit_reasoning("validate", f"Validation found issues:\n{details}")

    # ------------------------------------------------------------------
    # Tool helpers
    # ------------------------------------------------------------------

    def _get_stage_tools(self, stage_name: str) -> list[dict[str, Any]]:
        """Return tool definitions allowed for a given stage."""
        allowed_names = STAGE_TOOLS.get(stage_name, [])
        tools: list[dict[str, Any]] = []
        for name in allowed_names:
            if name in self._tools_by_name:
                tools.append(self._tools_by_name[name])
        return tools or self.tools[:3]

    def _get_pruned_tools(self, *, query: str, stage_name: str) -> list[dict[str, Any]]:
        """Return the stage-required tools.

        Tool pruning logs relevance scores for debugging,
        but the final tool list is always the static STAGE_TOOLS mapping.
        """
        stage_tools = self._get_stage_tools(stage_name)
        tool_names = [self._tool_name(t) for t in stage_tools]

        # Log pruning info for debugging (non-blocking)
        try:
            combined_query = query
            planner_query = getattr(self, "_current_retrieval_query", "")
            if planner_query and planner_query != query:
                combined_query = f"{query} | {planner_query}"
            self.tool_pruner.retrieve_candidates(
                query=combined_query,
                tools=self.tools,
                top_n=self.candidate_pool_size,
            )
        except Exception:
            pass

        self._emit_reasoning_raw(
            "reranker",
            f"Tools for {stage_name}: " + ", ".join(sorted(tool_names)),
        )
        return stage_tools

    @staticmethod
    def _tool_name(tool: dict[str, Any]) -> str:
        """Extract tool function name from a tool definition dict."""
        func = tool.get("function", {})
        return str(func.get("name", "")) if isinstance(func, dict) else ""

    def _normalize_tool_call(self, call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Normalize tool call: resolve aliases, fix argument names."""
        tool_name = str(call.get("name", "")).strip()
        arguments = call.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}

        # Resolve aliases
        canonical = TOOL_NAME_ALIASES.get(tool_name, tool_name)

        # Fuzzy name matching
        if canonical == tool_name:
            lowered = canonical.lower()
            if "edit" in lowered or "write" in lowered or "save" in lowered:
                canonical = "create_file"
            elif "read" in lowered or "open" in lowered or "view" in lowered:
                canonical = "read_file"
            elif "list" in lowered or lowered == "ls":
                canonical = "list_directory"
            elif "valid" in lowered or "check" in lowered:
                canonical = "validate_web_app"
            elif "plan" in lowered:
                canonical = "plan_web_build"

        # Fix argument names
        if canonical == "create_file":
            if "file_path" in arguments and "relative_path" not in arguments:
                arguments["relative_path"] = arguments["file_path"]
            rel = arguments.get("relative_path")
            if isinstance(rel, str):
                arguments["relative_path"] = self._normalize_path(rel)
            arguments.setdefault("overwrite", True)

        if canonical == "read_file":
            if "file_path" in arguments and "relative_path" not in arguments:
                arguments["relative_path"] = arguments["file_path"]
            rel = arguments.get("relative_path")
            if isinstance(rel, str):
                arguments["relative_path"] = self._normalize_path(rel)

        if canonical == "list_directory":
            rel = arguments.get("relative_path")
            if isinstance(rel, str):
                arguments["relative_path"] = self._normalize_path(rel)
            elif "relative_path" not in arguments:
                arguments["relative_path"] = "."

        if canonical == "validate_web_app":
            app_dir = arguments.get("app_dir")
            if isinstance(app_dir, str):
                arguments["app_dir"] = self._normalize_path(app_dir)

        return canonical, arguments

    def _call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool via subprocess."""
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
                "error": {"type": "InvalidJSON", "message": output[:500]},
            }
        return parsed

    def _deduplicate_tool_calls(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Remove duplicate tool calls."""
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

    # ------------------------------------------------------------------
    # UI event emitters
    # ------------------------------------------------------------------

    def _emit_reasoning(self, stage_name: str, content: str) -> None:
        """Emit reasoning text to UI via stderr."""
        clean = self._extract_clean_reasoning(content)
        if clean:
            self._emit_reasoning_raw(stage_name, clean)

    def _emit_reasoning_raw(self, stage_name: str, text: str) -> None:
        """Emit pre-cleaned reasoning text to UI — no further parsing."""
        if not text:
            return
        payload: dict[str, str] = {"content": text}
        if stage_name:
            payload["stage"] = stage_name
        print(
            f"[response:agent] {json.dumps(payload, ensure_ascii=False)}",
            file=sys.stderr,
            flush=True,
        )

    def _emit_code_block(self, filename: str, content: str) -> None:
        """Emit a file's content as a [code] reasoning event for UI display."""
        if not content.strip():
            return
        code_text = f"[code] {filename}\n{content}"
        payload = {"content": code_text, "stage": "code"}
        print(
            f"[response:agent] {json.dumps(payload, ensure_ascii=False)}",
            file=sys.stderr,
            flush=True,
        )

    def _extract_clean_reasoning(self, content: str) -> str:
        """Extract human-readable reasoning from LLM output.

        Handles:
        - Lines prefixed with 'type=reason' -> strip prefix, keep text
        - Lines prefixed with 'type=signal' -> discard (control messages)
        - JSON envelopes with type=reason -> extract text
        - ```lang code blocks -> emit separately as [code] events, replace inline
        - qwen3 <think>...</think> blocks
        """
        stripped = content.strip()
        if not stripped:
            return ""

        # Convert <think>...</think> blocks into readable reasoning
        stripped = self._format_think_tags(stripped)

        # Extract fenced code blocks and emit them separately
        stripped = self._extract_and_emit_code_blocks(stripped)

        # Strip inline type=reason / type=signal prefixes
        stripped = self._strip_type_prefixes(stripped)

        # Try line-by-line: separate JSON envelopes from plain text
        reasons: list[str] = []
        plain_text: list[str] = []

        for line in stripped.split("\n"):
            line = line.strip()
            if not line:
                plain_text.append("")
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    obj_type = str(obj.get("type", "")).lower()
                    if obj_type == "reason":
                        text = str(obj.get("text", obj.get("message", ""))).strip()
                        if text:
                            reasons.append(text)
                    elif obj_type == "signal":
                        continue
                    elif obj_type in ("control", "chat"):
                        text = str(obj.get("text", obj.get("message", ""))).strip()
                        if text:
                            reasons.append(text)
                else:
                    plain_text.append(line)
            except json.JSONDecodeError:
                plain_text.append(line)

        if reasons:
            return "\n".join(reasons)

        # Try block-level JSON parsing
        try:
            payloads = self._extract_json_payloads(stripped)
            for payload in payloads:
                self._collect_reasons(payload, reasons)
            if reasons:
                return "\n".join(reasons)
        except Exception:
            pass

        if plain_text:
            result = "\n".join(plain_text)
            result = re.sub(r"\n{3,}", "\n\n", result)
            return result.strip()
        return stripped

    # ------------------------------------------------------------------
    # LLM output cleaning helpers
    # ------------------------------------------------------------------

    _RE_TYPE_REASON = re.compile(r"^type\s*=\s*reason\s*", re.IGNORECASE)
    _RE_TYPE_SIGNAL = re.compile(r"^type\s*=\s*signal\b.*$", re.IGNORECASE)

    def _strip_type_prefixes(self, text: str) -> str:
        r"""Strip 'type=reason' prefix from lines and remove 'type=signal' lines entirely."""
        out: list[str] = []
        for line in text.split("\n"):
            stripped_line = line.strip()
            if self._RE_TYPE_SIGNAL.match(stripped_line):
                continue
            m = self._RE_TYPE_REASON.match(stripped_line)
            if m:
                remainder = stripped_line[m.end():].strip()
                if remainder:
                    out.append(remainder)
                continue
            out.append(line)
        return "\n".join(out)

    _RE_CODE_FENCE = re.compile(
        r"```(\w*)\n(.*?)```", re.DOTALL
    )

    def _extract_and_emit_code_blocks(self, text: str) -> str:
        """Extract fenced code blocks, emit them as [code] reasoning events, and
        replace them in the text with a short placeholder."""
        parts: list[str] = []
        last_end = 0
        for m in self._RE_CODE_FENCE.finditer(text):
            parts.append(text[last_end:m.start()])
            lang = m.group(1).strip().lower()
            code_body = m.group(2).strip()
            filename = self._guess_code_filename(lang, code_body)

            if lang == "json" and self._looks_like_tool_call(code_body):
                last_end = m.end()
                continue

            # Skip empty code blocks entirely
            if not code_body:
                last_end = m.end()
                continue

            label = filename or lang or "code"
            self._emit_reasoning_raw("code", f"[code] {label}\n{code_body}")
            parts.append(f"[code: {label}]")
            last_end = m.end()

        parts.append(text[last_end:])
        return "".join(parts)

    @staticmethod
    def _looks_like_tool_call(code: str) -> bool:
        """Check if a code block looks like an embedded JSON tool call."""
        try:
            obj = json.loads(code)
            if isinstance(obj, dict):
                return any(k in obj for k in ("name", "action", "tool", "tool_calls"))
        except (json.JSONDecodeError, ValueError):
            pass
        return False

    @staticmethod
    def _guess_code_filename(lang: str, code: str) -> str:
        """Try to guess a filename from the code block language or content."""
        lang_to_ext = {
            "html": "index.html",
            "css": "styles.css",
            "javascript": "script.js",
            "js": "script.js",
            "json": "",
        }
        if lang in lang_to_ext and lang_to_ext[lang]:
            return lang_to_ext[lang]
        first_line = code.split("\n", 1)[0].strip()
        if first_line.startswith("//") or first_line.startswith("/*"):
            for token in first_line.split():
                if "." in token and not token.startswith("//") and not token.startswith("/*"):
                    clean = token.strip("*/").strip()
                    if clean:
                        return clean
        return ""

    def _extract_json_payloads(self, text: str) -> list[Any]:
        """Extract all top-level JSON objects from text."""
        payloads: list[Any] = []
        decoder = json.JSONDecoder()
        index = 0
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            try:
                payload, end_index = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                break
            payloads.append(payload)
            index = end_index
        return payloads

    def _collect_reasons(self, payload: Any, reasons: list[str]) -> None:
        """Recursively collect reason text from JSON payloads."""
        if isinstance(payload, list):
            for item in payload:
                self._collect_reasons(item, reasons)
            return
        if not isinstance(payload, dict):
            return
        obj_type = str(payload.get("type", "")).lower()
        if obj_type == "reason":
            text = str(payload.get("text", payload.get("message", ""))).strip()
            if text:
                reasons.append(text)
        elif payload.get("action") == "call_tool" and isinstance(payload.get("result"), dict):
            rendered = self._format_tool_result_reasoning(
                name=str(payload.get("tool", "")).strip(),
                result=payload,
            )
            if rendered:
                reasons.append(rendered)
        for value in payload.values():
            if isinstance(value, (dict, list)):
                self._collect_reasons(value, reasons)

    def _format_tool_result_reasoning(self, *, name: str, result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return ""
        nested = result.get("result") if isinstance(result.get("result"), dict) else result
        if not isinstance(nested, dict):
            return ""

        summary = str(nested.get("summary", "")).strip()
        file_structure = nested.get("file_structure")
        features: list[str] = []
        for key in ("elements", "css_features", "js_features", "prompt_features", "phases"):
            value = nested.get(key)
            if isinstance(value, list):
                features.extend(str(item).strip() for item in value if str(item).strip())

        lines: list[str] = []
        display_name = name or str(result.get("tool", "")).strip()
        if display_name:
            lines.append(f"Tool result: {display_name}")
        if summary:
            lines.append(f"Summary: {summary}")

        if isinstance(file_structure, dict) and file_structure:
            lines.append("Planned files:")
            for rel, desc in file_structure.items():
                rel_text = str(rel).strip()
                if not rel_text:
                    continue
                desc_text = str(desc).strip()
                if desc_text:
                    lines.append(f"- {rel_text}: {desc_text}")
                else:
                    lines.append(f"- {rel_text}")

        if features:
            lines.append("Key features:")
            for feature in features[:10]:
                lines.append(f"- {feature}")

        if not lines:
            return ""
        return "\n".join(lines)

    def _extract_tool_calls_from_text(self, content: str) -> list[dict[str, Any]]:
        """Adaptively extract tool calls written as text/JSON in the LLM response."""
        calls: list[dict[str, Any]] = []
        for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", content, re.DOTALL):
            body = m.group(1).strip()
            try:
                obj = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                items = [obj]
            elif isinstance(obj, list):
                items = [x for x in obj if isinstance(x, dict)]
            else:
                continue
            for item in items:
                if "name" in item or "tool" in item:
                    name = str(item.get("name", item.get("tool", ""))).strip()
                    args = item.get("arguments", item.get("params", item.get("input", {})))
                    if name and isinstance(args, dict):
                        normalized_name, normalized_args = self._normalize_tool_call(
                            {"name": name, "arguments": args}
                        )
                        calls.append({"name": normalized_name, "arguments": normalized_args})
        return self._deduplicate_tool_calls(calls)

    def _emit_terminal_logs(self, tool_name: str, result: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            return

        # Handle top-level error (MCP server exception)
        top_error = result.get("error")
        if isinstance(top_error, dict):
            self._emit_reasoning_raw("terminal", f"tool={tool_name} ok=False")
            msg = str(top_error.get("message", "")).strip()
            etype = str(top_error.get("type", "")).strip()
            if msg:
                self._emit_reasoning_raw("terminal", f"error: {etype}: {msg}" if etype else f"error: {msg}")
            return
        elif isinstance(top_error, str) and top_error.strip():
            self._emit_reasoning_raw("terminal", f"tool={tool_name} ok=False")
            self._emit_reasoning_raw("terminal", f"error: {top_error.strip()}")
            return

        nested = result.get("result") if isinstance(result, dict) else None
        if not isinstance(nested, dict):
            return

        self._emit_reasoning_raw("terminal", f"tool={tool_name} ok={bool(nested.get('ok', False))}")
        stdout_text = str(nested.get("stdout", "")).strip()
        stderr_text = str(nested.get("stderr", "")).strip()
        if stdout_text:
            for line in stdout_text.splitlines():
                text = line.strip()
                if text:
                    self._emit_reasoning_raw("terminal", text[:500])
        if stderr_text:
            for line in stderr_text.splitlines():
                text = line.strip()
                if text:
                    self._emit_reasoning_raw("terminal", text[:500])

        missing_files = nested.get("missing_files")
        if isinstance(missing_files, list) and missing_files:
            self._emit_reasoning_raw("terminal", "missing_files: " + ", ".join(str(item) for item in missing_files))

        issues = nested.get("issues")
        if isinstance(issues, list) and issues:
            self._emit_reasoning_raw("terminal", "issues: " + " | ".join(str(item) for item in issues))

        error_payload = nested.get("error")
        if isinstance(error_payload, dict):
            message = str(error_payload.get("message", "")).strip()
            if message:
                self._emit_reasoning_raw("terminal", f"error: {message}")
        elif isinstance(error_payload, str) and error_payload.strip():
            self._emit_reasoning_raw("terminal", f"error: {error_payload.strip()}")

    def _extract_error_details(self, result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return "unknown error"

        parts: list[str] = []

        top_error = result.get("error")
        if isinstance(top_error, dict):
            msg = str(top_error.get("message", "")).strip()
            etype = str(top_error.get("type", "")).strip()
            if msg:
                parts.append(f"{etype}: {msg}" if etype else msg)
        elif isinstance(top_error, str) and top_error.strip():
            parts.append(top_error.strip())

        nested = result.get("result")
        if isinstance(nested, dict):
            stderr = str(nested.get("stderr", "")).strip()
            stdout = str(nested.get("stdout", "")).strip()
            if stderr:
                parts.append(stderr)
            if stdout:
                parts.append(stdout)

            missing_files = nested.get("missing_files")
            if isinstance(missing_files, list) and missing_files:
                parts.append("missing_files: " + ", ".join(str(item) for item in missing_files))

            issues = nested.get("issues")
            if isinstance(issues, list) and issues:
                parts.append("issues: " + " | ".join(str(item) for item in issues))

            error_payload = nested.get("error")
            if isinstance(error_payload, dict):
                message = str(error_payload.get("message", "")).strip()
                if message:
                    parts.append(message)
            elif isinstance(error_payload, str) and error_payload.strip():
                parts.append(error_payload.strip())

        return "\n".join(part for part in parts if part) or "unknown error"

    def _emit_tool_call_event(
        self, *, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        """Emit tool call event to UI via stderr."""
        safe_args: dict[str, Any] = {}
        for key, value in arguments.items():
            if key in {"content", "replacement_text"} and isinstance(value, str):
                safe_args[key] = f"<trimmed:{len(value)} chars>"
            else:
                safe_args[key] = value
        print(
            f"[tool:call] {json.dumps({'name': tool_name, 'arguments': safe_args}, ensure_ascii=False)}",
            file=sys.stderr,
            flush=True,
        )

    @staticmethod
    def _format_think_tags(text: str) -> str:
        """Convert qwen3 <think>...</think> blocks into labelled reasoning text."""
        def _replace_block(m: re.Match) -> str:
            inner = m.group(1).strip()
            if not inner:
                return ""
            return f"[thinking] {inner}\n"

        formatted = re.sub(
            r"<think>(.*?)</think>",
            _replace_block,
            text,
            flags=re.DOTALL,
        )
        formatted = re.sub(
            r"<think>(.*?)$",
            lambda m: f"[thinking] {m.group(1).strip()}\n" if m.group(1).strip() else "",
            formatted,
            flags=re.DOTALL,
        )
        return formatted.strip() if formatted.strip() else text.strip()

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """Remove qwen3 <think>...</think> blocks."""
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL).strip()
        return cleaned if cleaned else text.strip()

    # ------------------------------------------------------------------
    # Summary generation
    # ------------------------------------------------------------------

    def _generate_summary(
        self, *, task: str, tool_trace: list[dict[str, Any]]
    ) -> str:
        """Generate a final summary of changes via LLM.

        Asks the model to produce a markdown-formatted summary describing
        the features built, files created, and key implementation details.
        """
        changed_files = sorted(
            {
                str(item.get("arguments", {}).get("relative_path", "")).strip()
                for item in tool_trace
                if isinstance(item, dict)
                and str(item.get("tool", "")) == "create_file"
                and str(item.get("arguments", {}).get("relative_path", "")).strip()
            }
        )

        # Read file contents for richer summary
        file_snippets: list[str] = []
        for fname in changed_files[:6]:
            fpath = self.workspace_root_path / fname
            if fpath.is_file():
                try:
                    raw = fpath.read_text(errors="replace")[:3000]
                    file_snippets.append(f"--- {fname} ---\n{raw}")
                except Exception:
                    pass

        summary_prompt = (
            "You are summarising what was just built for the user.\n"
            "Write a clear, helpful markdown summary. Do NOT start with 'DONE:'.\n"
            "Use the following structure:\n"
            "1. A **bold one-line headline** describing what was built.\n"
            "2. A bullet list of the key **features / interactions** the user can try.\n"
            "3. A short **Files** section listing each file and a one-line description.\n\n"
            f"Original request: {task}\n\n"
            "Files created/updated:\n"
            + (
                "\n".join(f"- {f}" for f in changed_files[:20])
                if changed_files
                else "- (none)"
            )
        )
        if file_snippets:
            summary_prompt += "\n\nFile contents (for reference):\n" + "\n".join(file_snippets)

        try:
            response = self.ollama_client.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": summary_prompt}],
                tools=[],
                stream=False,
                num_ctx=_env_int("ORCHESTRATOR_AGENT_NUM_CTX", 40000),
                num_predict=1200,
            )
            msg = self.ollama_client.extract_assistant_message(response)
            text = str(msg.get("content", "")).strip()
            # Strip <think> blocks the model might add
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            if text:
                return text
        except Exception:
            pass

        # Fallback: build a basic markdown summary ourselves
        if changed_files:
            lines = [f"**Built: {task.strip()[:80]}**", ""]
            lines.append("**Files:**")
            for f in changed_files:
                lines.append(f"- {f}")
            return "\n".join(lines)
        return f"**Task completed:** {task.strip()[:120]}"

    # ------------------------------------------------------------------
    # Memory management
    # ------------------------------------------------------------------

    def _compact_memory(self, memory: SessionMemory) -> None:
        """Compact memory if it exceeds the character budget."""
        budget = _env_int("ORCHESTRATOR_MEMORY_CHAR_BUDGET", 120000)
        total = sum(len(str(m.get("content", ""))) + 50 for m in memory.messages)
        if total <= budget:
            return

        head = memory.messages[:2]
        tail_count = _env_int("ORCHESTRATOR_MEMORY_TAIL_COUNT", 16)
        tail = (
            memory.messages[-tail_count:]
            if len(memory.messages) > tail_count
            else list(memory.messages)
        )

        middle = (
            memory.messages[2:-tail_count]
            if len(memory.messages) > (2 + tail_count)
            else []
        )
        summary_lines: list[str] = []
        for item in middle[-10:]:
            role = str(item.get("role", ""))
            text = str(item.get("content", "")).strip()[:200]
            if text:
                summary_lines.append(f"- {role}: {text}")

        summary_text = "Memory compacted. Prior conversation summary:\n" + (
            "\n".join(summary_lines) if summary_lines else "- (no summary)"
        )

        memory.messages = [*head, {"role": "user", "content": summary_text}, *tail]

    # ------------------------------------------------------------------
    # Workspace helpers
    # ------------------------------------------------------------------

    def _read_created_files(
        self,
        created_files: set[str],
        *,
        extensions: set[str],
        exclude_patterns: set[str] | None = None,
        max_chars_per_file: int = 10000,
    ) -> str:
        """Read created files matching the given extensions and return their contents."""
        exclude = exclude_patterns or set()
        blocks: list[str] = []
        for rel in sorted(created_files):
            if not any(rel.endswith(ext) for ext in extensions):
                continue
            rel_lower = rel.lower()
            if any(pat in rel_lower for pat in exclude):
                continue
            file_path = self.workspace_root_path / rel
            if not file_path.is_file():
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not content.strip():
                continue
            if len(content) > max_chars_per_file:
                content = content[:max_chars_per_file] + "\n... (truncated)"
            blocks.append(f"--- {rel} ---\n{content}\n--- end {rel} ---")
        return "\n\n".join(blocks)

    def _get_relevant_file_context(self, query: str, top_k: int = 4) -> str:
        """Use ProjectMemory to retrieve semantically relevant files for a query."""
        try:
            self.project_memory.refresh()
            retrieved = self.project_memory.retrieve(query=query, top_k=top_k)
            if not retrieved:
                return ""
            return self.project_memory.build_retrieval_context(
                retrieved=retrieved,
                include_full_top_n=min(2, len(retrieved)),
                max_full_chars=8000,
            )
        except Exception:
            return ""

    def _build_workspace_manifest(self, max_files: int = 32) -> str:
        """Build a manifest of workspace files."""
        files: list[str] = []
        ignored = {
            ".git", ".venv", "venv", "node_modules", "__pycache__",
            ".low-cortisol-html-logs",
        }
        for path in sorted(self.workspace_root_path.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(self.workspace_root_path).as_posix())
            if any(
                part.startswith(".") or part in ignored
                for part in rel.split("/")
            ):
                continue
            files.append(rel)
            if len(files) >= max_files:
                break

        if not files:
            return (
                "Workspace is empty. This is a new project. "
                "All files need to be created."
            )

        return "Current workspace files:\n" + "\n".join(f"- {f}" for f in files)

    def _normalize_path(self, raw_path: str) -> str:
        """Normalize a workspace-relative path.

        Aggressively strips absolute-path prefixes that LLMs commonly emit.
        """
        candidate = raw_path.strip().replace("\\", "/")
        if not candidate:
            return "."

        if candidate.startswith("/"):
            try:
                path_obj = Path(candidate)
                resolved = path_obj.expanduser().resolve()
                relative = resolved.relative_to(self.workspace_root_path.resolve())
                candidate = str(relative)
                if not candidate or candidate == ".":
                    return "."
            except (ValueError, OSError):
                pass

        ws_name = self.workspace_root_path.name
        marker = f"/{ws_name}/"
        idx = candidate.find(marker)
        if idx != -1:
            candidate = candidate[idx + len(marker):]

        marker_end = f"/{ws_name}"
        if candidate.endswith(marker_end) or candidate == ws_name:
            return "."

        if candidate.startswith(f"{ws_name}/"):
            candidate = candidate[len(ws_name) + 1:]

        candidate = candidate.lstrip("/")
        while candidate.startswith("./"):
            candidate = candidate[2:]
        candidate = candidate.rstrip("/")

        return candidate or "."

    def _as_chat_envelope(self, text: str) -> str:
        """Wrap text in a chat JSON envelope."""
        return json.dumps(
            {"type": "chat", "text": text.strip()}, ensure_ascii=False
        )
