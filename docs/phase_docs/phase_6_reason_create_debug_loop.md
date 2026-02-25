# Phase 6 — Reason–Create–Debug Loop

## Objective
Implement a robust iterative control loop that:
1. reasons about next action,
2. creates/edits/runs using tools,
3. consumes tool/runtime diagnostics,
4. repeats until objective completion is explicitly confirmed.

Additionally, add automatic compute backend detection:
- macOS => `mps`
- NVIDIA GPU host => `cuda`
- fallback => `cpu`

## Implemented Changes

### 1) Iterative Reason→Create→Debug loop behavior
Updated `orchestrator/loop_controller.py` to enforce loop discipline:
- System prompt now explicitly frames operation as a `Reason→Create→Debug` loop.
- Agent must end with a final response starting with `DONE:`.
- If assistant returns no tool calls without `DONE:`, controller injects a continuation instruction and loops.

### 2) Tool diagnostics feedback injection
Added tool/runtime feedback extraction and reinjection in `orchestrator/loop_controller.py`:
- Collects failures from tool calls in:
  - `create_file`
  - `read_file`
  - `list_directory`
  - `scaffold_web_app`
  - `validate_web_app`
  - `run_unit_tests`
  - `plan_web_build`
- Extracts `stderr`, `stdout`, and structured `error` payloads.
- Injects summarized diagnostics as a new user message so the model repairs in next iteration.
- Persists repair events in `repair_trace` for post-run inspection.

### 3) Multi-step repair confirmation support
The loop now records `repair_trace` containing per-iteration diagnostics and tools involved.
This enables validation that multi-step failures were observed and followed by repair attempts before completion.

### 4) Completion gating
Completion now requires explicit success signal:
- `DONE:` prefix in assistant message,
- otherwise loop continues until `max_loops`.

When loop limit is reached:
- system prompts user whether to continue,
- `yes` extends budget by `+5` loops,
- `no` stops and returns `max_loops_reached`.

This prevents premature stop when the model returns plain text without truly finishing the task.

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

## Validation Expectations
Manual checks should confirm:
1. Loop does not terminate early unless assistant replies with `DONE:`.
2. Tool failure output appears in next-iteration context (via feedback injection).
3. `repair_trace` records failure cycles.
4. Device auto-detection returns:
   - macOS => `mps`
   - NVIDIA Linux/WSL host with `nvidia-smi` => `cuda`
   - otherwise => `cpu`

## Phase 6 Completion Status
Implemented.
