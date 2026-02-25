from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PROJECT_ROOT / "mcp_server" / "server.py"


def call_server(payload: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)

    result = subprocess.run(
        [sys.executable, str(SERVER_PATH)],
        cwd=str(PROJECT_ROOT),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    output = result.stdout.strip() or result.stderr.strip()
    return json.loads(output)


def assert_blocked(name: str, payload: dict[str, Any], workspace_root: Path) -> None:
    response = call_server(payload, workspace_root)
    if response.get("ok") is not False:
        raise AssertionError(f"{name} expected failure but got: {response}")


def main() -> int:
    workspace_root = PROJECT_ROOT

    assert_blocked(
        "path_traversal_create_file",
        {
            "action": "call_tool",
            "tool": "create_file",
            "arguments": {
                "relative_path": "../escape.txt",
                "content": "blocked",
                "overwrite": True,
            },
        },
        workspace_root,
    )

    assert_blocked(
        "absolute_path_read_file",
        {
            "action": "call_tool",
            "tool": "read_file",
            "arguments": {
                "relative_path": "/etc/passwd",
            },
        },
        workspace_root,
    )

    assert_blocked(
        "unexpected_argument_rejected",
        {
            "action": "call_tool",
            "tool": "scaffold_web_app",
            "arguments": {
                "app_dir": "unsafe_demo",
                "unexpected": "blocked",
            },
        },
        workspace_root,
    )

    assert_blocked(
        "wrong_type_rejected",
        {
            "action": "call_tool",
            "tool": "run_unit_tests",
            "arguments": {
                "test_file": "demo_concept_post_edit/tests.js",
                "timeout_seconds": "10",
            },
        },
        workspace_root,
    )

    print("Phase 7 guardrail unsafe-operation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
