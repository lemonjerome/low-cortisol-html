from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from loop_controller import LoopController
from ollama_client import OllamaClient


STATIC_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "dummy_sandbox_echo",
            "description": "Returns metadata for a workspace-relative path while enforcing sandbox boundaries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "Path relative to WORKSPACE_ROOT",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 3 orchestrator skeleton")
    parser.add_argument("--workspace-root", required=True, help="Absolute workspace path")
    parser.add_argument("--task", required=True, help="User task prompt")
    parser.add_argument("--model", default="qwen2.5-coder:14b", help="Ollama model name")
    parser.add_argument("--max-loops", type=int, default=5, help="Maximum orchestrator iterations")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace_root = str(Path(args.workspace_root).expanduser().resolve())
    project_root = Path(__file__).resolve().parent.parent
    ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

    client = OllamaClient(base_url=ollama_base_url)
    controller = LoopController(
        project_root=project_root,
        workspace_root=workspace_root,
        ollama_client=client,
        model_name=args.model,
        tools=STATIC_TOOLS,
        max_loops=args.max_loops,
    )

    health = client.health()
    result = controller.run(args.task)

    print(
        json.dumps(
            {
                "ok": True,
                "phase": "phase_3_orchestrator_skeleton",
                "ollama_base_url": ollama_base_url,
                "ollama_health": health,
                "tools_sent_to_model": STATIC_TOOLS,
                "orchestrator_result": result,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
