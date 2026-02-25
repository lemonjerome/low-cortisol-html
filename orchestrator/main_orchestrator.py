from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from loop_controller import LoopController
from ollama_client import OllamaClient
from planner import Planner
from reranker import ToolReranker
from tool_pruner import ToolPruner


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
    parser = argparse.ArgumentParser(description="Phase 4 orchestrator with embedding-based tool pruning")
    parser.add_argument("--workspace-root", required=True, help="Absolute workspace path")
    parser.add_argument("--task", required=True, help="User task prompt")
    parser.add_argument("--model", default="qwen2.5-coder:14b", help="Ollama model name")
    parser.add_argument(
        "--embedding-model",
        default=os.environ.get("EMBEDDING_MODEL", "nomic-embed-text"),
        help="Ollama embedding model used for tool pruning",
    )
    parser.add_argument("--top-k-tools", type=int, default=5, help="Top-K tools to send after pruning")
    parser.add_argument(
        "--candidate-pool-size",
        type=int,
        default=8,
        help="Top-N embedding candidates before model reranking",
    )
    parser.add_argument("--max-loops", type=int, default=5, help="Maximum orchestrator iterations")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace_root = str(Path(args.workspace_root).expanduser().resolve())
    project_root = Path(__file__).resolve().parent.parent
    ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    vectors_path = project_root / "embeddings" / "tool_vectors.json"
    pruning_log_path = project_root / "logs" / "tool_pruning.log"

    client = OllamaClient(base_url=ollama_base_url)
    preload = client.ensure_models_loaded([args.model, args.embedding_model])
    warmup = client.warmup_models(chat_model=args.model, embedding_model=args.embedding_model)

    pruner = ToolPruner(
        ollama_client=client,
        embedding_model=args.embedding_model,
        vectors_path=vectors_path,
        pruning_log_path=pruning_log_path,
    )
    planner = Planner(ollama_client=client, model_name=args.model)
    reranker = ToolReranker(ollama_client=client, model_name=args.model)

    controller = LoopController(
        project_root=project_root,
        workspace_root=workspace_root,
        ollama_client=client,
        model_name=args.model,
        tools=STATIC_TOOLS,
        planner=planner,
        reranker=reranker,
        tool_pruner=pruner,
        top_k_tools=args.top_k_tools,
        candidate_pool_size=args.candidate_pool_size,
        max_loops=args.max_loops,
    )

    health = client.health()
    result = controller.run(args.task)

    print(
        json.dumps(
            {
                "ok": True,
                "phase": "phase_4_embedded_tool_pruning",
                "ollama_base_url": ollama_base_url,
                "ollama_health": health,
                "model_preload": preload,
                "model_warmup": warmup,
                "planner": {"enabled": True, "model": args.model},
                "reranker": {"enabled": True, "model": args.model},
                "tool_pruning": {
                    "enabled": True,
                    "embedding_model": args.embedding_model,
                    "top_k_tools": args.top_k_tools,
                    "candidate_pool_size": args.candidate_pool_size,
                    "vectors_path": str(vectors_path),
                    "log_path": str(pruning_log_path),
                },
                "orchestrator_result": result,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
