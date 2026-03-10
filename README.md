# Low Cortisol HTML

A local AI coding agent that turns a chat prompt into a working HTML/CSS/JS web app. Everything runs on your machine — no cloud, no API keys, no subscription.

You describe what you want to build. The agent plans it, writes all the files, and opens the result in your browser.

---

## What it does

You open the UI at `localhost:8000`, create or open a project, and send a message like:

> "Build me a pomodoro timer with a start/pause button and a session counter."

The agent then:

1. **Plans** — thinks through features, UI flow, and the class-name contract shared across all three files.
2. **Writes HTML** — creates `index.html` with semantic structure, all element IDs, initial state classes, and a template comment for dynamically created elements.
3. **Writes JS** — creates `script.js` using the exact IDs from the HTML, with localStorage persistence, modal toggling, and edit-state tracking.
4. **Writes CSS** — creates `styles.css` using the exact class names from the HTML and JS, including all button variants, dynamic element styles, and the required `.hidden` rule.
5. **Validates** — checks that all file links and references are consistent.
6. **Shows you** — streams its reasoning and file writes to the UI as it works.

When it is done, you click **Open HTML** to view the result in your browser.

---

## How the UI works

The interface has three columns:

- **Actions** (left) — every file write and tool call, with expandable code previews.
- **Reasoning** (middle) — the agent's planning thoughts and stage progress.
- **Chat** (right) — your messages and the agent's final summary.

**New Project** creates a folder inside `~/Desktop/lch_workspaces`. All project names must start with `lch_`.

**Open Project** shows a list of your existing `lch_` projects. Click one to switch to it.

**Open HTML** opens the current project's `index.html` (or `main.html`) in your default browser.

---

## The MCP server and tools

The agent does not generate files by calling Python directly. Instead it communicates through a local **MCP server** — a small JSON-over-subprocess protocol that the orchestrator uses to execute actions in a controlled way.

Every tool call goes through the MCP server. The server validates the inputs, enforces the workspace sandbox (the agent cannot touch files outside the project folder), logs the action, and returns a structured result.

### Available tools

| Tool | What it does |
|---|---|
| `create_file` | Write a file to the workspace. Requires a relative path and content. |
| `read_file` | Read a file from the workspace (size-limited). |
| `list_directory` | List files and folders within the workspace. |
| `plan_web_build` | Record a structured development plan as a workspace artifact. |
| `validate_web_app` | Check that HTML links to CSS and JS files correctly and the key files exist. |
| `run_unit_tests` | Run a plain JavaScript test file through Node.js and return the output. |
| `dummy_sandbox_echo` | Debug tool that returns path metadata without touching any files. |

### Guardrails

- **Sandbox enforcement** — every file operation resolves the path inside the workspace root and raises an error if it tries to escape (e.g. `../`).
- **Schema validation** — every tool call is checked against its declared input schema before the handler runs. Unknown fields and wrong types are rejected.
- **Audit log** — every tool invocation (success or failure) is appended as a JSON line to `.low-cortisol-html-logs/tool_actions.log` inside the project folder.
- **Stage restrictions** — during planning the model can only use read and planning tools. During coding it can only use `create_file`. This is enforced per stage, not by prompt alone.

---

## The orchestrator and agent loop

The orchestrator is the Python process that connects the UI, the language model, and the MCP server.

### Four-stage pipeline

The agent works in four sequential stages, each focused on exactly one task:

**Stage 1 — Feature Plan** (`feature_plan`)

The model receives the task and workspace state. It calls `plan_web_build` to produce a structured plan covering: features, UI flow, file connections, and a cross-file class-name contract (which IDs JS will query, which classes CSS will style, which classes JS will toggle on dynamic elements). For existing projects the agent reads current files first.

Allowed tools: `plan_web_build`, `read_file`, `list_directory`

**Stage 2 — HTML** (`html_code`)

The model receives the plan and the HTML skill guide, then writes the complete `index.html`. It must include all element IDs, initial `hidden` classes on modals and panels, a persistent add/create button in the header, and an HTML comment documenting the structure of dynamically created elements.

Allowed tools: `create_file`, `read_file`

**Stage 3 — JavaScript** (`js_code`)

The model receives the plan, the JS skill guide, and the completed HTML (so it can reference exact IDs). It writes the complete `script.js` with localStorage persistence, modal open/close using the `hidden` class only, edit-state tracking via a module-level variable, and event delegation for dynamic elements.

Allowed tools: `create_file`, `read_file`

**Stage 4 — CSS** (`css_code`)

The model receives the plan, the CSS skill guide, the completed HTML, and the completed JS (so it can see every class name in use). It writes the complete `styles.css` with `.hidden { display: none !important; }`, all standard button classes, styles for every dynamic element class, and modal overlay styling.

Allowed tools: `create_file`, `read_file`

After all four stages the orchestrator runs a validation check and streams a summary back to the UI.

### How the model is guided

A few techniques work together to keep the output reliable:

- **Skill files** — each coding stage injects a dedicated skill guide (`skills/html.md`, `skills/js.md`, `skills/css.md`) that defines exact rules, patterns, and anti-patterns for that file type. These cover the cross-file class-name contract, modal patterns, edit-state tracking, and more.
- **Cross-file class contract** — the system enforces that HTML, JS, and CSS all agree on every class name. The only allowed visibility toggle is `hidden`. JS must only toggle classes CSS defines. CSS must style every class JS creates in dynamic elements. Banned alternatives (`is-open`, `show`, `visible`, etc.) are explicitly listed in every skill file and stage prompt.
- **Planner** — before the stages begin, a separate lightweight LLM call produces a rationale and a retrieval query. This helps the tool pruner pick the most relevant tools to show the model.
- **Tool pruning** — tools are ranked by semantic similarity to the current task using text embeddings. Only the most relevant tools are included in the model's context window, reducing noise. A reranker model then fine-tunes the order.
- **Alias normalization** — models sometimes refer to tools by informal names (`edit_file`, `write_file`, `open_file`). The controller maps these to the correct MCP tool names automatically.
- **Inline call extraction** — if a model writes tool calls as raw JSON text instead of structured calls, the controller parses and executes them.
- **XML error retry** — if the model returns a malformed tool call that causes an Ollama XML parse error, the controller retries the stage without tools and extracts any tool calls from the text response.
- **Retry on empty** — if the model returns nothing for a stage, the controller sends a nudge and retries once.
- **Context compaction** — long conversation histories are trimmed so the model does not run out of context mid-task.
- **Project memory** — a semantic index of the workspace files provides file-level retrieval context so the model can orient itself in existing projects.

### Workspace detection

Before planning, the controller checks whether the workspace is empty or already has files:

- **Empty** → the model is told this is a fresh project and creates everything from scratch.
- **Populated** → the existing files and their contents are injected into the model's context so it edits consistently with what is already there.

---

## Setup

### Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS, Windows, or Linux)
- [Ollama](https://ollama.com) running on your host machine

---

### 1. Install and run Ollama

Download Ollama from [ollama.com](https://ollama.com) and install it. Then start it:

```bash
ollama serve
```

On macOS, Ollama runs as a menu bar app after installation. You do not need to run `ollama serve` manually — just make sure the app is open.

Confirm it is running:

```bash
curl http://localhost:11434/api/tags
```

You should see a JSON response listing available models.

---

### 2. Pull the required model

The agent uses `qwen3.5:9b` by default. Pull it with:

```bash
ollama pull qwen3.5:9b
```

This is a 9 GB download. It only needs to be done once. The model is stored locally and used for all subsequent runs.

The agent also uses `nomic-embed-text` for tool selection embeddings:

```bash
ollama pull nomic-embed-text
```

This is a small model (~274 MB) used only for ranking tools, not for generating code.

**Optional fallback model** — if `qwen3.5:9b` fails to load (not enough VRAM), the orchestrator will automatically try `qwen3:7b`. Pull it now to have it ready:

```bash
ollama pull qwen3:7b
```

---

### 3. Create the workspaces folder

All projects are stored in `~/Desktop/lch_workspaces`. Create it now:

```bash
mkdir -p ~/Desktop/lch_workspaces
```

The UI server creates this folder automatically on first launch, but creating it beforehand avoids any timing issues with Docker volume mounts.

---

### 4. Build and start with Docker

From the project root:

```bash
docker compose -f docker/docker-compose.yml up --build
```

This builds the container image, mounts your project source and the `lch_workspaces` folder, and starts the UI server on port 8000.

On subsequent runs (no code changes):

```bash
docker compose -f docker/docker-compose.yml up
```

---

### 5. Open the UI

Go to [http://localhost:8000](http://localhost:8000) in your browser.

Click **New Project**, give your project a name starting with `lch_`, and start chatting.

---

### Running without Docker

If you prefer to run directly on your machine:

```bash
cd ui
pip install -r ../requirements.txt   # if a requirements.txt exists
python server.py
```

The UI server will start on `http://localhost:8000`. Ollama must be running locally at `http://localhost:11434`.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint. In Docker use `http://host.docker.internal:11434` (set automatically). |
| `ORCHESTRATOR_MODEL` | `qwen3.5:9b` | Primary chat model. |
| `ORCHESTRATOR_FALLBACK_MODEL` | `qwen3:7b` | Fallback model if primary is unavailable. |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Embedding model for tool pruning. |
| `ORCHESTRATOR_AGENT_NUM_CTX` | `40000` | LLM context window size. |
| `ORCHESTRATOR_CODE_NUM_PREDICT` | `16384` | Max tokens for the code stage. |
| `UI_HOST` | `127.0.0.1` | UI server bind address (`0.0.0.0` in Docker). |
| `UI_PORT` | `8000` | UI server port. |

---

## Project structure

```
compilot/
├── docker/               Dockerfile and docker-compose.yml
├── docs/phase_docs/      Per-phase implementation notes
├── embeddings/           Cached tool embedding vectors
├── logs/                 Runtime logs (tool pruning, project memory)
├── mcp_server/           MCP server + tool implementations
│   └── tools/            Individual tool modules
├── orchestrator/         Agent logic (loop, planner, reranker, memory)
├── skills/               Skill guides injected into each coding stage
│   ├── html.md           HTML rules: semantics, IDs, state classes, modal pattern
│   ├── js.md             JS rules: modal toggling, edit-state tracking, escapeHtml usage
│   └── css.md            CSS rules: .hidden contract, button classes, dynamic element styles
├── tests/                Guardrail safety tests
└── ui/                   Browser UI (HTML/CSS/JS + Python server)
```

