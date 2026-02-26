# Phase 3 — Orchestrator Skeleton

## What was built in this phase

This phase implemented the first working orchestrator skeleton that can:

1. Connect to Ollama (`qwen3:14b`) via local HTTP API.
2. Send a static tool list to the model.
3. Receive and interpret structured tool calls.
4. Route tool executions to the MCP server.
5. Feed tool output back into the conversation loop.
6. Stop when the model indicates completion (`DONE`).

## New files created

- `orchestrator/main_orchestrator.py`
- `orchestrator/loop_controller.py`
- `orchestrator/ollama_client.py`
- `orchestrator/session_memory.py`
- `docs/phase_docs/phase_3_orchestrator_skeleton.md`

## Detailed explanation of each file/module

### 1) `orchestrator/main_orchestrator.py`

Purpose:
- CLI entrypoint for running the orchestrator skeleton.

Key behaviors:
- Accepts:
  - `--workspace-root` (absolute workspace path)
  - `--task` (user prompt)
  - `--model` (default: `qwen3:14b`)
- Defines and sends a static tool schema list (`STATIC_TOOLS`) to the model.
- Initializes:
  - Ollama client
  - Loop controller
- Returns structured JSON output including:
  - ollama health status
  - tools sent to model
  - orchestrator loop result

Design note:
- Default Ollama URL is `http://localhost:11434` for host execution.
- In Docker, `OLLAMA_BASE_URL` can override to `http://host.docker.internal:11434`.

### 2) `orchestrator/loop_controller.py`

Purpose:
- Implements iterative orchestration logic.

Core flow:
1. Add system and user messages to session memory.
2. Request model response with static tool list.
3. Parse tool calls from response.
4. For each tool call:
   - Send MCP `call_tool` JSON request to `mcp_server/server.py`.
   - Capture and parse structured JSON result.
   - Append tool result message back into conversation.
5. Repeat until:
  - model produces no tool call (completion).

Outputs:
- `status`, `iterations`, `final_message`, and `tool_trace`.

### 3) `orchestrator/ollama_client.py`

Purpose:
- Encapsulates Ollama API communication and tool-call extraction.

Features:
- `health()`:
  - checks `/api/tags` for model availability
- `chat()`:
  - sends non-streaming chat request with `messages` and `tools`
- `extract_assistant_message()`:
  - validates response shape
- `extract_tool_calls()`:
  - parses native tool calls from `message.tool_calls`
  - includes fallback parser for JSON tool intent embedded in assistant text

Testing helper:
- `ORCHESTRATOR_MOCK_TOOLCALL=1` enables deterministic local mock responses for tool-call loop verification.

### 4) `orchestrator/session_memory.py`

Purpose:
- Lightweight message-memory container for loop state.

Responsibilities:
- Stores chronological conversation messages.
- Supports adding messages with extra fields (for tool metadata).

## How this phase works and interacts with other parts

Phase 3 integration flow:

1. User task enters orchestrator CLI.
2. Orchestrator sends messages + static tools to Ollama.
3. Model returns tool instruction.
4. Orchestrator calls MCP server through stdin JSON request.
5. MCP server executes tool in workspace sandbox and returns JSON.
6. Orchestrator appends tool result and continues loop.
7. Loop ends when the model returns `DONE` and returns structured summary.

This creates the foundational LLM↔Tool orchestration path required before tool-pruning logic in Phase 4.

## Validation performed

### A) Deterministic structured tool-call test (mock mode)

Command used:
- `ORCHESTRATOR_MOCK_TOOLCALL=1 python3 orchestrator/main_orchestrator.py --workspace-root "$PWD" --task "Inspect docs directory and finish when done"`

Observed:
- Orchestrator performed tool call to `dummy_sandbox_echo`.
- MCP server returned structured JSON tool result.
- Loop completed with `DONE` and non-empty `tool_trace`.

### B) Real Ollama connectivity test

Observed:
- `http://localhost:11434/api/tags` reachable.
- Local models detected, including `qwen3:14b`.

### C) Real-model structured tool-call loop

Command used:
- `python3 orchestrator/main_orchestrator.py --workspace-root "$PWD" --task "Use the dummy_sandbox_echo tool on relative path docs, then say DONE." --model qwen3:14b`

Observed:
- Tool call executed successfully.
- Tool result was traced and fed back.
- Loop completed with final `DONE` message.

## Open-source and cost compliance

This phase uses only free/open-source components:

- Python standard library (`urllib`, `subprocess`, `json`, etc.)
- Local Ollama endpoint using open-source models
- Existing local MCP server code

No paid APIs, proprietary SDKs, or commercial services were introduced.