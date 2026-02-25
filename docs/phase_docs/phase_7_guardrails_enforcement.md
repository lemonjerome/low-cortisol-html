# Phase 7 — Guardrails Enforcement

## Objective
Enforce strict safety guarantees for tool execution by hardening workspace scope checks, validating all tool inputs, increasing auditability, and verifying unsafe operations are blocked.

## Scope
Implemented in this phase:
1. Restrict filesystem scope to `WORKSPACE_ROOT` for all tool file access.
2. Validate tool inputs against each tool's declared JSON schema before execution.
3. Keep structured per-tool action logging for successful and failed calls.
4. Add unsafe-operation tests to verify guardrails are effective.

Not implemented in this phase:
- Workspace/root selection from UI (planned for Phase 8).

## Implemented Changes

### 1) Filesystem scope restriction
- Existing sandbox enforcement in `mcp_server/tools/sandbox.py` remains the mandatory path resolver for tool file operations.
- `resolve_workspace_root(...)` requires a valid absolute directory and rejects empty roots.
- `resolve_path_in_workspace(...)` rejects traversal/escape attempts outside the active workspace.

### 2) Tool input validation (new runtime enforcement)
- Updated `mcp_server/tool_registry.py` to enforce tool schemas at call time.
- Validation now checks:
  - `type` constraints (`object`, `array`, `string`, `boolean`, `integer`),
  - required fields,
  - unknown field rejection when `additionalProperties: false`,
  - array item type validation through `items` schema.
- Tool handlers execute only after schema validation succeeds.

### 3) Logging
- Tool action logging remains active via `mcp_server/tools/action_logger.py`.
- Both success and failure paths are logged with arguments and results in:
  - `.low-cortisol-html-logs/tool_actions.log`

### 4) Unsafe operation tests (new)
- Added `tests/phase7_guardrails_unsafe_tests.py`.
- Test suite verifies blocked behavior for:
  - path traversal file write (`../escape.txt`),
  - absolute path read (`/etc/passwd`),
  - unexpected input fields rejected by schema,
  - wrong input types rejected by schema.

## Validation Run
Recommended command:
- `python3 tests/phase7_guardrails_unsafe_tests.py`

Expected result:
- Script exits successfully and prints guardrail check pass message.

## Phase 8 Note
`WORKSPACE_ROOT` is still provided by runtime/env and is not selected by UI in this phase.
User-driven workspace/root selection remains planned for Phase 8 and is intentionally not implemented here.
