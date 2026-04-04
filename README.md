# low cortisol html

An AI coding agent that turns a plain-text chat prompt into a working HTML/CSS/JS web app. You describe what you want. The agent plans, writes all three files, validates them, and shows you the result.

Runs in Docker. Uses [Ollama Cloud](https://ollama.com) for model inference — no local GPU required.

---

## What it does

Open the UI at `localhost:8000`, create a project, and send a message like:

> "Build me a pomodoro timer with a start/pause button and a session counter."

The agent then runs a four-stage pipeline:

1. **Plan** — reasons about features, UI flow, and the cross-file class-name contract.
2. **HTML** — writes `index.html` with semantic structure, all element IDs, and initial state classes.
3. **JS** — writes `script.js` referencing the exact IDs from the HTML, with localStorage and modal logic.
4. **CSS** — writes `styles.css` using every class from the HTML and JS, with a design from the approved palette.

After the four stages: validation, a test run, and a written summary.

When it is done, click **Open HTML** to view the result in your browser.

---

## The UI

Three columns run in parallel as the agent works:

- **Actions** (left) — every file write and tool call, with expandable previews.
- **Reasoning** (middle) — live stream of the agent's planning and stage progress.
- **Chat** (right) — your messages and the agent's final summary.

### Header buttons

| Button | What it does |
|---|---|
| **Open HTML** | Opens the project's `index.html` in a new browser tab via the local server. |
| **New Project** | Creates a new folder inside `lch_workspaces/`. Just enter a name — `lch_` is prepended automatically. |
| **Open Project** | Lists all existing `lch_` projects. Click one to load it. |
| **Clear Chat** | Resets the in-memory conversation context (does not delete files). |
| **?** | Opens the tutorial. |

---

## Setup

### Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- An [Ollama Cloud](https://ollama.com) account and API key

No local Ollama installation needed. The model runs on Ollama's cloud infrastructure.

---

### 1. Clone the repo

```bash
git clone <repo-url>
cd low-cortisol-html
```

---

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```
OLLAMA_API_KEY=your_ollama_cloud_api_key_here
OLLAMA_BASE_URL=https://ollama.com
ORCHESTRATOR_MODEL=qwen3-coder-next
```

Your Ollama API key is available in your Ollama Cloud account dashboard.

---

### 3. Create the workspaces folder

```bash
mkdir -p lch_workspaces
touch lch_workspaces/.gitkeep
```

All generated projects are stored here. The folder is gitignored — your work stays local.

---

### 4. Build and start

```bash
docker compose -f docker/docker-compose.yml up --build
```

On subsequent runs (no code changes):

```bash
docker compose -f docker/docker-compose.yml up
```

---

### 5. Open the UI

Go to [http://localhost:8000](http://localhost:8000).

Click **New Project**, enter a name, and start chatting.

---

### Running without Docker

```bash
cd ui
python server.py
```

The server starts at `http://localhost:8000`. Your `.env` must be present in the project root.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_API_KEY` | — | Ollama Cloud API key (required for cloud mode). |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint. Use `https://ollama.com` for cloud. |
| `ORCHESTRATOR_MODEL` | `qwen3.5:9b` | Primary chat model name. |
| `ORCHESTRATOR_FALLBACK_MODEL` | `qwen3:7b` | Fallback if the primary model does not support tool calls. |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Model used for tool-selection embeddings (local only). |
| `ORCHESTRATOR_AGENT_NUM_CTX` | `32768` | Context window size passed to the model. |
| `ORCHESTRATOR_CODE_NUM_PREDICT` | `16384` | Max tokens per coding stage response. |
| `ORCHESTRATOR_MEMORY_CHAR_BUDGET` | `120000` | Character budget before memory compaction triggers. |
| `ORCHESTRATOR_MEMORY_TAIL_COUNT` | `16` | Number of recent messages preserved verbatim after compaction. |
| `ORCHESTRATOR_REACT_MAX_ITERS_<STAGE>` | varies | Per-stage ReAct turn limit override (e.g. `ORCHESTRATOR_REACT_MAX_ITERS_JS_CODE=8`). |
| `UI_HOST` | `127.0.0.1` | UI server bind address (`0.0.0.0` in Docker). |
| `UI_PORT` | `8000` | UI server port. |

---

## Agent pipeline in detail

### ReAct loop

Each stage runs multiple turns, not a single call. On each turn the model can:

- Call `read_file`, `search_files`, or `list_directory` to explore the workspace.
- Call `create_file` to write the complete output file.
- Return plain text (no tool calls) to signal the stage is done.

Turn limits per stage: `feature_plan` 4, `html_code` 5, `js_code` 6, `css_code` 5. Override with environment variables.

### Nudge logic

If a coding stage returns a text plan with no tool calls and hasn't written its primary file yet, the controller sends a correction message and continues the loop rather than advancing. Same pattern applies to the test stage.

### Tool system

The agent communicates with the workspace through a local **MCP server** — a JSON-over-subprocess protocol. All file operations are sandboxed: the agent cannot read or write paths outside the current project folder.

| Tool | What it does |
|---|---|
| `create_file` | Write a complete file to the workspace. |
| `read_file` | Read a file (size-limited). |
| `list_directory` | List workspace contents. |
| `search_files` | Glob pattern + optional content substring search. |
| `plan_web_build` | Record a structured development plan as a workspace artifact. |
| `validate_web_app` | Check file links and key file presence. |
| `run_unit_tests` | Run a plain Node.js test file and return output. |

### Tool selection

Before each stage, the controller uses text embeddings to rank all available tools by similarity to the stage's task, then a reranker model fine-tunes the order. Only the top-ranked tools are included in the model's context window. This reduces noise and keeps the prompt tight.

### Skill system

Each coding stage injects a dedicated skill guide from `skills/`:

| File | Controls |
|---|---|
| `skills/html.md` | Semantic structure, element ID rules, state class contract, modal pattern, required initial `hidden` classes, persistent action button rule (primary add/create must be in the header — never only inside a conditional empty-state section). |
| `skills/js.md` | Modal toggle pattern (`hidden` only), edit-state tracking, event delegation, `escapeHtml` usage rules. |
| `skills/css.md` | Color palette selection (Uncodixify), button classes, dynamic element styles, `.hidden` rule, banned anti-patterns. |
| `skills/test.md` | Node.js `assert`-only tests, what to test and what not to test. |
| `skills/context.md` | Context efficiency rules: avoid re-reading files already in context, write complete files once, keep reasoning concise. |

### Cross-file class contract

HTML is the source of truth. JS must reference only IDs and classes defined in the HTML. CSS must style only classes that exist in the HTML or are created by JS in dynamic elements. The only allowed visibility toggle class is `hidden` — alternatives like `is-open`, `show`, or `visible` are explicitly banned in all skill files and stage prompts.

### Memory and context management

Conversation history is compacted when it exceeds a character budget. The compactor uses structured extraction: it records which files were created, which tool calls were made (tool name + target path, not the KB-sized content), and the first reasoning line per assistant turn. Large stage prompts in the middle of history are collapsed to their label line only.

Critical rules are appended at the **end** of each coding stage prompt in addition to appearing at the start. This is position-aware: attention recall is highest at the beginning and end of a context (U-shaped curve) and lowest in the middle where large skill guides sit.

---

## Project structure

```
low-cortisol-html/
├── docker/
│   ├── Dockerfile           Python 3.11-slim + git + nodejs
│   └── docker-compose.yml   Service definition, env_file, volume mounts
├── docs/phase_docs/         Per-phase implementation notes
├── embeddings/              Cached tool embedding vectors (gitignored at runtime)
├── lch_workspaces/          Generated project folders (gitignored contents)
├── logs/                    Runtime logs (gitignored)
├── mcp_server/
│   ├── server.py            JSON-over-subprocess MCP server
│   ├── tool_registry.py     Tool registration and dispatch
│   └── tools/
│       ├── file_tools.py    create_file, read_file, list_directory, search_files
│       ├── web_tools.py     validate_web_app, run_unit_tests, plan_web_build
│       ├── sandbox.py       Path sandboxing and schema validation
│       └── action_logger.py Audit log writer
├── orchestrator/
│   ├── main_orchestrator.py Entry point: startup, model selection, fallback
│   ├── loop_controller.py   Four-stage pipeline, ReAct loop, memory compaction
│   ├── ollama_client.py     HTTP client for Ollama API (stdlib only, no SDK)
│   ├── planner.py           Pre-pipeline planning call
│   ├── reranker.py          Tool reranker
│   ├── tool_pruner.py       Embedding-based tool selection
│   ├── project_memory.py    Semantic file index for existing projects
│   └── session_memory.py    Message list with structured compaction
├── skills/
│   ├── html.md              HTML rules and cross-file contract
│   ├── js.md                JavaScript rules
│   ├── css.md               CSS rules + Uncodixify design philosophy
│   ├── test.md              Unit test rules
│   └── context.md           Context efficiency rules
├── ui/
│   ├── index.html           Browser UI
│   ├── style.css            UI styles
│   ├── script.js            UI logic (SSE, streaming, modals)
│   └── server.py            Python HTTP server + SSE endpoint
├── .env.example             Template for required environment variables
└── .gitignore
```

---

## Tips

- Be specific about layout, colors, and interactions in your prompt.
- For follow-up changes, just type what to update — the agent reads existing files first and rewrites them completely.
- The agent works best for single-page apps. Complex multi-page sites or framework-based projects are outside its scope.
- If a stage runs out of turns without writing its file, increase the relevant `ORCHESTRATOR_REACT_MAX_ITERS_*` variable.
