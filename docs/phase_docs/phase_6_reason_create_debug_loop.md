# Phase 6 — Two-Stage Pipeline Agent

## Objective
Replace the general-purpose iterative loop with a focused two-stage pipeline optimized for HTML/CSS/JS generation:
1. **Plan** — one thorough planning pass using `plan_web_build`.
2. **Code** — write all required files in a single pass using `create_file`.

Additionally, add automatic compute backend detection:
- macOS => `mps`
- NVIDIA GPU host => `cuda`
- fallback => `cpu`

## Implemented Changes

### 1) Two-stage pipeline (`orchestrator/loop_controller.py`)
The loop controller now runs exactly two sequential stages:

```
Stage 1 — plan
  Allowed tools: plan_web_build, read_file, list_directory
  Goal: produce a comprehensive feature-level plan

Stage 2 — code
  Allowed tools: create_file (only)
  Goal: write every file (HTML, CSS, JS) in one pass
```

Each stage gets its own LLM call. Tool availability is restricted per stage — the model cannot call `create_file` during planning, and cannot call planning tools during coding.

### 2) Workspace detection
Before any stage runs, the controller inspects the workspace:
- **Empty workspace** → new project path; model is told to create everything from scratch.
- **Populated workspace** → existing project path; model receives the current file list and contents (capped at first 30 files / 10 KB per file) as context before planning.

### 3) Adaptive recovery within stages
- If the model returns an empty response, the controller sends a nudge prompt and retries once.
- If the model embeds tool calls as raw JSON text (instead of structured calls), the controller parses and extracts them.
- Tool name aliases (`edit_file`, `write_file`, `open_file`, etc.) are normalized to canonical MCP tool names.
- Duplicate tool calls within a stage are deduplicated to avoid redundant file writes.

### 4) Validation pass (informational)
After both stages complete, the controller runs `validate_web_app` as a read-only check and emits the result as reasoning output. Validation failures do not restart the pipeline.

### 5) Session and project memory
- `SessionMemory` holds the conversation messages for the current run.
- `ProjectMemory` tracks which files were touched and provides file-level semantic retrieval context.
- Both are reset at the start of each new run.

### 6) Context management
- Long conversation histories are compacted to stay within model context limits.
- Code stage is given a higher `num_predict` budget (default 16 384 tokens) vs planning (8 192).

## Auto GPU Detection

### New module
Added `orchestrator/device_detection.py` with `detect_compute_backend(preferred="auto")`.

Selection policy:
1. If explicit `--device` is passed (`mps|cuda|cpu`), honor it.
2. If auto and host is macOS, select `mps`.
3. Else if `nvidia-smi` is available, select `cuda`.
4. Else use `cpu`.

### Orchestrator integration
Updated `orchestrator/main_orchestrator.py`:
- Added CLI flag `--device {auto,mps,cuda,cpu}`.
- Runs backend detection at startup.
- Exports selected backend to `LOW_CORTISOL_HTML_DEVICE` environment variable.
- Includes backend info in final JSON output under `compute_backend`.

## Result shape
```json
{
  "ok": true,
  "status": "completed",
  "iterations": 2,
  "final_message": "...",
  "tool_trace": [...],
  "selection_trace": [],
  "repair_trace": []
}
```
`iterations` is always 2 (one per stage). `selection_trace` and `repair_trace` are kept for schema compatibility but are empty in the current pipeline.

## Phase 6 Completion Status
Implemented.
