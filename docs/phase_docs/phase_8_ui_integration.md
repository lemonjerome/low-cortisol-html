# Phase 8 — UI Integration

## Objective
Provide a local UI for project management and chat-driven HTML workflow execution.

## Delivered UX
1. Static UI bundle with `index.html`, `style.css`, `script.js`.
2. Header layout:
   - left: `Open HTML`
   - center: `Low Cortisol HTML`
   - right: `New Project`, `Open Project`, `Clear Chat`, `Help`
3. Three vertical columns:
   - left: action stream (tool calls + file edits with code previews)
   - middle: reasoning/thinking/debugging stream
   - right: chat stream with user bubbles (right) and assistant output (left)
4. Working-state indicators under active assistant response (`thinking...`, `getting tools...`, `working...`).
5. Send locking while model is running (no concurrent user sends).
6. Project indicator in the header showing the currently open project name.

## Project Lifecycle Flows

### Workspaces root
- The server auto-creates `~/Desktop/lch_workspaces` on first launch.
- No startup modal or root path prompt. The workspaces directory is fixed and transparent.

### New Project
- Modal asks only for a workspace folder name (default `lch_new_project`).
- Name must start with `lch_`.
- Folder is always created inside `~/Desktop/lch_workspaces` — the parent directory is not configurable.
- Clears chat/memory when project is created.

### Open Project
- Modal shows a locked list of `lch_` folders found in `lch_workspaces`.
- No free-text path input. No folder browser navigation.
- Clicking a project entry opens it immediately.
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
- JSON APIs for project create/open, clear chat, browse-dir (project picker), open landing HTML.
- `/api/status` returns `workspaces_root`, `current_project_name`, and model info.
- Streaming chat endpoint using NDJSON events:
  - status updates
  - reasoning stream tokens
  - action events from tool trace (including code block previews)
  - final assistant response stream

## API endpoints (key)
| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | App state, workspaces root, current project |
| POST | `/api/create-project` | Create a new `lch_` project folder |
| POST | `/api/open-project` | Open an existing project |
| GET | `/api/browse-dir` | List `lch_` subdirs for project picker |
| POST | `/api/chat` | Streaming NDJSON chat endpoint |
| POST | `/api/clear-chat` | Reset conversation memory |
| POST | `/api/open-html` | Open project `index.html` or `main.html` |

## Docker launch integration
`docker/docker-compose.yml` supports one-command launch for the UI runtime with:
- source mount (`..:/app`)
- workspace mount (`${HOME}/Desktop/lch_workspaces:/root/Desktop/lch_workspaces`)
- `8000:8000` port mapping

Run:
- `docker compose -f docker/docker-compose.yml up --build`
