# Phase 5 — Safe Web Tools + Guardrails

## Objective
Implement MCP tools for a local HTML/CSS/JS workflow with strict safety boundaries:
- workspace sandboxing only,
- no arbitrary shell execution,
- bounded subprocess execution,
- constrained arguments and timeouts,
- auditable tool-call logging.

## Summary of Delivered Work
Phase 5 provides safe, web-focused tooling:
1. Safe file tools for create/read/list operations in the workspace sandbox.
2. Safe web scaffold and validation tools.
3. Safe JavaScript test execution with bounded timeout.
4. Hardened sandbox/process guardrails with path, timeout, and argument constraints.
5. MCP registration for all web tools with explicit JSON schemas.
6. Structured tool action logging for success/failure events.
7. Dynamic tool loading from MCP `list_tools` in the orchestrator.

## Files Added / Updated

### MCP Server
- `mcp_server/server.py`
  - Registers web tools and schemas.
  - Wraps handlers with `with_logging` for auditable tool calls.

- `mcp_server/tools/sandbox.py`
  - Core guardrails:
    - workspace root validation,
    - path traversal/absolute path rejection,
    - timeout validation,
    - constrained command execution.

- `mcp_server/tools/file_tools.py`
  - `create_file_tool`
  - `read_file_tool`
  - `list_directory_tool`

- `mcp_server/tools/web_tools.py`
  - `scaffold_web_app_tool`
  - `validate_web_app_tool`
  - `run_unit_tests_tool`
  - `plan_web_build_tool`

- `mcp_server/tools/action_logger.py`
  - Appends JSONL records to `.low-cortisol-html-logs/tool_actions.log`.

### Orchestrator Integration
- `orchestrator/main_orchestrator.py`
  - Loads tool schemas dynamically from MCP `list_tools`.

## Guardrail Model (Implemented)
1. Filesystem sandbox (workspace-relative paths only).
2. Process safety (no shell strings, bounded timeout, minimal env).
3. Input constraints (`additionalProperties: false`, typed args).
4. Auditability (structured logs for all tool calls).

## Validation Performed
Manual validation confirmed:
1. `list_tools` returns web tool catalog.
2. Path traversal and absolute paths are blocked.
3. Web scaffold creates expected files.
4. Structure validation checks required HTML/CSS/JS references.
5. JS test runner executes with timeout and returns structured output.

## Phase 5 Completion Status
Complete.
