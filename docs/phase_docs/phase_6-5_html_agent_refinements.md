# Phase 6.5 — HTML Agent Refinements

## Objective
Strengthen the HTML/CSS/JS-first agent workflow with a locked two-stage pipeline, reliable tool invocation, and stable output behavior.

## Functional Changes
1. Replaced the open-ended iterative loop with a fixed two-stage pipeline (plan → code).
2. Restricted available tools per stage to prevent the model from mixing planning and writing.
3. Hardened tool-call reliability with alias normalization, inline JSON extraction, and retry-on-empty.
4. Kept runtime constrained to plain local HTML/CSS/JS workflows.

## MCP Tools (Active Catalog)
Registered in `mcp_server/server.py`:
- `create_file`
- `read_file`
- `list_directory`
- `validate_web_app`
- `run_unit_tests`
- `plan_web_build`
- `dummy_sandbox_echo`

Note: `scaffold_web_app` has been removed from the active catalog. File creation is handled exclusively by `create_file`.

## Stage Tool Restrictions
```
plan stage  →  plan_web_build, read_file, list_directory
code stage  →  create_file (only)
```

## Planner
The planner (`orchestrator/planner.py`) runs once before the stages to produce a retrieval context and rationale. It emits:
- `subgoal`
- `retrieval_query`
- `tool_hints`
- `rationale`
- `app_purpose`, `suggested_features`, `visual_direction`, `interaction_model`
- `unit_test_plan`, `development_phases`, `active_phase`

The planner output is used to seed the embedding retrieval for tool pruning and to emit a reasoning summary to the UI. It does not drive per-iteration loop control.

## Reliability Mechanisms
| Mechanism | Description |
|---|---|
| Alias normalization | `edit_file`, `open_file`, `write_file`, etc. are remapped to canonical MCP names |
| Inline JSON extraction | If the model writes tool calls as text, the controller parses and executes them |
| Retry on empty | One automatic retry if the model returns no content and no tool calls |
| Deduplication | Duplicate tool calls within a stage are collapsed before execution |
| Context compaction | Long conversation histories are trimmed to stay within context limits |

## Completion Behavior
- Both stages always run regardless of intermediate results.
- A post-run validation call (`validate_web_app`) is made after the code stage as an informational check.
- No `DONE:` sentinel is required or expected from the model.

## Constraints
- No framework dependencies are required.
- Output targets local browser-openable HTML/CSS/JS files.
- JavaScript tests run through Node.js where available.
