# Phase 1 — Docker Environment

## What was built in this phase

This phase established a containerized, open-source runtime foundation for the local MCP-based HTML coding agent project.

Implemented deliverables:

1. Added Docker image definition for development/runtime.
2. Added Docker Compose configuration for local orchestration.
3. Configured container-to-host networking for local Ollama access.
4. Created `docs/phase_docs/` for iterative phase documentation.
5. Kept workspace handling deferred (as required) so the container can start without a user-defined workspace.

## New files created

- `docker/Dockerfile`
- `docker/docker-compose.yml`
- `docs/phase_docs/.gitkeep`
- `docs/phase_docs/phase_1_docker_environment.md`

## Detailed file/module explanation

### 1) `docker/Dockerfile`

Purpose:
- Defines a reproducible, open-source runtime image for orchestrator + MCP server development.

Key design choices:
- Base image: `python:3.11-slim` (open-source, lightweight, suitable for orchestrator + MCP services).
- Installs web-workflow dependencies required by the roadmap:
   - `nodejs`, `npm` for local JavaScript unit tests
   - `git`, `curl`, `ca-certificates` for local development utilities
- Uses `PYTHONDONTWRITEBYTECODE` and `PYTHONUNBUFFERED` for cleaner container behavior.
- Sets `/app` as working directory and upgrades `pip`.

How it supports later phases:
- Provides Python runtime needed for MCP server and orchestrator modules.
- Provides Node.js runtime needed for plain JS test execution in generated HTML concepts.

### 2) `docker/docker-compose.yml`

Purpose:
- Defines how to run the project container locally in development.

Key fields:
- `build`: builds from project root context using `docker/Dockerfile`.
- `volumes`: mounts project directory into `/app` for live local development.
- `environment`:
  - `OLLAMA_BASE_URL=http://host.docker.internal:11434`
  - `WORKSPACE_ROOT=""` (intentionally empty in Phase 1; workspace is provided later via UI flow).
- `extra_hosts` includes:
  - `host.docker.internal:host-gateway`
  - This keeps host-resolution behavior compatible across environments.
- `stdin_open` and `tty` enabled for interactive debugging.

How it supports later phases:
- Orchestrator can call local Ollama running outside Docker using host networking alias.
- Container starts even when no workspace path exists yet (required by spec).

### 3) `docs/phase_docs/.gitkeep`

Purpose:
- Ensures `docs/phase_docs/` exists in git even before multiple phase docs accumulate.

### 4) `docs/phase_docs/phase_1_docker_environment.md`

Purpose:
- Captures all Phase 1 implementation details for auditability and phase-gated development.

## How this phase works and interacts with other parts

Runtime interaction model established in Phase 1:

1. Developer starts container via Docker Compose.
2. Container has local project files mounted at `/app`.
3. Future orchestrator and MCP services run inside this container.
4. Those services can call Ollama on the host through `host.docker.internal:11434`.
5. Workspace remains undefined at boot and is intended to be provided later by UI validation flow.

This keeps the base infrastructure ready while respecting guardrail and workspace requirements.

## Open-source and cost compliance

All technologies selected in this phase are free and open-source:

- Docker / Docker Compose
- Python (official OSS image)
- Node.js and npm

No paid APIs, proprietary SDKs, or commercial services were introduced.

## Verification notes for Phase 1

Recommended checks:

1. Validate compose file:
   - `docker compose -f docker/docker-compose.yml config`
2. Build image:
   - `docker compose -f docker/docker-compose.yml build`
3. Start container:
   - `docker compose -f docker/docker-compose.yml up -d`
4. Optional Ollama connectivity test from container (when Ollama is running on host):
   - `curl http://host.docker.internal:11434/api/tags`

These checks verify that the container can boot independently of workspace selection and is prepared for subsequent web-agent phases.