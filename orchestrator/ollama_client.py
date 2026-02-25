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

    def list_model_names(self) -> list[str]:
        if self._mock_enabled:
            return ["qwen2.5-coder:14b", "nomic-embed-text"]

        health = self.health()
        if not health.get("ok"):
            raise RuntimeError(f"Unable to query Ollama models: {health}")

        models = health.get("models", [])
        names: list[str] = []
        for model in models:
            if isinstance(model, dict):
                name = model.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
        return names

    def ensure_models_loaded(self, required_models: list[str]) -> dict[str, Any]:
        if self._mock_enabled:
            return {
                "ok": True,
                "mode": "mock",
                "required_models": required_models,
                "pulled_models": [],
            }

        installed = set(self.list_model_names())
        pulled: list[str] = []
        for model in required_models:
            if self._is_model_installed(model=model, installed=installed):
                continue
            self._pull_model(model)
            pulled.append(model)
            installed.update(self.list_model_names())

        return {
            "ok": True,
            "mode": "ollama",
            "required_models": required_models,
            "pulled_models": pulled,
        }

    def warmup_models(self, *, chat_model: str, embedding_model: str) -> dict[str, Any]:
        if self._mock_enabled:
            return {
                "ok": True,
                "mode": "mock",
                "chat_model": chat_model,
                "embedding_model": embedding_model,
            }

        _ = self.chat(
            model=chat_model,
            messages=[{"role": "user", "content": "Reply with READY only."}],
            tools=[],
        )
        _ = self.embed(embedding_model=embedding_model, text="tool pruning warmup")

        return {
            "ok": True,
            "mode": "ollama",
            "chat_model": chat_model,
            "embedding_model": embedding_model,
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

    def embed(self, *, embedding_model: str, text: str) -> list[float]:
        if self._mock_enabled:
            seed = sum(ord(ch) for ch in text)
            return [float((seed + idx) % 101) / 100.0 for idx in range(32)]

        payload = {
            "model": embedding_model,
            "input": text,
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read().decode("utf-8")
                parsed = json.loads(body)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8") if error.fp else ""
            raise RuntimeError(f"Ollama embed HTTP error {error.code}: {detail}") from error
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(f"Ollama embed request failed: {error}") from error

        embeddings = parsed.get("embeddings", [])
        if not isinstance(embeddings, list) or not embeddings:
            raise ValueError("Invalid embed response: missing embeddings")
        vector = embeddings[0]
        if not isinstance(vector, list):
            raise ValueError("Invalid embed response: vector is not a list")

        output: list[float] = []
        for value in vector:
            if isinstance(value, (int, float)):
                output.append(float(value))
        if not output:
            raise ValueError("Invalid embed response: empty embedding vector")
        return output

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

    def _pull_model(self, model: str) -> None:
        payload = {
            "model": model,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/pull",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=7200) as response:
                body = response.read().decode("utf-8")
                parsed = json.loads(body)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8") if error.fp else ""
            raise RuntimeError(f"Ollama pull HTTP error {error.code}: {detail}") from error
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(f"Ollama pull request failed for model '{model}': {error}") from error

        if isinstance(parsed, dict) and parsed.get("error"):
            raise RuntimeError(f"Ollama pull failed for model '{model}': {parsed.get('error')}")

    def _is_model_installed(self, *, model: str, installed: set[str]) -> bool:
        if model in installed:
            return True

        if ":" in model:
            bare = model.split(":", 1)[0]
            return bare in installed

        return f"{model}:latest" in installed

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
