from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ollama_client import OllamaClient


@dataclass
class FileSnapshot:
    relative_path: str
    mtime_ns: int
    size_bytes: int
    summary: str
    embedding: list[float]
    touched_count: int = 0
    change_count: int = 0


class ProjectMemory:
    def __init__(
        self,
        *,
        workspace_root: Path,
        ollama_client: OllamaClient,
        embedding_model: str,
        events_log_path: Path,
    ) -> None:
        self.workspace_root = workspace_root
        self.ollama_client = ollama_client
        self.embedding_model = embedding_model
        self.events_log_path = events_log_path
        self.snapshots: dict[str, FileSnapshot] = {}
        self.max_file_bytes = 200_000
        self.query_embedding_cache: dict[str, list[float]] = {}
        self.max_query_cache_items = 32

    def refresh(self) -> None:
        discovered: set[str] = set()
        for path in sorted(self.workspace_root.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(self.workspace_root).as_posix())
            if self._ignore_path(rel):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue

            discovered.add(rel)
            mtime_ns = int(stat.st_mtime_ns)
            size_bytes = int(stat.st_size)

            existing = self.snapshots.get(rel)
            if existing and existing.mtime_ns == mtime_ns and existing.size_bytes == size_bytes:
                continue

            content = self._safe_read_text(path)
            summary = self._summarize_file(rel, content)
            text_for_embedding = self._embedding_text(rel, summary, content)
            embedding = self.ollama_client.embed(embedding_model=self.embedding_model, text=text_for_embedding)

            touched = existing.touched_count if existing else 0
            changes = (existing.change_count + 1) if existing else 0
            self.snapshots[rel] = FileSnapshot(
                relative_path=rel,
                mtime_ns=mtime_ns,
                size_bytes=size_bytes,
                summary=summary,
                embedding=embedding,
                touched_count=touched,
                change_count=changes,
            )

        stale = [path for path in self.snapshots.keys() if path not in discovered]
        for path in stale:
            self.snapshots.pop(path, None)

    def mark_touched(self, relative_path: str) -> None:
        key = relative_path.strip()
        if not key:
            return
        snap = self.snapshots.get(key)
        if snap is not None:
            snap.touched_count += 1

    def retrieve(self, *, query: str, top_k: int) -> list[dict[str, Any]]:
        if not self.snapshots:
            return []

        query_key = query.strip()
        query_vector = self.query_embedding_cache.get(query_key)
        if query_vector is None:
            query_vector = self.ollama_client.embed(embedding_model=self.embedding_model, text=query_key)
            if len(self.query_embedding_cache) >= self.max_query_cache_items:
                oldest_key = next(iter(self.query_embedding_cache))
                self.query_embedding_cache.pop(oldest_key, None)
            self.query_embedding_cache[query_key] = query_vector
        scored: list[dict[str, Any]] = []
        for snapshot in self.snapshots.values():
            score = _cosine_similarity(query_vector, snapshot.embedding)
            touch_boost = min(snapshot.touched_count * 0.02, 0.12)
            total = score + touch_boost
            scored.append(
                {
                    "relative_path": snapshot.relative_path,
                    "score": total,
                    "base_score": score,
                    "touch_boost": touch_boost,
                    "summary": snapshot.summary,
                    "size_bytes": snapshot.size_bytes,
                }
            )

        scored.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return scored[: max(1, min(top_k, len(scored)))]

    def read_full_file(self, relative_path: str, *, max_bytes: int = 200_000) -> str:
        path = (self.workspace_root / relative_path).resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError:
            return ""
        if not path.exists() or not path.is_file():
            return ""
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        return raw[:max_bytes].decode("utf-8", errors="replace")

    def build_retrieval_context(
        self,
        *,
        retrieved: list[dict[str, Any]],
        include_full_top_n: int = 2,
        max_full_chars: int = 12000,
    ) -> str:
        if not retrieved:
            return "No retrieved files."

        lines: list[str] = ["Retrieved relevant files:"]
        for item in retrieved:
            lines.append(
                f"- {item['relative_path']} (score={item['score']:.4f}, touch_boost={item['touch_boost']:.2f}) :: {item['summary']}"
            )

        lines.append("")
        lines.append("Top file contents (full for high-relevance files):")
        for item in retrieved[: max(0, include_full_top_n)]:
            rel = str(item["relative_path"])
            content = self.read_full_file(rel)
            if len(content) > max_full_chars:
                content = content[:max_full_chars] + f"\n...<trimmed {len(content)-max_full_chars} chars>"
            lines.append(f"--- FILE: {rel} ---")
            lines.append(content or "<empty>")

        return "\n".join(lines)

    def write_event(self, *, stage: str, payload: dict[str, Any]) -> None:
        self.events_log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"stage": stage, "payload": payload}
        with self.events_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _embedding_text(self, rel: str, summary: str, content: str) -> str:
        excerpt = content[:5000]
        return f"path: {rel}\nsummary: {summary}\ncontent_excerpt:\n{excerpt}"

    def _summarize_file(self, rel: str, content: str) -> str:
        stripped = content.strip()
        if not stripped:
            return f"{rel}: empty file"
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        preview = " ".join(lines[:3])
        if len(preview) > 180:
            preview = preview[:180] + "..."
        return f"{rel}: {preview}"

    def _safe_read_text(self, path: Path) -> str:
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        if len(raw) > self.max_file_bytes:
            raw = raw[: self.max_file_bytes]
        return raw.decode("utf-8", errors="replace")

    def _ignore_path(self, rel: str) -> bool:
        parts = rel.split("/")
        ignored_roots = {
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "dist",
            "build",
            "coverage",
            "__pycache__",
            ".low-cortisol-html-logs",
        }
        if any(part in ignored_roots for part in parts):
            return True
        return any(part.startswith(".") for part in parts)


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    length = min(len(vec_a), len(vec_b))
    if length == 0:
        return 0.0

    a = vec_a[:length]
    b = vec_b[:length]
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
