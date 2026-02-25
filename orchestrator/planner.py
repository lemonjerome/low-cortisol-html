from __future__ import annotations

import json
from typing import Any

from ollama_client import OllamaClient


class Planner:
    def __init__(self, *, ollama_client: OllamaClient, model_name: str) -> None:
        self.ollama_client = ollama_client
        self.model_name = model_name

    def plan_step(
        self,
        *,
        task: str,
        iteration: int,
        recent_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        prompt = self._build_prompt(task=task, iteration=iteration, recent_messages=recent_messages)
        response = self.ollama_client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        message = self.ollama_client.extract_assistant_message(response)
        content = str(message.get("content", ""))
        parsed = self._parse_json(content)
        if parsed is None:
            return {
                "subgoal": f"Iteration {iteration} execution",
                "retrieval_query": task,
                "tool_hints": [],
                "rationale": "Planner fallback due to non-JSON output",
            }
        return {
            "subgoal": str(parsed.get("subgoal", f"Iteration {iteration} execution")),
            "retrieval_query": str(parsed.get("retrieval_query", task)),
            "tool_hints": [str(item) for item in parsed.get("tool_hints", []) if isinstance(item, str)],
            "rationale": str(parsed.get("rationale", "")),
        }

    def _build_prompt(self, *, task: str, iteration: int, recent_messages: list[dict[str, Any]]) -> str:
        recent_lines: list[str] = []
        for item in recent_messages[-4:]:
            role = str(item.get("role", "unknown"))
            content = str(item.get("content", ""))
            recent_lines.append(f"- {role}: {content[:400]}")

        recent_text = "\n".join(recent_lines) if recent_lines else "- none"

        return (
            "You are a planning module for a coding agent. "
            "Given the task and recent execution context, generate only a JSON object with keys: "
            "subgoal (string), retrieval_query (string), tool_hints (array of strings), rationale (string).\n\n"
            f"Task:\n{task}\n\n"
            f"Iteration: {iteration}\n"
            "Recent context:\n"
            f"{recent_text}\n\n"
            "Return JSON only."
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
