# Phase 7 — HTML Agent Refinements

## Objective
Finalize the HTML/CSS/JS-first agent workflow with stronger planning structure, reliable tool invocation, and stable completion behavior.

## Functional Changes
1. Expanded planning structure for purpose, features, visual direction, interaction model, tests, and development phases.
2. Enforced phased small-step execution inside the main loop.
3. Hardened tool-call reliability with alias normalization and recovery prompting.
4. Kept runtime constrained to plain local HTML/CSS/JS workflows.

## MCP Tools (Active Catalog)
Registered in `mcp_server/server.py`:
- `create_file`
- `read_file`
- `list_directory`
- `scaffold_web_app`
- `validate_web_app`
- `run_unit_tests`
- `plan_web_build`
- `dummy_sandbox_echo`

Implemented in `mcp_server/tools/web_tools.py`:
- `scaffold_web_app_tool`
- `validate_web_app_tool`
- `run_unit_tests_tool`
- `plan_web_build_tool`

## Reasoning and Loop Updates
- Planner (`orchestrator/planner.py`) emits structured fields:
  - `app_purpose`
  - `suggested_features`
  - `visual_direction`
  - `interaction_model`
  - `unit_test_plan`
  - `development_phases`
  - `active_phase`
- Loop controller (`orchestrator/loop_controller.py`):
  - injects the active phase each iteration,
  - executes one concrete tool step per iteration,
  - normalizes alias tool names (`edit_file`, `open_file`, etc.) to actual MCP tools,
  - uses recovery prompting when analysis text is returned without tool calls,
  - requires explicit `DONE:` completion,
  - supports loop extension prompt (`+5`) when loop budget is exhausted.

## Reliability Notes
Primary failure mode addressed:
- Model output sometimes contained analysis text or alias tool names instead of valid calls.

Mitigations:
1. Alias normalization in `_normalize_tool_call`.
2. Recovery reprompt constrained to available tools.
3. Fallback parser support for JSON-like tool call content.

## Constraints
- No framework dependencies are required.
- Output targets local browser-openable HTML/CSS/JS files.
- JavaScript tests run through Node.js where available.
