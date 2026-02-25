from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from ollama_client import OllamaClient


class ToolPruner:
    def __init__(
        self,
        *,
        ollama_client: OllamaClient,
        embedding_model: str,
        vectors_path: Path,
        pruning_log_path: Path,
    ) -> None:
        self.ollama_client = ollama_client
        self.embedding_model = embedding_model
        self.vectors_path = vectors_path
        self.pruning_log_path = pruning_log_path

    def retrieve_candidates(
        self,
        *,
        query: str,
        tools: list[dict[str, Any]],
        top_n: int,
    ) -> dict[str, Any]:
        vectors = self._load_or_generate_vectors(tools)
        query_vector = self.ollama_client.embed(embedding_model=self.embedding_model, text=query)

        scored: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function", {})
            name = function.get("name") if isinstance(function, dict) else None
            description = function.get("description", "") if isinstance(function, dict) else ""
            if not isinstance(name, str):
                continue
            tool_vector = vectors.get(name)
            if not isinstance(tool_vector, list):
                continue

            score = _cosine_similarity(query_vector, tool_vector)
            scored.append(
                {
                    "name": name,
                    "description": str(description),
                    "score": score,
                    "tool": tool,
                }
            )

        scored.sort(key=lambda item: item["score"], reverse=True)
        limited = scored[: max(1, min(top_n, len(scored)))]

        retrieval_report = {
            "embedding_model": self.embedding_model,
            "top_n": top_n,
            "query": query,
            "candidates": [{"name": item["name"], "score": item["score"]} for item in limited],
            "total_tools": len(tools),
        }
        self.log_event(stage="retrieval", payload=retrieval_report)
        return {
            "candidates": limited,
            "report": retrieval_report,
        }

    def _load_or_generate_vectors(self, tools: list[dict[str, Any]]) -> dict[str, list[float]]:
        existing = self._read_vectors_file()
        vectors_by_tool = existing.get("vectors", {}) if isinstance(existing, dict) else {}
        stored_model = existing.get("embedding_model") if isinstance(existing, dict) else None

        result_vectors: dict[str, list[float]] = {}
        changed = stored_model != self.embedding_model

        for tool in tools:
            function = tool.get("function", {})
            if not isinstance(function, dict):
                continue

            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue

            cached = vectors_by_tool.get(name) if isinstance(vectors_by_tool, dict) else None
            if isinstance(cached, list) and not changed:
                result_vectors[name] = [float(v) for v in cached if isinstance(v, (int, float))]
                continue

            text = _tool_to_text(tool)
            vector = self.ollama_client.embed(embedding_model=self.embedding_model, text=text)
            result_vectors[name] = vector
            changed = True

        if changed:
            self._write_vectors_file(result_vectors)

        return result_vectors

    def _read_vectors_file(self) -> dict[str, Any]:
        if not self.vectors_path.exists():
            return {}
        try:
            parsed = json.loads(self.vectors_path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_vectors_file(self, vectors: dict[str, list[float]]) -> None:
        self.vectors_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "embedding_model": self.embedding_model,
            "vectors": vectors,
        }
        self.vectors_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def log_event(self, *, stage: str, payload: dict[str, Any]) -> None:
        self.pruning_log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "stage": stage,
            "payload": payload,
        }
        with self.pruning_log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")


def _tool_to_text(tool: dict[str, Any]) -> str:
    function = tool.get("function", {})
    if not isinstance(function, dict):
        return json.dumps(tool, ensure_ascii=False)

    name = str(function.get("name", ""))
    description = str(function.get("description", ""))
    parameters = json.dumps(function.get("parameters", {}), ensure_ascii=False, sort_keys=True)
    return f"name: {name}\ndescription: {description}\nparameters: {parameters}"


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    length = min(len(vec_a), len(vec_b))
    if length == 0:
        return 0.0

    a = vec_a[:length]
    b = vec_b[:length]
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
