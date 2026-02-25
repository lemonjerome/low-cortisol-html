from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_tool_action(*, workspace_root: Path, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
    logs_dir = workspace_root / ".low-cortisol-html-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "tool_actions.log"

    entry = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "arguments": arguments,
        "result_ok": bool(result.get("ok", False)),
        "result": result,
    }

    with log_file.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry) + "\n")
