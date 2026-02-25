from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from device_detection import detect_compute_backend
from loop_controller import LoopController
from ollama_client import OllamaClient
from planner import Planner
from reranker import ToolReranker
from tool_pruner import ToolPruner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 6 orchestrator with reason-create-debug loop")
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
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "mps", "cuda", "cpu"],
        help="Compute backend selection policy",
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
    device_info = detect_compute_backend(args.device)
    os.environ["COMPILOT_DEVICE"] = device_info["device"]

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
    tool_catalog = load_tools_from_mcp(project_root=project_root, workspace_root=workspace_root)

    controller = LoopController(
        project_root=project_root,
        workspace_root=workspace_root,
        ollama_client=client,
        model_name=args.model,
        tools=tool_catalog,
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
                "phase": "phase_6_reason_create_debug_loop",
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
                "compute_backend": device_info,
                "orchestrator_result": result,
            }
        )
    )
    return 0


def load_tools_from_mcp(*, project_root: Path, workspace_root: str) -> list[dict[str, Any]]:
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root

    request = {"action": "list_tools"}
    result = subprocess.run(
        [sys.executable, "mcp_server/server.py"],
        cwd=str(project_root),
        input=json.dumps(request),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    output = result.stdout.strip() or result.stderr.strip()
    payload = json.loads(output)
    if not payload.get("ok"):
        raise RuntimeError(f"Unable to load tools from MCP server: {payload}")

    listed = payload.get("result", [])
    if not isinstance(listed, list):
        raise RuntimeError("Invalid list_tools payload")

    catalog: list[dict[str, Any]] = []
    for item in listed:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        description = item.get("description", "")
        input_schema = item.get("input_schema", {})
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(input_schema, dict):
            input_schema = {}

        catalog.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(description),
                    "parameters": input_schema,
                },
            }
        )

    return catalog


if __name__ == "__main__":
    raise SystemExit(main())
