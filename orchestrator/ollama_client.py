from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class OllamaClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._mock_enabled = os.environ.get("ORCHESTRATOR_MOCK_TOOLCALL", "0") == "1"
        self._mock_turn = 0

    def health(self) -> dict[str, Any]:
        if self._mock_enabled:
            return {"ok": True, "mode": "mock", "base_url": self.base_url}

        request = urllib.request.Request(
            f"{self.base_url}/api/tags",
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body)
                return {"ok": True, "mode": "ollama", "models": payload.get("models", [])}
        except Exception as error:  # noqa: BLE001
            return {
                "ok": False,
                "mode": "ollama",
                "error": {"type": error.__class__.__name__, "message": str(error)},
            }

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self._mock_enabled:
            return self._mock_chat()

        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8") if error.fp else ""
            raise RuntimeError(f"Ollama HTTP error {error.code}: {detail}") from error
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(f"Ollama request failed: {error}") from error

    def extract_assistant_message(self, response: dict[str, Any]) -> dict[str, Any]:
        message = response.get("message")
        if not isinstance(message, dict):
            raise ValueError("Invalid Ollama response: missing message object")
        return message

    def extract_tool_calls(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        tool_calls = message.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            tool_calls = []

        parsed: list[dict[str, Any]] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function", {})
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            if isinstance(name, str) and name:
                parsed.append({"name": name, "arguments": arguments})

        if parsed:
            return parsed

        content = message.get("content", "")
        if isinstance(content, str):
            fallback = self._parse_tool_call_from_content(content)
            if fallback is not None:
                return [fallback]
        return parsed

    def _parse_tool_call_from_content(self, content: str) -> dict[str, Any] | None:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None

        snippet = content[start : end + 1]
        try:
            payload = json.loads(snippet)
        except json.JSONDecodeError:
            return None

        if not isinstance(payload, dict):
            return None

        name = payload.get("name")
        arguments = payload.get("arguments", {})
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(arguments, dict):
            arguments = {}
        return {"name": name, "arguments": arguments}

    def _mock_chat(self) -> dict[str, Any]:
        if self._mock_turn == 0:
            self._mock_turn += 1
            return {
                "model": "mock",
                "done": True,
                "message": {
                    "role": "assistant",
                    "content": "I will inspect the docs directory first.",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "dummy_sandbox_echo",
                                "arguments": {"relative_path": "docs"},
                            }
                        }
                    ],
                },
            }

        return {
            "model": "mock",
            "done": True,
            "message": {
                "role": "assistant",
                "content": "DONE: tool call executed and response analyzed.",
                "tool_calls": [],
            },
        }
