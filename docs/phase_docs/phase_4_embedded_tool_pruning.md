# Phase 4 — Embedded Tool Pruning (Enhanced with Planner + Reranker)

## What was built in this phase

Phase 4 now implements a model-guided tool selection pipeline instead of direct one-shot pruning from only the user prompt.

Implemented outcomes:

1. Startup model lifecycle (no lazy-loading)
   - Required models are ensured at startup.
   - Missing models are pulled on first run and then reused from local Ollama storage.
   - Both chat and embedding models are warmed before agent loop starts.

2. Embedding candidate retrieval
   - Tool vectors are generated/cached in local storage.
   - Per-iteration retrieval query is embedded.
   - Top-N candidate tools are retrieved by cosine similarity.

3. Planner integration (model-driven)
   - For each loop iteration, a planner step derives:
     - `subgoal`
     - `retrieval_query`
     - `tool_hints`
     - `rationale`
   - Retrieval is based on this evolving plan context, not just the initial user prompt.

4. Reranker integration (model-driven)
   - Candidate tools are reranked by the model using current task + plan + candidate metadata.
   - Top-K reranked tools are sent to the tool-calling model for that iteration.

5. Structured trace logging
   - Retrieval and final selection events are logged to support verification and debugging.

## New files created

- `orchestrator/planner.py`
- `orchestrator/reranker.py`
- `docs/phase_docs/phase_4_embedded_tool_pruning.md`

## Existing files updated

- `orchestrator/main_orchestrator.py`
- `orchestrator/loop_controller.py`
- `orchestrator/tool_pruner.py`
- `orchestrator/ollama_client.py`

## Detailed explanation of each file/module

### 1) `orchestrator/planner.py`

Purpose:
- Produces step-level planning context that guides retrieval/reranking each iteration.

Main behavior:
- Calls chat model with recent loop context.
- Requests strict JSON output with:
  - `subgoal`
  - `retrieval_query`
  - `tool_hints`
  - `rationale`
- Includes robust JSON fallback parsing when model output is noisy.

Why this matters:
- Tool candidate retrieval is now context-aware and iterative.

### 2) `orchestrator/reranker.py`

Purpose:
- Reranks embedding-retrieved candidates using model reasoning.

Main behavior:
- Receives task, planner output, and candidate list.
- Requests JSON ranking from model (`name`, `score` in range 0..1).
- Filters to valid candidate names only.
- Falls back to embedding-only order when reranker output is invalid.

Why this matters:
- Final tool menu is model-guided per iteration.

### 3) `orchestrator/tool_pruner.py` (updated)

Purpose:
- Handles embedding storage + candidate retrieval + logging.

Main behavior:
- Maintains local vector store in `embeddings/tool_vectors.json`.
- Computes cosine similarity between query and tool vectors.
- Returns Top-N candidate pool (`retrieve_candidates`).
- Emits JSONL log events (`retrieval`, `selection`) to `logs/tool_pruning.log`.

### 4) `orchestrator/loop_controller.py` (updated)

Purpose:
- Runs iterative orchestration loop with dynamic tool availability.

New per-iteration flow:
1. Planner generates subgoal/retrieval query from current context.
2. Tool pruner retrieves Top-N candidates via embeddings.
3. Reranker model scores and selects Top-K tools.
4. Only selected tools are sent in the tool schema for that iteration.
5. Model issues tool calls from this constrained menu.
6. Tool results are fed back into memory.

New output traces:
- `selection_trace` now includes:
  - planner output
  - retrieval report
  - reranker report
  - selected tool names

### 5) `orchestrator/main_orchestrator.py` (updated)

Purpose:
- Wires startup preload/warmup and planner+rereanker orchestration.

New CLI options:
- `--embedding-model`
- `--top-k-tools`
- `--candidate-pool-size`

Startup sequence:
1. Ensure required models exist (`qwen2.5-coder:14b` + embedding model).
2. Warm both models at startup (no lazy-load behavior).
3. Instantiate planner, pruner, reranker.
4. Run loop with dynamic per-iteration tool menu.

Result JSON now exposes:
- planner config
- reranker config
- tool pruning config
- orchestrator result with `selection_trace`

### 6) `orchestrator/ollama_client.py` (updated)

Purpose:
- Adds robust model management for startup requirements.

New behavior:
- Ensures required models are present and pulls missing models.
- Handles bare model names vs `:latest` names equivalently to avoid redundant pulls.
- Exposes warmup calls for both chat and embedding models.

## Architecture update summary

Before enhancement:
- User prompt -> embedding prune -> static tool subset for run.

After enhancement:
- Iteration context -> Planner -> embedding candidate retrieval -> model reranker -> per-iteration tool subset -> tool-calling model.

This now aligns with your requirement that tool determination is model-guided, with embedding retrieval as candidate generation rather than final authority.

## Validation performed

1. Module compile checks:
- `python3 -m py_compile` over updated orchestrator modules.

2. End-to-end orchestrator run:
- Verified planner and reranker enabled in output.
- Verified `selection_trace` contains planner/retrieval/rerank per iteration.

3. Log verification:
- `logs/tool_pruning.log` records retrieval and selection events as structured JSON lines.

4. Model preload behavior verification:
- Confirmed no redundant pull when embedding model exists as `nomic-embed-text:latest` and runtime requests `nomic-embed-text`.

## Embedding model sizing guidance (for compiler-focused tool pruning)

Answer:
- Lightweight embeddings remain sufficient for your current compiler-agent scope.

Why:
1. Tool catalog size is still small/moderate.
2. Tools are semantically distinct and structured.
3. Planner+rereanker now handles most fine-grained disambiguation.

Upgrade trigger:
- Move to larger embedding models only if candidate retrieval recall degrades as tool count grows or prompts become much more ambiguous.

## Open-source and cost compliance

All implementations remain free/open-source and local:

- Ollama local runtime
- Open-source chat + embedding models via Ollama
- Python standard library implementation

No paid APIs, proprietary SDKs, or commercial dependencies were introduced.
