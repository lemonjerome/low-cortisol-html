# Phase 8 — UI Integration

## Objective
Provide a local UI for project management and chat-driven HTML workflow execution.

## Delivered UX
1. Static UI bundle with `index.html`, `style.css`, `script.js`.
2. Header layout:
   - left: `Open HTML`
   - center: `Low Cortisol HTML`
   - right: `New Project`, `Open Project`, `Clear Chat`
3. Three vertical columns:
   - left: action stream (tool calls + file edits)
   - middle: reasoning/thinking/debugging stream
   - right: chat stream with user bubbles (right) and assistant output (left)
4. Working-state indicators under active assistant response (`thinking...`, `getting tools...`, `working...`).
5. Send locking while model is running (no concurrent user sends).

## Project Lifecycle Flows
### Startup root prompt
- UI requires workspace parent root before continuing.
- Validates absolute path through backend before enabling use.

### New Project
- Modal asks:
  - parent directory (default workspace root)
  - workspace folder name (default `lch_new_project`)
- Supports `Choose` button with cross-platform chooser fallback handling.
- Creates workspace directory after validation.
- Clears chat/memory when project is created.

### Open Project
- Validates project absolute path.
- Restricts openable project names to `lch_` prefix only.
- On open, backend scans and stores workspace structure summary for model context.
- Clears chat/memory when project is opened.

### Clear Chat
- Confirmation modal required.
- Clears chat UI and in-memory conversation state.
- No persistence across app restarts.

### Open HTML
- Opens landing page using convention:
  1. `index.html`
  2. `main.html`
- Returns errors when no project is open or landing file is missing.

## Backend integration
Implemented in `ui/server.py`:
- Static file serving for UI assets.
- JSON APIs for root setup, project create/open, clear chat, open landing HTML.
- Streaming chat endpoint using NDJSON events:
  - status updates
  - reasoning stream tokens
  - action events from tool trace
  - final assistant response stream

## Docker launch integration
`docker/docker-compose.yml` now supports one-command launch for the UI runtime with:
- source mount (`..:/app`)
- workspace mount (`${HOME}/Desktop/lch_workspaces:/root/Desktop/lch_workspaces`)
- `8000:8000` port mapping

Run:
- `docker compose -f docker/docker-compose.yml up --build`

## Notes
- Workspace/root selection from external product UI remains a later concern; current Phase 8 local UI validates and sets root at startup.
