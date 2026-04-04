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
    "find_files": "search_files",
    "glob_files": "search_files",
    "search_workspace": "search_files",
    "search_file": "search_files",
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
    "feature_plan": ["plan_web_build", "read_file", "list_directory", "search_files"],
    "html_code": ["create_file", "read_file", "search_files"],
    "js_code": ["create_file", "read_file", "search_files"],
    "css_code": ["create_file", "read_file", "search_files"],
    "test_code": ["create_file", "read_file", "run_unit_tests", "search_files"],
}

# Max iterations for the test feedback loop
TEST_STAGE_MAX_ITERATIONS = 5

# Per-stage max ReAct turns (overridable via ORCHESTRATOR_REACT_MAX_ITERS_<STAGE>)
REACT_STAGE_MAX_ITERATIONS: dict[str, int] = {
    "feature_plan": 4,
    "html_code": 5,
    "js_code": 6,
    "css_code": 5,
}

# Expected primary output file per coding stage — used as a stop signal
STAGE_PRIMARY_FILE: dict[str, str] = {
    "html_code": "index.html",
    "js_code": "script.js",
    "css_code": "styles.css",
}

# Context window budgets for qwen3.5's 256k token window.
# Conservative estimate: ~3 chars per token for mixed code + prose.
# Soft = 200k tokens (normal ceiling). Hard = 250k tokens (emergency — still fits 256k with 6k margin).
CONTEXT_SOFT_BUDGET_CHARS = 600_000   # ~200k tokens — normal compaction trigger
CONTEXT_HARD_BUDGET_CHARS = 750_000   # ~250k tokens — emergency slim trigger

SYSTEM_PROMPT = """\
==================== PRIMACY (READ FIRST) ====================

## [ROLE]
You are an expert frontend coding agent specialising in HTML, CSS, and vanilla JavaScript.
You build complete, working single-page web apps. You are methodical, precise, and
follow skill guides exactly. You never guess at IDs, class names, or file structures.

## [CORE RULES]
- Use RELATIVE paths only: 'index.html', 'styles.css', 'script.js'. Never absolute.
- Write COMPLETE files with create_file — never partial snippets or placeholders.
- Do NOT hallucinate element IDs, class names, or function names. Use only what is defined.
- Read PLAN.md first when it exists — it has cross-file references and build status.
- Keep reasoning concise and in plain text. No JSON wrappers, no type=reason prefixes.
- Use only the tools provided for each stage.

## [REASONING STRATEGY — ReAct]
- Each stage runs multiple turns. Plan → explore → act.
- Turn 1: reason about the task. If a PLAN.md exists, read it. Explore sparingly.
- Later turns: build on tool results. Call create_file when you have what you need.
- Do NOT re-read files already visible in this conversation — they are in context.
- If a tool returns an error: adapt. Do not repeat the same failing call.
- Signal stage done by returning plain text only (zero tool calls).

## [OUTPUT FORMAT]
- Reasoning: plain text, concise. One create_file call per stage.
- File content: complete — every line, every tag, every function, no TODO stubs.
- After writing the file: short plain-text confirmation. No further tool calls.

## [GLOBAL CONSTRAINTS — Cross-File Class Contract]
- HTML is the single source of truth for all element IDs and class names.
- JS queries ONLY IDs that exist in the HTML.
- CSS styles ONLY classes from HTML and JS dynamic element creation.
- ONLY three visibility toggle classes are allowed: hidden · active · disabled
  BANNED: is-open  is-hidden  is-visible  show  visible  open  closed  is-active
- Modals: start with class='hidden' in HTML. JS calls classList.remove('hidden') to show.
- CSS MUST include: .hidden { display: none !important; }
- All three files must agree on every class name. Zero mismatches.

==================== END PRIMACY ====================
"""


# Recency zone is built per-stage by LoopController._build_recency_zone().


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _react_max_iters(stage_name: str) -> int:
    """Return max ReAct iterations for a stage; overridable via env var."""
    env_key = f"ORCHESTRATOR_REACT_MAX_ITERS_{stage_name.upper()}"
    return _env_int(env_key, REACT_STAGE_MAX_ITERATIONS.get(stage_name, 4))


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

        # Runtime state — populated at the start of run() and updated during stages
        self._pipeline_task: str = ""
        self._plan_html_refs: dict[str, Any] = {}
        self._plan_js_classes: list[str] = []

        # Chat history tracking for CHAT.md compression
        self._stage_summaries: list[dict[str, Any]] = []
        self._current_stage_info: dict[str, Any] = {}

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
        self._pipeline_task = task
        self._plan_html_refs = {}
        self._plan_js_classes = []
        self._stage_summaries = []
        self._current_stage_info = {}

        memory = SessionMemory()
        memory.add("system", SYSTEM_PROMPT)
        memory.add("user", f"Task: {task}")

        tool_trace: list[dict[str, Any]] = []
        iteration = 0
        created_files: set[str] = set()
        max_tool_calls = _env_int("ORCHESTRATOR_MAX_TOOL_CALLS_PER_ITERATION", 12)

        # --- Load skill files ---
        skill_texts: dict[str, str] = {}
        for skill_name in ("html", "js", "css", "test"):
            skill_path = self.project_root / "skills" / f"{skill_name}.md"
            try:
                skill_texts[skill_name] = skill_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skill_texts[skill_name] = ""

        # --- Inject context efficiency skill (once at session start) ---
        context_skill_path = self.project_root / "skills" / "context.md"
        try:
            context_skill = context_skill_path.read_text(encoding="utf-8", errors="replace")
            if context_skill.strip():
                memory.add("user", f"[Context Efficiency Guide]\n{context_skill}")
        except OSError:
            pass

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
            self._current_stage_info = {
                "stage": stage_name,
                "nudges": 0,
                "errors": [],
                "primary_written": False,
            }

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

            # --- ReAct loop for this stage ---
            general_plan_text, created_files = self._run_react_stage(
                stage_name=stage_name,
                task=task,
                memory=memory,
                tool_trace=tool_trace,
                created_files=created_files,
                stage_tools=stage_tools,
                num_predict=num_predict,
                max_tool_calls=max_tool_calls,
                base_iteration=iteration,
                general_plan_text=general_plan_text,
            )
            # Record stage outcome in CHAT.md
            primary_file = STAGE_PRIMARY_FILE.get(stage_name)
            self._current_stage_info["primary_written"] = bool(
                primary_file and primary_file in created_files
            )
            if stage_name == "feature_plan":
                snippet = general_plan_text.strip().split("\n")[0][:200] if general_plan_text else ""
                self._current_stage_info["reasoning_summary"] = snippet
            self._stage_summaries.append(dict(self._current_stage_info))
            self._write_chat_md(created_files)

        # --- Run test stage (write tests, run, fix, loop until passing) ---
        iteration = self._run_test_stage(
            task=task,
            memory=memory,
            tool_trace=tool_trace,
            created_files=created_files,
            skill_texts=skill_texts,
            iteration=iteration,
            max_tool_calls=max_tool_calls,
        )

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

        # test_code is handled by _run_test_stage, not this builder

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Feature plan prompts
    # ------------------------------------------------------------------
    # Recency zone builder
    # ------------------------------------------------------------------

    def _build_recency_zone(self, *, stage_name: str) -> list[str]:
        """Build the RECENCY zone appended at the END of each coding stage prompt.

        Position-aware design: the most critical per-stage rules live here, at the
        bottom of the context window where attention recall is highest (85–95%).
        Follows: Current Task → Focus → Constraints → Action → Format → Checklist.
        """
        primary_file = STAGE_PRIMARY_FILE.get(stage_name, "output file")
        lines = [
            "",
            "==================== RECENCY (READ LAST) ====================",
            "",
            "## [CURRENT TASK]",
            f"Stage: {stage_name}",
            f"Deliverable: `{primary_file}` — write the COMPLETE file with create_file.",
            "",
            "## [FOCUS INSTRUCTIONS]",
            "- Call read_file 'PLAN.md' if you need cross-file references (IDs, classes, build status).",
            "- Use the skill guide above for required patterns and anti-patterns.",
            "- Do NOT re-read files whose content is already visible in this conversation.",
        ]

        if stage_name == "html_code":
            lines += [
                "- Add/Create button MUST be in a persistent header — not only in empty-state sections.",
                "- Modals: id='...' class='modal-overlay hidden' (starts hidden).",
            ]
        elif stage_name == "js_code":
            lines += [
                "- PLAN.md > HTML Element Reference has every ID your JS needs — use it.",
                "- Track edit state with a module-level variable: let currentEditId = null;",
            ]
        elif stage_name == "css_code":
            lines += [
                "- PLAN.md > JS Dynamic Classes lists every dynamic class you must style.",
                "- PLAN.md > HTML Element Reference lists selectors to target.",
            ]

        lines += [
            "",
            "## [CONSTRAINTS REPEATED]",
            "- Visibility toggles: ONLY `hidden` / `active` / `disabled`. Nothing else.",
            "- No inline styles. No inventing new class names not in the HTML.",
        ]

        if stage_name == "css_code":
            lines += [
                "- MUST define: .hidden { display: none !important; }",
                "- MUST style every class listed under PLAN.md > JS Dynamic Classes.",
            ]
        elif stage_name == "js_code":
            lines += [
                "- Only use hidden/active/disabled for classList toggling.",
                "- escapeHtml() for innerHTML only — NOT for input.value or textarea.value.",
            ]

        lines += [
            "",
            "## [ACTION INSTRUCTIONS]",
            "1. Read PLAN.md if you need element IDs or class references (call read_file 'PLAN.md').",
            f"2. Call create_file with relative_path='{primary_file}' and the COMPLETE file content.",
            "3. Return plain text only after writing — this signals end of stage.",
            "",
            "## [OUTPUT FORMAT REPEATED]",
            f"- Exactly ONE create_file call with the complete {primary_file}.",
            "- Short plain-text confirmation afterward. No more tool calls.",
            "",
            "## [CHECKLIST]",
            "- [ ] File is complete — no TODO stubs, no partial snippets",
            "- [ ] All IDs and classes match what is in the HTML exactly",
            "- [ ] Only hidden / active / disabled used for visibility toggling",
        ]

        if stage_name == "css_code":
            lines += [
                "- [ ] .hidden { display: none !important; } is defined",
                "- [ ] Every JS dynamic class has a corresponding CSS rule",
            ]
        elif stage_name == "js_code":
            lines += [
                "- [ ] currentEditId module-level variable used for edit state tracking",
                "- [ ] escapeHtml() only on innerHTML, never on input/textarea values",
            ]
        elif stage_name == "html_code":
            lines += [
                "- [ ] Persistent Add/Create button exists in the header",
                "- [ ] Modals have class='modal-overlay hidden'",
                "- [ ] Every interactive element has a unique descriptive id",
            ]

        lines += ["", "==================== END RECENCY ===================="]
        return lines

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
        ])
        lines.extend(self._build_recency_zone(stage_name="html_code"))
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
        ])
        lines.extend(self._build_recency_zone(stage_name="js_code"))
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
        ])
        lines.extend(self._build_recency_zone(stage_name="css_code"))
        return lines

    # ------------------------------------------------------------------
    # Test stage — multi-turn feedback loop
    # ------------------------------------------------------------------

    def _run_test_stage(
        self,
        *,
        task: str,
        memory: SessionMemory,
        tool_trace: list[dict[str, Any]],
        created_files: set[str],
        skill_texts: dict[str, str],
        iteration: int,
        max_tool_calls: int,
    ) -> int:
        """Write tests, run them, fix errors, repeat until all pass or max iterations."""
        stage_name = "test_code"
        stage_tools = self._get_pruned_tools(
            query="Write and run unit tests for JavaScript logic",
            stage_name=stage_name,
        )

        # Read the full script.js so the model has complete context
        js_content = self._read_workspace_file("script.js")
        html_content = self._read_workspace_file("index.html")

        last_test_result: dict[str, Any] | None = None
        tests_passed = False

        for test_iter in range(TEST_STAGE_MAX_ITERATIONS):
            iteration += 1
            iter_label = f"test_code (attempt {test_iter + 1}/{TEST_STAGE_MAX_ITERATIONS})"
            print(f"[status:agent] stage: {iter_label}", file=sys.stderr, flush=True)
            self._emit_reasoning(stage_name, f"Starting test iteration {test_iter + 1}")

            prompt = self._build_test_stage_prompt(
                task=task,
                js_content=js_content,
                html_content=html_content,
                skill_text=skill_texts.get("test", ""),
                last_test_result=last_test_result,
                test_iter=test_iter,
                created_files=created_files,
            )
            memory.add("user", prompt)

            num_predict = _env_int("ORCHESTRATOR_CODE_NUM_PREDICT", 16384)
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
                    self._emit_reasoning(stage_name, "Malformed XML, retrying without tools...")
                    try:
                        response = self.ollama_client.chat(
                            model=self.model_name,
                            messages=memory.messages,
                            tools=[],
                            stream=False,
                            num_ctx=_env_int("ORCHESTRATOR_AGENT_NUM_CTX", 32768),
                            num_predict=num_predict,
                        )
                        message = self.ollama_client.extract_assistant_message(response)
                        content = str(message.get("content", ""))
                        tool_calls = self._extract_tool_calls_from_text(content)
                    except RuntimeError:
                        self._emit_reasoning(stage_name, "Retry failed. Ending test stage.")
                        memory.add("assistant", content if "content" in dir() else "", tool_calls=[])  # type: ignore[possibly-undefined]
                        break
                elif "HTTP error 500" in err_msg or "Internal Server Error" in err_msg:
                    self._emit_reasoning(stage_name, "Server error (500), retrying with reduced context...")
                    system_msgs = [m for m in memory.messages if m.get("role") == "system"]
                    recent_msgs = [m for m in memory.messages if m.get("role") != "system"][-4:]
                    try:
                        response = self.ollama_client.chat(
                            model=self.model_name,
                            messages=system_msgs + recent_msgs,
                            tools=stage_tools,
                            stream=False,
                            num_predict=num_predict,
                        )
                        message = self.ollama_client.extract_assistant_message(response)
                        content = str(message.get("content", ""))
                        tool_calls = self.ollama_client.extract_tool_calls(message)
                    except RuntimeError:
                        self._emit_reasoning(stage_name, "Retry also failed. Ending test stage.")
                        break
                else:
                    raise

            # Normalize and deduplicate
            tool_calls = [
                {"name": n, "arguments": a}
                for n, a in (self._normalize_tool_call(tc) for tc in tool_calls)
            ]
            tool_calls = self._deduplicate_tool_calls(tool_calls)
            if not tool_calls and content.strip():
                tool_calls = self._extract_tool_calls_from_text(content)

            if content.strip():
                self._emit_reasoning(stage_name, content)

            # Execute tools; capture run_unit_tests result
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

                if name == "create_file":
                    rel = str(args.get("relative_path", "")).strip()
                    file_content_val = str(args.get("content", ""))
                    if rel and file_content_val.strip():
                        self._emit_code_block(rel, file_content_val)

                if name == "run_unit_tests":
                    last_test_result = result
                    nested = result.get("result") if isinstance(result, dict) else result
                    if isinstance(nested, dict) and nested.get("ok"):
                        stdout = str(nested.get("stdout", ""))
                        stderr_out = str(nested.get("stderr", ""))
                        # Tests pass when Node exits cleanly with no error output
                        if nested.get("exit_code", 1) == 0 and not stderr_out.strip():
                            tests_passed = True
                            self._emit_reasoning(stage_name, "All tests passed.")

                    # Re-read script.js in case the model just updated it
                    js_content = self._read_workspace_file("script.js")

                tool_trace.append({
                    "iteration": iteration,
                    "stage": stage_name,
                    "tool": name,
                    "arguments": args,
                    "result": result,
                })
                memory.add("tool", json.dumps(result), name=name)
                executed_count += 1

                if name == "create_file":
                    rel = str(args.get("relative_path", "")).strip()
                    nested = result.get("result") if isinstance(result, dict) else None
                    if rel and isinstance(nested, dict) and nested.get("ok", False):
                        created_files.add(rel)
                        self.project_memory.mark_touched(rel)

            memory.add("assistant", content, tool_calls=tool_calls)
            self._compact_memory(memory)

            if tests_passed:
                self._emit_reasoning(stage_name, "Test stage complete — all tests passing.")
                break

            # If model described a plan but called no tools, nudge it to act
            if not tool_calls:
                memory.add(
                    "user",
                    "[test_code] You output a description but called no tools. "
                    "You MUST call create_file to write tests.js (and script.js if needed), "
                    "then call run_unit_tests to execute them. Do not describe — act.",
                )

            if test_iter < TEST_STAGE_MAX_ITERATIONS - 1 and last_test_result is not None:
                self._emit_reasoning(
                    stage_name,
                    f"Tests not yet passing. Starting correction iteration {test_iter + 2}...",
                )

        if not tests_passed:
            self._emit_reasoning(
                stage_name,
                f"Test stage finished after {TEST_STAGE_MAX_ITERATIONS} iterations. "
                "Some tests may still be failing.",
            )

        return iteration

    def _read_workspace_file(self, relative_path: str) -> str:
        """Read a file from the workspace root; return empty string if missing."""
        try:
            target = Path(self.workspace_root) / relative_path
            if target.is_file():
                return target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return ""

    def _build_test_stage_prompt(
        self,
        *,
        task: str,
        js_content: str,
        html_content: str,
        skill_text: str,
        last_test_result: dict[str, Any] | None,
        test_iter: int,
        created_files: set[str],
    ) -> str:
        lines: list[str] = ["=== STAGE: test_code ===", ""]

        if created_files:
            lines.append("Known files: " + ", ".join(sorted(created_files)))
            lines.append("")

        if skill_text:
            lines.extend([
                "=== TEST SKILL GUIDE ===",
                skill_text,
                "=== END TEST SKILL GUIDE ===",
                "",
            ])

        lines.extend([
            "=== ORIGINAL TASK ===",
            task,
            "=== END TASK ===",
            "",
        ])

        if html_content:
            lines.extend([
                "=== COMPLETED index.html ===",
                html_content,
                "=== END index.html ===",
                "",
            ])

        if js_content:
            lines.extend([
                "=== COMPLETE script.js (full context) ===",
                js_content,
                "=== END script.js ===",
                "",
            ])

        if test_iter == 0:
            # First iteration: instruct model to write tests
            lines.extend([
                "TASK: Write a JavaScript unit test file called 'tests.js'.",
                "",
                "INSTRUCTIONS:",
                "1. Use create_file to write 'tests.js' at the workspace root.",
                "2. The file must test the core JavaScript logic in script.js.",
                "3. Use Node.js built-in assert module (require('assert')).",
                "4. Each test must be a plain function call — no test framework needed.",
                "5. Extract and test pure functions from script.js where possible.",
                "   If functions aren't exported, you may redefine the relevant logic inline.",
                "6. After writing the file, call run_unit_tests with test_file='tests.js'.",
                "7. You have up to " + str(TEST_STAGE_MAX_ITERATIONS) + " iterations to get all tests passing.",
                "",
                "TEST FILE FORMAT:",
                "```",
                "const assert = require('assert');",
                "",
                "// -- test helpers or extracted logic --",
                "",
                "// Test 1",
                "assert.strictEqual(someFunction(input), expectedOutput, 'test description');",
                "",
                "// Test 2",
                "assert.ok(anotherCheck, 'another test');",
                "",
                "console.log('All tests passed');",
                "```",
                "",
                "IMPORTANT:",
                "- The file name MUST be 'tests.js' (matches the run_unit_tests validator).",
                "- Do NOT test DOM/browser APIs — Node.js has no DOM.",
                "  Only test pure logic functions (formatters, calculators, state helpers, etc.).",
                "- Use search_files if you need to confirm file names in the workspace.",
            ])
        else:
            # Subsequent iterations: show previous result and ask for fixes
            if last_test_result is not None:
                nested = last_test_result.get("result") if isinstance(last_test_result, dict) else last_test_result
                if isinstance(nested, dict):
                    stdout = str(nested.get("stdout", "")).strip()
                    stderr_out = str(nested.get("stderr", "")).strip()
                    exit_code = nested.get("exit_code", "?")
                    lines.extend([
                        "=== LAST TEST RUN RESULT ===",
                        f"Exit code: {exit_code}",
                    ])
                    if stdout:
                        lines.extend(["stdout:", stdout])
                    if stderr_out:
                        lines.extend(["stderr:", stderr_out])
                    lines.extend(["=== END TEST RESULT ===", ""])

            lines.extend([
                "TASK: Fix the failing tests.",
                "",
                "INSTRUCTIONS:",
                "1. Read the test output above carefully.",
                "2. Identify which assertion failed and why.",
                "3. You may:",
                "   a) Fix tests.js if the test expectations are wrong, OR",
                "   b) Fix script.js if the application logic is wrong.",
                "   c) Fix both if needed.",
                "4. Use create_file to rewrite the corrected file(s).",
                "5. Then call run_unit_tests with test_file='tests.js' to verify.",
                "",
                "Do NOT just tweak assertions to make them trivially pass — fix the real logic.",
                "Use search_files or read_file if you need to inspect the workspace.",
            ])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # ReAct agent loop helpers
    # ------------------------------------------------------------------

    def _single_react_turn(
        self,
        *,
        stage_name: str,
        memory: SessionMemory,
        stage_tools: list[dict[str, Any]],
        num_predict: int,
        is_code_stage: bool,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Make one LLM call with XML-error retry and inline text extraction.

        Returns (content, tool_calls) — normalized and deduplicated.
        On unrecoverable error returns ("", []).
        """
        slim_messages = self._slim_context_for_call(memory)
        try:
            print("[status:agent] calling model...", file=sys.stderr, flush=True)
            response = self.ollama_client.chat(
                model=self.model_name,
                messages=slim_messages,
                tools=stage_tools,
                stream=False,
                num_ctx=_env_int("ORCHESTRATOR_AGENT_NUM_CTX", 200000),
                num_predict=num_predict,
            )
            message = self.ollama_client.extract_assistant_message(response)
            content = str(message.get("content", ""))
            tool_calls = self.ollama_client.extract_tool_calls(message)
        except RuntimeError as err:
            err_msg = str(err)
            if "XML syntax error" in err_msg or "unexpected end element" in err_msg:
                self._emit_reasoning(stage_name, "Model returned malformed XML, retrying without tools...")
                self._current_stage_info.setdefault("errors", []).append("XML parse error — retried without tools")
                try:
                    response = self.ollama_client.chat(
                        model=self.model_name,
                        messages=slim_messages,
                        tools=[],
                        stream=False,
                        num_ctx=_env_int("ORCHESTRATOR_AGENT_NUM_CTX", 200000),
                        num_predict=num_predict,
                    )
                    message = self.ollama_client.extract_assistant_message(response)
                    content = str(message.get("content", ""))
                    tool_calls = []
                    if content.strip() and is_code_stage:
                        tool_calls = self._extract_tool_calls_from_text(content)
                except RuntimeError:
                    self._emit_reasoning(stage_name, "Retry also failed. Returning empty turn.")
                    return "", []
            elif "HTTP error 500" in err_msg or "Internal Server Error" in err_msg:
                # Server-side crash — often caused by context overflow. Retry with
                # a truncated message list (system + last 6 messages) and no ctx override.
                self._emit_reasoning(stage_name, "Server error (500), retrying with reduced context...")
                self._current_stage_info.setdefault("errors", []).append("HTTP 500 — retried with reduced context")
                system_msgs = [m for m in memory.messages if m.get("role") == "system"]
                recent_msgs = [m for m in memory.messages if m.get("role") != "system"][-6:]
                try:
                    response = self.ollama_client.chat(
                        model=self.model_name,
                        messages=system_msgs + recent_msgs,
                        tools=stage_tools,
                        stream=False,
                        num_predict=num_predict,
                    )
                    message = self.ollama_client.extract_assistant_message(response)
                    content = str(message.get("content", ""))
                    tool_calls = self.ollama_client.extract_tool_calls(message)
                except RuntimeError:
                    self._emit_reasoning(stage_name, "Retry also failed. Returning empty turn.")
                    return "", []
            else:
                raise

        # Normalize and deduplicate
        tool_calls = [
            {"name": n, "arguments": a}
            for n, a in (self._normalize_tool_call(tc) for tc in tool_calls)
        ]
        tool_calls = self._deduplicate_tool_calls(tool_calls)

        # Adaptive: extract tool calls written inline in text
        if not tool_calls and content.strip() and is_code_stage:
            inline_calls = self._extract_tool_calls_from_text(content)
            if inline_calls:
                tool_calls = inline_calls
                self._emit_reasoning(
                    stage_name,
                    f"Extracted {len(inline_calls)} tool call(s) from model text output",
                )

        return content, tool_calls

    def _run_react_stage(
        self,
        *,
        stage_name: str,
        task: str,
        memory: SessionMemory,
        tool_trace: list[dict[str, Any]],
        created_files: set[str],
        stage_tools: list[dict[str, Any]],
        num_predict: int,
        max_tool_calls: int,
        base_iteration: int,
        general_plan_text: str,
    ) -> tuple[str, set[str]]:
        """Bounded multi-turn ReAct loop for one pipeline stage.

        Returns (updated_general_plan_text, updated_created_files).
        """
        is_code_stage = stage_name.endswith("_code")
        max_iters = _react_max_iters(stage_name)
        primary_file = STAGE_PRIMARY_FILE.get(stage_name)
        allowed = set(STAGE_TOOLS.get(stage_name, []))
        primary_written = False

        for react_iter in range(max_iters):
            turn_label = f"turn {react_iter + 1}/{max_iters}"
            print(f"[status:agent] {stage_name} ({turn_label})", file=sys.stderr, flush=True)

            content, tool_calls = self._single_react_turn(
                stage_name=stage_name,
                memory=memory,
                stage_tools=stage_tools,
                num_predict=num_predict,
                is_code_stage=is_code_stage,
            )

            # Empty response — nudge and retry on next iteration
            if not content.strip() and not tool_calls:
                self._emit_reasoning(stage_name, f"Empty response on {turn_label}, nudging...")
                nudge = (
                    f"[Stage {stage_name}, {turn_label}] You produced no output and called no tools. "
                    "Please continue: reason about the task, call tools to explore the workspace if needed, "
                    "or write the required output file."
                )
                memory.add("user", nudge)
                self._current_stage_info["nudges"] = self._current_stage_info.get("nudges", 0) + 1
                continue

            # Emit reasoning
            if content.strip():
                self._emit_reasoning(stage_name, content)
            elif tool_calls and is_code_stage:
                file_names = [
                    str(tc.get("arguments", {}).get("relative_path", "")).strip()
                    for tc in tool_calls
                    if tc.get("name") == "create_file"
                ]
                if file_names := [f for f in file_names if f]:
                    self._emit_reasoning(stage_name, f"Writing files: {', '.join(file_names)}")

            # Capture general plan text from feature_plan content
            if stage_name == "feature_plan":
                clean = self._strip_think_tags(content).strip()
                if clean:
                    general_plan_text = clean

            # Execute tool calls
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

                # Skip empty create_file calls
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

                if name == "create_file":
                    rel = str(args.get("relative_path", "")).strip()
                    file_content_val = str(args.get("content", ""))
                    if rel and file_content_val.strip():
                        self._emit_code_block(rel, file_content_val)

                tool_trace.append({
                    "iteration": base_iteration,
                    "stage": stage_name,
                    "tool": name,
                    "arguments": args,
                    "result": result,
                })
                memory.add("tool", json.dumps(result), name=name)
                executed_count += 1

                # Track created files and check primary output stop condition
                if name == "create_file":
                    rel = str(args.get("relative_path", "")).strip()
                    nested = result.get("result") if isinstance(result, dict) else None
                    if rel and isinstance(nested, dict) and nested.get("ok", False):
                        created_files.add(rel)
                        self.project_memory.mark_touched(rel)
                        if primary_file and rel == primary_file:
                            primary_written = True
                            # Extract cross-file references and update PLAN.md
                            if rel == "index.html":
                                written_content = str(args.get("content", ""))
                                self._plan_html_refs = self._extract_html_refs(written_content)
                                self._write_plan_md(general_plan_text, created_files)
                            elif rel == "script.js":
                                written_content = str(args.get("content", ""))
                                self._plan_js_classes = self._extract_js_classes(written_content)
                                self._write_plan_md(general_plan_text, created_files)
                            elif rel == "styles.css":
                                self._write_plan_md(general_plan_text, created_files)

                # feature_plan done when plan_web_build is called
                if stage_name == "feature_plan" and name == "plan_web_build":
                    # Capture plan summary from tool result if available
                    nested = result.get("result") if isinstance(result, dict) else {}
                    if isinstance(nested, dict):
                        summary = str(nested.get("summary", "")).strip()
                        if summary and not general_plan_text:
                            general_plan_text = summary
                    # Write initial PLAN.md with features and build status
                    self._write_plan_md(general_plan_text, created_files)
                    memory.add("assistant", content, tool_calls=tool_calls)
                    self._compact_memory(memory)
                    return general_plan_text, created_files

            memory.add("assistant", content, tool_calls=tool_calls)
            self._compact_memory(memory)

            # Stop: primary file written
            if primary_written:
                self._emit_reasoning(
                    stage_name, f"Stage complete — {primary_file} written."
                )
                break

            # Stop: model returned text but no tools
            if not tool_calls:
                if is_code_stage and not primary_written:
                    # Model planned but didn't write the file — nudge it to act
                    self._emit_reasoning(
                        stage_name,
                        f"You described your plan but did not call create_file. "
                        f"Now call create_file with relative_path='{primary_file}' "
                        f"and the COMPLETE file content. Do not describe it again — write it.",
                    )
                    memory.add(
                        "user",
                        f"[{stage_name}] You output a plan but did not call any tools. "
                        f"You MUST now call create_file with relative_path='{primary_file}' "
                        f"and the full file content. Do not output JSON or descriptions — "
                        f"call the tool.",
                    )
                    self._current_stage_info["nudges"] = self._current_stage_info.get("nudges", 0) + 1
                    continue
                break

        if is_code_stage and not primary_written:
            self._emit_reasoning(
                stage_name,
                f"Warning: {stage_name} exhausted {max_iters} turns without writing {primary_file}.",
            )

        return general_plan_text, created_files

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
        """Compact memory if it exceeds the character budget.

        Structured extraction preserves meaningful decision state:
        - Which files were created and which tools were called
        - Key reasoning snippets (first line of each assistant turn)
        - Stage prompt labels (not full content)
        """
        budget = _env_int("ORCHESTRATOR_MEMORY_CHAR_BUDGET", CONTEXT_SOFT_BUDGET_CHARS)
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
        if not middle:
            return

        files_created: list[str] = []
        tool_calls_summary: list[str] = []
        reasoning_snippets: list[str] = []

        for msg in middle:
            role = msg.get("role", "")
            content = str(msg.get("content") or "")
            tool_calls = msg.get("tool_calls") or []

            if role == "assistant":
                if tool_calls:
                    names: list[str] = []
                    for tc in tool_calls:
                        name = tc.get("name") or tc.get("function", {}).get("name", "?")
                        args = tc.get("arguments") or tc.get("function", {}).get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {}
                        path = args.get("relative_path", "") if isinstance(args, dict) else ""
                        names.append(f"{name}({path})" if path else name)
                    tool_calls_summary.append("called: " + ", ".join(names))
                first_line = next((ln.strip() for ln in content.split("\n") if ln.strip()), "")
                if first_line:
                    reasoning_snippets.append(first_line[:200])

            elif role == "tool":
                m = re.search(r'["\']?([a-zA-Z0-9_\-]+\.[a-z]{2,4})["\']?', content)
                if m and "created" in content.lower():
                    files_created.append(m.group(1))

            elif role == "user" and len(content) > 500:
                first_line = content.split("\n")[0][:120]
                reasoning_snippets.append(f"[stage prompt] {first_line}")

        lines: list[str] = ["[Context compacted — prior conversation summary]"]
        if files_created:
            lines.append("Files created so far: " + ", ".join(dict.fromkeys(files_created)))
        if tool_calls_summary:
            lines.append("Tool calls made:")
            for s in tool_calls_summary[-8:]:
                lines.append(f"  - {s}")
        if reasoning_snippets:
            lines.append("Key reasoning steps:")
            for s in reasoning_snippets[-6:]:
                lines.append(f"  - {s}")
        lines.append(f"({len(middle)} messages summarized above)")

        memory.messages = [*head, {"role": "user", "content": "\n".join(lines)}, *tail]

    # ------------------------------------------------------------------
    # Context slimming — keeps full memory object, returns trimmed call list
    # ------------------------------------------------------------------

    def _slim_context_for_call(self, memory: SessionMemory) -> list[dict[str, Any]]:
        """Return a trimmed message list for one LLM call, based on token estimates.

        - Under soft budget (~200k tokens): pass full memory unchanged.
        - Soft–hard range (200k–250k tokens): system + PLAN.md + last 12 messages.
        - Over hard budget (>250k tokens): system + PLAN.md + last 6 messages (emergency).

        The memory object itself is NOT modified — this only affects what is sent to the API.
        """
        total_chars = sum(len(str(m.get("content", ""))) for m in memory.messages)

        if total_chars <= CONTEXT_SOFT_BUDGET_CHARS:
            return memory.messages

        # Over soft budget — slim
        keep_tail = 12 if total_chars <= CONTEXT_HARD_BUDGET_CHARS else 6
        level = "slim" if total_chars <= CONTEXT_HARD_BUDGET_CHARS else "emergency slim"

        system_msgs = [m for m in memory.messages if m.get("role") == "system"]
        tail = memory.messages[-keep_tail:]

        plan_content = self._read_plan_md()
        chat_content = self._read_chat_md()
        offload_msgs: list[dict[str, Any]] = []
        if chat_content.strip():
            offload_msgs.append({
                "role": "user",
                "content": "[CHAT.md — compressed conversation history]\n" + chat_content,
            })
        if plan_content.strip():
            offload_msgs.append({
                "role": "user",
                "content": "[PLAN.md — project reference, element IDs, class names, build status]\n" + plan_content,
            })

        estimated_tokens = total_chars // 3
        self._emit_reasoning_raw(
            "system",
            f"Context {level}: {estimated_tokens:,} tokens estimated "
            f"(budget 200k). Keeping CHAT.md + PLAN.md + last {keep_tail} messages.",
        )
        return system_msgs + offload_msgs + tail

    # ------------------------------------------------------------------
    # PLAN.md — workspace-level project reference file
    # ------------------------------------------------------------------

    def _plan_md_path(self) -> Path:
        return self.workspace_root_path / "PLAN.md"

    # ------------------------------------------------------------------
    # CHAT.md — compressed conversation history offload
    # ------------------------------------------------------------------

    def _chat_md_path(self) -> Path:
        return self.workspace_root_path / "CHAT.md"

    def _write_chat_md(self, created_files: set[str]) -> None:
        """Write a compressed, structured conversation history to CHAT.md.

        Sections match the context-zone structure:
        - Working memory: per-stage outcomes with primary file status
        - Errors and nudges: what went wrong and how it was handled
        - Known issues: stages that exhausted turns without writing output

        This file is read back by _slim_context_for_call when context grows
        beyond the soft budget, giving the model a concise history without
        full message list replay.
        """
        lines: list[str] = [
            "# CHAT.md — Compressed Conversation History\n",
            f"Task: {self._pipeline_task}\n",
        ]

        if self._stage_summaries:
            lines.append("## [WORKING MEMORY — Stage Outcomes]")
            for s in self._stage_summaries:
                stage = s.get("stage", "?")
                primary_file = STAGE_PRIMARY_FILE.get(stage)
                written = s.get("primary_written", False)
                nudges = s.get("nudges", 0)
                errors = s.get("errors", [])
                reasoning = s.get("reasoning_summary", "")

                status = "DONE" if (written or stage == "feature_plan") else "INCOMPLETE"
                file_note = f" → `{primary_file}` written" if written else (
                    f" → `{primary_file}` NOT written" if primary_file else ""
                )
                line = f"- **{stage}**: {status}{file_note}"
                if nudges:
                    line += f" ({nudges} nudge{'s' if nudges > 1 else ''})"
                lines.append(line)

                if reasoning:
                    lines.append(f"  - Plan: {reasoning[:200]}")
                for err in errors:
                    lines.append(f"  - Error handled: {err}")

            lines.append("")

        all_primary = ["index.html", "script.js", "styles.css"]
        issues = [f for f in all_primary if f not in created_files]
        if issues:
            lines.append("## [KNOWN ISSUES]")
            for f in issues:
                lines.append(f"- `{f}` has not been written yet")
            lines.append("")

        lines.append("## [PREVIOUS ATTEMPTS]")
        all_errors: list[str] = []
        for s in self._stage_summaries:
            for err in s.get("errors", []):
                all_errors.append(f"- [{s.get('stage', '?')}] {err}")
        if all_errors:
            lines.extend(all_errors)
        else:
            lines.append("- No errors recorded.")
        lines.append("")

        lines.append("## [CONTEXT NOTE]")
        lines.append(
            "This file is a compressed summary. Full file contents are in PLAN.md. "
            "Do not re-read files whose content is already visible in this conversation."
        )

        content = "\n".join(lines)
        try:
            self._chat_md_path().write_text(content, encoding="utf-8")
        except OSError as exc:
            self._emit_reasoning_raw("system", f"Warning: could not write CHAT.md: {exc}")

    def _read_plan_md(self) -> str:
        try:
            return self._plan_md_path().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _read_chat_md(self) -> str:
        try:
            return self._chat_md_path().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _write_plan_md(self, general_plan_text: str, created_files: set[str]) -> None:
        """Write or update PLAN.md in the workspace with current build state and cross-file references."""
        all_primary = ["index.html", "script.js", "styles.css"]
        lines: list[str] = ["# PLAN.md\n"]

        # Task
        if self._pipeline_task:
            lines.append(f"## Task\n{self._pipeline_task}\n")

        # Features and architecture
        if general_plan_text.strip():
            lines.append(f"## Features & Architecture\n{general_plan_text.strip()}\n")

        # Build status checklist
        status = ["## Build Status"]
        for f in all_primary:
            mark = "x" if f in created_files else " "
            status.append(f"- [{mark}] {f}")
        lines.append("\n".join(status) + "\n")

        # HTML element reference (populated after html_code stage)
        if self._plan_html_refs:
            refs = self._plan_html_refs
            ref_lines = [
                "## HTML Element Reference",
                "> For: **script.js** — use these IDs in `getElementById`/`querySelector`",
                "> For: **styles.css** — use these class names as selectors",
                "",
            ]
            if refs.get("ids"):
                ref_lines.append("### Element IDs")
                for id_ in refs["ids"]:
                    ref_lines.append(f"- `#{id_}`")
                ref_lines.append("")
            if refs.get("buttons"):
                ref_lines.append("### Buttons")
                for id_, label in refs["buttons"]:
                    ref_lines.append(f'- `#{id_}` — "{label}"')
                ref_lines.append("")
            if refs.get("modal_ids"):
                ref_lines.append("### Modals (start hidden — JS removes `.hidden` to show)")
                for id_ in refs["modal_ids"]:
                    ref_lines.append(f"- `#{id_}`")
                ref_lines.append("")
            meaningful_classes = [
                c for c in refs.get("classes", [])
                if c not in {"hidden", "active", "disabled", "btn"}
            ]
            if meaningful_classes:
                ref_lines.append("### CSS Classes (must be styled in styles.css)")
                ref_lines.append(", ".join(f"`.{c}`" for c in meaningful_classes[:40]))
                ref_lines.append("")
            lines.append("\n".join(ref_lines))

        # JS dynamic classes (populated after js_code stage)
        if self._plan_js_classes:
            dynamic = [c for c in self._plan_js_classes if c not in {"hidden", "active", "disabled"}]
            if dynamic:
                js_lines = [
                    "## JS Dynamic Classes",
                    "> For: **styles.css** — must define CSS rules for ALL of these",
                    "",
                ]
                for cls in dynamic:
                    js_lines.append(f"- `.{cls}`")
                lines.append("\n".join(js_lines) + "\n")

        # Next steps hint
        remaining = [f for f in all_primary if f not in created_files]
        if remaining:
            lines.append(f"## Next Steps\nStill to write: {', '.join(remaining)}\n")

        content = "\n".join(lines)
        try:
            self._plan_md_path().write_text(content, encoding="utf-8")
            done_count = sum(1 for f in all_primary if f in created_files)
            self._emit_reasoning_raw("system", f"PLAN.md updated ({done_count}/3 files complete)")
        except OSError as exc:
            self._emit_reasoning_raw("system", f"Warning: could not write PLAN.md: {exc}")

    def _extract_html_refs(self, html_content: str) -> dict[str, Any]:
        """Extract element IDs, class names, modals, and buttons from HTML content."""
        # Element IDs — preserve order, deduplicate
        raw_ids = re.findall(r'\bid=["\']([^"\']+)["\']', html_content)
        ids: list[str] = list(dict.fromkeys(raw_ids))

        # All class names — flatten, deduplicate, preserve order
        class_attr_values = re.findall(r'\bclass=["\']([^"\']+)["\']', html_content)
        seen_cls: set[str] = set()
        classes: list[str] = []
        for val in class_attr_values:
            for cls in val.split():
                if cls and cls not in seen_cls:
                    seen_cls.add(cls)
                    classes.append(cls)

        # Modal IDs — IDs that have class containing "modal" or "overlay", or start with "hidden"
        modal_ids: list[str] = []
        for id_ in ids:
            # Find the element line containing this ID
            pattern = rf'\bid=["\']({re.escape(id_)})["\'][^>]*class=["\']([^"\']*)["\']'
            m = re.search(pattern, html_content)
            if not m:
                pattern = rf'class=["\']([^"\']*)["\'][^>]*\bid=["\']({re.escape(id_)})["\']'
                m = re.search(pattern, html_content)
                if m:
                    cls_val = m.group(1)
                else:
                    cls_val = ""
            else:
                cls_val = m.group(2)
            cls_lower = cls_val.lower()
            id_lower = id_.lower()
            if ("modal" in cls_lower or "overlay" in cls_lower or
                    "modal" in id_lower or "overlay" in id_lower or
                    "hidden" in cls_lower.split()):
                modal_ids.append(id_)

        # Buttons — id + visible text label
        button_pattern = re.findall(
            r'<button[^>]*\bid=["\']([^"\']+)["\'][^>]*>([^<]*)</button>',
            html_content,
        )
        buttons = [(id_.strip(), text.strip()) for id_, text in button_pattern if id_.strip()]

        return {
            "ids": ids,
            "classes": classes,
            "modal_ids": modal_ids,
            "buttons": buttons,
        }

    def _extract_js_classes(self, js_content: str) -> list[str]:
        """Extract dynamic class names from JS classList calls and className assignments."""
        seen: set[str] = set()
        classes: list[str] = []

        def _add(cls_str: str) -> None:
            for cls in cls_str.split():
                if cls and cls not in seen:
                    seen.add(cls)
                    classes.append(cls)

        # classList.add/remove/toggle/replace('name') or ("name")
        for m in re.finditer(r'classList\.\w+\(\s*["\']([^"\']+)["\']', js_content):
            _add(m.group(1))

        # .className = 'name' or .className = "name" or += "name"
        for m in re.finditer(r'\.className\s*[+]?=\s*["\']([^"\']+)["\']', js_content):
            _add(m.group(1))

        # innerHTML / template literals: class="name" or class='name'
        for m in re.finditer(r'\bclass=["\']([^"\']+)["\']', js_content):
            _add(m.group(1))

        # el.setAttribute('class', 'name')
        for m in re.finditer(r'setAttribute\s*\(\s*["\']class["\'],\s*["\']([^"\']+)["\']', js_content):
            _add(m.group(1))

        return classes

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
