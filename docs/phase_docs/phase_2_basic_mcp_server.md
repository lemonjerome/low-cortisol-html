# Phase 2 — Basic MCP Server

## What was built in this phase

This phase implemented a functional local MCP server skeleton with:

1. A server entrypoint that accepts JSON requests via stdin and returns structured JSON responses.
2. A tool registry for tool metadata and dispatch.
3. A registered dummy tool (`dummy_sandbox_echo`) for manual execution tests.
4. Workspace sandbox enforcement to block path traversal outside the approved workspace.

## New files created

- `mcp_server/server.py`
- `mcp_server/tool_registry.py`
- `mcp_server/tools/sandbox.py`
- `mcp_server/tools/dummy_tools.py`
- `mcp_server/tools/__init__.py`
- `docs/phase_docs/phase_2_basic_mcp_server.md`

## Detailed explanation of each file/module

### 1) `mcp_server/tool_registry.py`

Purpose:
- Central registry for MCP tools and execution routing.

What it contains:
- `ToolDefinition` dataclass:
  - `name`, `description`, `input_schema`, `handler`
- `ToolRegistry` class:
  - `register(...)`: adds tools and prevents duplicate names.
  - `list_tools()`: returns tool metadata in JSON-serializable structure.
  - `call_tool(tool_name, arguments)`: dispatches validated call to handler.

How it interacts:
- `server.py` builds this registry at startup and uses it to list/call tools.

### 2) `mcp_server/tools/sandbox.py`

Purpose:
- Enforces filesystem boundaries for all workspace file access.

What it contains:
- `resolve_workspace_root(workspace_root: str) -> Path`
  - Requires a non-empty absolute path.
  - Requires path to exist and be a directory.
- `resolve_path_in_workspace(workspace_root: Path, relative_path: str) -> Path`
  - Resolves requested relative path.
  - Ensures resolved target is still inside workspace root.
  - Raises `ValueError` on escape attempts.

How it interacts:
- Tool modules call this before touching filesystem paths.

### 3) `mcp_server/tools/dummy_tools.py`

Purpose:
- Provides a minimal test tool for Phase 2 validation.

Tool:
- `sandbox_echo_path(arguments, workspace_root)`
  - Input: `relative_path` (optional, default `.`)
  - Behavior:
    - Resolves path using sandbox helper.
    - Returns structured metadata:
      - workspace root
      - requested path
      - resolved path
      - existence and directory flag
      - first 50 child names when directory exists

How it interacts:
- Registered in `server.py` via `ToolRegistry`.

### 4) `mcp_server/server.py`

Purpose:
- Executable MCP server skeleton for local tool operations.

Supported request actions:
- `list_tools`
  - Returns all registered tool metadata (`name`, `description`, `input_schema`).
- `call_tool`
  - Requires `tool` string and object `arguments`.
  - Calls matching tool in registry.

Response format:
- Success:
  - `{ "ok": true, "action": "...", "result": ... }`
- Error:
  - `{ "ok": false, "error": { "type": "...", "message": "..." } }`

Startup behavior:
- Reads `WORKSPACE_ROOT` env var.
- Validates workspace root through sandbox module.
- Refuses startup if workspace root is missing/invalid.

### 5) `mcp_server/tools/__init__.py`

Purpose:
- Marks the tools directory as a Python package for consistent imports.

## How this phase works with current architecture

Flow implemented in Phase 2:

1. Client sends a JSON request to stdin (`list_tools` or `call_tool`).
2. `server.py` loads and validates workspace root.
3. `ToolRegistry` resolves tool metadata or dispatches execution.
4. Dummy tool runs with sandbox path enforcement.
5. Server returns structured JSON on stdout.

This provides the MCP execution core required before connecting the orchestrator in later phases.

## Manual validation performed

The following manual checks were executed successfully:

1. List tools:
   - Request: `{\"action\":\"list_tools\"}`
   - Result: `ok: true` and includes `dummy_sandbox_echo` schema.

2. Execute dummy tool with safe path:
   - Request: `call_tool` with `relative_path: \"docs\"`
   - Result: `ok: true`, returned resolved path and directory contents.

3. Sandbox escape attempt:
   - Request: `call_tool` with `relative_path: \"../\"`
   - Result: `ok: false`, message `Path escapes workspace sandbox`.

## Guardrail status in this phase

Implemented now:
- Filesystem path traversal protection for tool path resolution.
- Workspace root validation before server serves requests.
- Structured error responses for invalid operations.

Planned later (deeper guardrails and logging):
- Expanded input validation for all future tools.
- Full action logging/tracing.
- Additional policy enforcement across web/runtime tools.

## Open-source and cost compliance

This phase uses only Python standard library modules and project-local code.
No paid services, proprietary SDKs, or closed-source dependencies were introduced.