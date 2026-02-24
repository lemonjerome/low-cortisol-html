from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionMemory:
    messages: list[dict[str, Any]] = field(default_factory=list)

    def add(self, role: str, content: str, **extra: Any) -> None:
        message: dict[str, Any] = {"role": role, "content": content}
        message.update(extra)
        self.messages.append(message)
