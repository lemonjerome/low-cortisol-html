from __future__ import annotations

import json
import os
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
        if os.environ.get("ORCHESTRATOR_FAST_MODE", "0") == "1":
            phases = [
                "Plan architecture and milestones",
                "Implement HTML structure and layout",
                "Implement CSS styling and spacing",
                "Implement JavaScript state + CRUD flows",
                "Refine UX interactions and edge cases",
                "Run validation + tests and finalize",
            ]
            phase_index = min(max(iteration - 1, 0), len(phases) - 1)
            return {
                "subgoal": phases[phase_index],
                "retrieval_query": self._normalize_retrieval_query(task, fallback=task),
                "tool_hints": [],
                "rationale": "Fast mode: using deterministic phase plan",
                "app_purpose": "",
                "suggested_features": [],
                "visual_direction": "",
                "interaction_model": "",
                "unit_test_plan": [],
                "development_phases": phases,
                "active_phase": phases[phase_index],
            }

        prompt = self._build_prompt(task=task, iteration=iteration, recent_messages=recent_messages)
        response = self.ollama_client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            stream=True,
            stream_label="planner",
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
                "app_purpose": "",
                "suggested_features": [],
                "visual_direction": "",
                "interaction_model": "",
                "unit_test_plan": [],
                "development_phases": [],
                "active_phase": f"Iteration {iteration}",
            }
        return {
            "subgoal": str(parsed.get("subgoal", f"Iteration {iteration} execution")),
            "retrieval_query": self._normalize_retrieval_query(parsed.get("retrieval_query"), fallback=task),
            "tool_hints": [str(item) for item in parsed.get("tool_hints", []) if isinstance(item, str)],
            "rationale": str(parsed.get("rationale", "")),
            "app_purpose": str(parsed.get("app_purpose", "")),
            "suggested_features": [str(item) for item in parsed.get("suggested_features", []) if isinstance(item, str)],
            "visual_direction": str(parsed.get("visual_direction", "")),
            "interaction_model": str(parsed.get("interaction_model", "")),
            "unit_test_plan": [str(item) for item in parsed.get("unit_test_plan", []) if isinstance(item, str)],
            "development_phases": [str(item) for item in parsed.get("development_phases", []) if isinstance(item, str)],
            "active_phase": str(parsed.get("active_phase", f"Iteration {iteration}")),
        }

    def _normalize_retrieval_query(self, value: Any, *, fallback: str) -> str:
        if isinstance(value, str):
            candidate = value.strip()
            if candidate:
                return candidate
        fallback_text = str(fallback).strip()
        return fallback_text or "html css js local concept app"

    def _build_prompt(self, *, task: str, iteration: int, recent_messages: list[dict[str, Any]]) -> str:
        recent_lines: list[str] = []
        for item in recent_messages[-4:]:
            role = str(item.get("role", "unknown"))
            content = str(item.get("content", ""))
            recent_lines.append(f"- {role}: {content[:400]}")

        recent_text = "\n".join(recent_lines) if recent_lines else "- none"

        return (
            "You are a planning module for an HTML/CSS/JS coding agent. "
            "Think step-by-step and return JSON only. Always include these keys:\n"
            "subgoal (string), retrieval_query (string), tool_hints (array of strings), rationale (string),\n"
            "app_purpose (string), suggested_features (array of strings), visual_direction (string),\n"
            "interaction_model (string), unit_test_plan (array of strings), development_phases (array of strings), active_phase (string).\n"
            "Rules:\n"
            "1) infer app purpose,\n"
            "2) suggest useful features beyond user prompt,\n"
            "3) define look-and-feel,\n"
            "4) connect functionality with layout,\n"
            "5) propose unit tests,\n"
            "6) split implementation into concrete phases before coding.\n"
            "Use only plain HTML/CSS/JS local files (no frameworks).\n\n"
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
