# Phase 5 — Safe Compiler Tools + Guardrails

## Objective
Implement MCP tools required for a local compiler-construction workflow (C, Flex, Bison) while enforcing strict safety boundaries:
- workspace sandboxing only,
- no arbitrary shell execution,
- bounded subprocess execution,
- constrained compiler/linker flags,
- auditable tool-call logging.

## Summary of Delivered Work
Phase 5 is implemented with the following outcomes:
1. Added safe file tools for create/read/list operations in the workspace sandbox.
2. Added guarded C compilation and binary execution tools.
3. Added guarded Flex/Bison generation and linking tools.
4. Hardened sandbox/process guardrails with path, timeout, and argument constraints.
5. Registered all safe tools in MCP server with explicit JSON input schemas.
6. Added structured tool action logging for both success and failure.
7. Updated orchestrator to dynamically load tool schemas from MCP `list_tools` output.

## Files Added / Updated

### MCP Server
- `mcp_server/server.py`
  - Registers all Phase 5 tools and schemas.
  - Wraps each tool handler with `with_logging` so all calls are logged.
  - Supports `list_tools` and `call_tool` actions.

- `mcp_server/tools/sandbox.py`
  - Core guardrails:
    - `resolve_workspace_root` validates configured sandbox root.
    - `validate_relative_path` blocks empty, absolute, null-byte, and oversized path args.
    - `resolve_path_in_workspace` blocks traversal/escape outside workspace.
    - `validate_timeout_seconds` caps tool execution timeout.
    - `sanitize_cli_arguments` enforces argument type/length/null-byte constraints.
    - `run_safe_command` executes fixed argv commands with constrained env and timeout.

- `mcp_server/tools/file_tools.py`
  - Safe text file operations in workspace sandbox:
    - `create_file_tool`
    - `read_file_tool`
    - `list_directory_tool`

- `mcp_server/tools/compiler_tools.py`
  - `compile_c_tool`
    - validates source files are workspace-relative and exist,
    - restricts `cflags` via explicit allowlist + limited prefixes,
    - compiles through fixed argv (`cc ...`) with timeout guardrails.
  - `run_binary_tool`
    - executes only workspace binary path with bounded args/time.
  - `clean_build_tool`
    - removes only workspace-resolved targets.

- `mcp_server/tools/flex_bison_tools.py`
  - `generate_lexer_tool` (`flex`)
  - `generate_parser_tool` (`bison`)
  - `link_compiler_tool` (`cc` link step)
  - All tools enforce workspace-relative paths, file existence checks, timeouts, and linker-flag constraints.

- `mcp_server/tools/action_logger.py`
  - Appends JSONL records to `.compilot_logs/tool_actions.log`.
  - Records timestamp, tool name, arguments, and full result payload.
  - Logging includes both successful and failed tool executions.

### Orchestrator Integration
- `orchestrator/main_orchestrator.py`
  - Replaced static tool catalog with dynamic loading from MCP server via `list_tools`.
  - `load_tools_from_mcp(...)` normalizes MCP tool metadata into Ollama-compatible function schemas.
  - Keeps orchestrator tool menu synchronized with MCP server as tools evolve.

## Guardrail Model (Implemented)

### 1) Filesystem Sandbox
- Every file argument is treated as workspace-relative.
- Absolute paths are rejected.
- Path traversal escapes are rejected after full resolution.
- All writes/reads/removals are constrained to `WORKSPACE_ROOT`.

### 2) Process Execution Safety
- No shell string execution; only explicit argv arrays.
- Timeout is required and bounded by global max.
- CLI arguments are length-limited and null-byte checked.
- Process environment is minimal and explicit.

### 3) Tool Input Constraints
- MCP tool schemas use `additionalProperties: false` to avoid unvalidated parameters.
- Compiler and linker flags are filtered through allowlists/prefix rules.
- Runtime arguments (`args`) must be arrays, preventing direct shell injection patterns.

### 4) Auditability
- Every tool call writes a structured action log record.
- Failed calls are logged with normalized error details.

## Validation Performed

Manual validation (JSON MCP requests) confirmed:
1. `list_tools` returns the full Phase 5 tool catalog.
2. Path traversal attempts are blocked (`Path escapes workspace sandbox`).
3. Absolute path attempts are blocked (`Absolute paths are not allowed`).
4. Disallowed compiler flags are rejected (e.g., unknown warning flag).
5. End-to-end compile flow works:
   - create source,
   - compile C,
   - run produced binary.
6. Flex/Bison generation works for sample `.l` and `.y` files.
7. Tool action log is written and includes failed calls.

## Notes
- Generated validation artifacts were cleaned after checks (binaries, generated parser/lexer outputs, runtime log file) to keep repository state minimal.
- Phase 5 intentionally does not add arbitrary command execution tools; all execution pathways are fixed and constrained.

## Phase 5 Completion Status
Complete.

Ready to proceed to Phase 6 only after explicit user approval.
