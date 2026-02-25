from __future__ import annotations

import json
from typing import Any

from ollama_client import OllamaClient


class ToolReranker:
    def __init__(self, *, ollama_client: OllamaClient, model_name: str) -> None:
        self.ollama_client = ollama_client
        self.model_name = model_name

    def rerank(
        self,
        *,
        task: str,
        plan: dict[str, Any],
        candidates: list[dict[str, Any]],
        top_k: int,
    ) -> dict[str, Any]:
        if not candidates:
            return {"selected": [], "report": {"method": "empty", "selected": []}}

        model_scored = self._model_score(task=task, plan=plan, candidates=candidates)
        if model_scored:
            ranked = sorted(model_scored, key=lambda item: item["score"], reverse=True)
            selected = ranked[: max(1, min(top_k, len(ranked)))]
            return {
                "selected": selected,
                "report": {
                    "method": "model_reranker",
                    "selected": [{"name": item["name"], "score": item["score"]} for item in selected],
                },
            }

        heuristic_ranked = sorted(candidates, key=lambda item: item["score"], reverse=True)
        selected = heuristic_ranked[: max(1, min(top_k, len(heuristic_ranked)))]
        return {
            "selected": selected,
            "report": {
                "method": "embedding_fallback",
                "selected": [{"name": item["name"], "score": item["score"]} for item in selected],
            },
        }

    def _model_score(
        self,
        *,
        task: str,
        plan: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        prompt = self._build_prompt(task=task, plan=plan, candidates=candidates)
        response = self.ollama_client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        message = self.ollama_client.extract_assistant_message(response)
        content = str(message.get("content", ""))
        parsed = self._parse_json(content)
        if parsed is None:
            return []

        rankings = parsed.get("rankings")
        if not isinstance(rankings, list):
            return []

        by_name: dict[str, dict[str, Any]] = {item["name"]: item for item in candidates if isinstance(item.get("name"), str)}
        scored: list[dict[str, Any]] = []
        for row in rankings:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            score = row.get("score")
            if not isinstance(name, str) or name not in by_name:
                continue
            if not isinstance(score, (int, float)):
                continue
            item = dict(by_name[name])
            item["score"] = float(score)
            scored.append(item)
        return scored

    def _build_prompt(self, *, task: str, plan: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
        candidate_lines = []
        for item in candidates:
            name = str(item.get("name", ""))
            description = str(item.get("description", ""))
            base_score = float(item.get("score", 0.0))
            candidate_lines.append(
                f"- {name} | base_embedding_score={base_score:.6f} | description={description}"
            )

        candidates_text = "\n".join(candidate_lines)
        plan_text = json.dumps(plan, ensure_ascii=False)

        return (
            "You are a tool reranker for a coding agent.\n"
            "Given task, plan, and candidate tools, return JSON only with this schema:\n"
            '{"rankings":[{"name":"tool_name","score":0.0}],"reason":"short"}\n'
            "Rules: higher score means more relevant now, include only candidate names, score range 0..1.\n\n"
            f"Task:\n{task}\n\n"
            f"Plan:\n{plan_text}\n\n"
            f"Candidates:\n{candidates_text}\n"
        )

    def _parse_json(self, text: str) -> dict[str, Any] | None:
        text = text.strip()
        if not text:
            return None

        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None

        snippet = text[start : end + 1]
        try:
            data = json.loads(snippet)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
