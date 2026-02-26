from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .sandbox import resolve_path_in_workspace, run_safe_command, validate_relative_path


def scaffold_web_app_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    app_dir = str(arguments.get("app_dir", "concept_app")).strip()
    app_title = str(arguments.get("app_title", "Low Cortisol HTML Concept")).strip()

    validate_relative_path(app_dir)
    target_dir = resolve_path_in_workspace(workspace_root, app_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    index_html = target_dir / "index.html"
    styles_css = target_dir / "styles.css"
    app_js = target_dir / "app.js"
    tests_js = target_dir / "tests.js"

    if not index_html.exists():
        index_html.write_text(
            """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>{title}</title>
    <link rel=\"stylesheet\" href=\"styles.css\" />
  </head>
  <body>
    <main id=\"app\"></main>
    <script src=\"app.js\"></script>
  </body>
</html>
""".format(title=app_title),
            encoding="utf-8",
        )

    if not styles_css.exists():
        styles_css.write_text(
            """* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif; padding: 24px; }
#app { max-width: 960px; margin: 0 auto; }
""",
            encoding="utf-8",
        )

    if not app_js.exists():
        app_js.write_text(
            """const app = document.getElementById('app');
if (app) {
  app.innerHTML = '<h1>Concept Ready</h1><p>Start building your HTML idea.</p>';
}
""",
            encoding="utf-8",
        )

    if not tests_js.exists():
        tests_js.write_text(
            """function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function runTests() {
  const title = 'Low Cortisol HTML Concept';
  assert(typeof title === 'string', 'title should be a string');
  console.log('All tests passed');
}

runTests();
""",
            encoding="utf-8",
        )

    return {
        "ok": True,
        "app_dir": str(target_dir),
        "created_or_verified": [
            str(index_html),
            str(styles_css),
            str(app_js),
            str(tests_js),
        ],
    }


def validate_web_app_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    app_dir = str(arguments.get("app_dir", "concept_app")).strip()
    validate_relative_path(app_dir)
    target_dir = resolve_path_in_workspace(workspace_root, app_dir)

    if not target_dir.exists() or not target_dir.is_dir():
        raise ValueError("app_dir does not exist")

    required_files = ["index.html", "styles.css", "app.js"]
    missing: list[str] = []
    for file_name in required_files:
        path = target_dir / file_name
        if not path.exists() or not path.is_file():
            missing.append(file_name)

    issues: list[str] = []
    if not missing:
        html = (target_dir / "index.html").read_text(encoding="utf-8", errors="replace")
        if "<script src=\"app.js\"" not in html:
            issues.append("index.html does not reference app.js")
        if "<link rel=\"stylesheet\" href=\"styles.css\"" not in html:
            issues.append("index.html does not reference styles.css")

    return {
        "ok": not missing and not issues,
        "app_dir": str(target_dir),
        "missing_files": missing,
        "issues": issues,
    }


def run_unit_tests_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    test_file = str(arguments.get("test_file", "concept_app/tests.js")).strip()
    timeout_seconds = int(arguments.get("timeout_seconds", 30))

    validate_relative_path(test_file)
    test_path = resolve_path_in_workspace(workspace_root, test_file)
    if not test_path.exists() or not test_path.is_file():
        raise ValueError("test_file does not exist")

    file_name = test_path.name.lower()
    if not re.search(r"(test|spec)s?\.js$", file_name):
        raise ValueError("test_file must be a real JS test file (e.g., tests.js or *.test.js)")

    source = test_path.read_text(encoding="utf-8", errors="replace")
    if "assert(" not in source and "test(" not in source:
        raise ValueError("test_file must contain test assertions")

    node_binary = shutil.which("node")
    if node_binary is None:
        return {
            "ok": False,
            "error": {
                "type": "MissingDependency",
                "message": "Node.js is required to run tests.js",
            },
            "test_file": str(test_path),
        }

    result = run_safe_command(
        argv=[node_binary, str(test_path)],
        cwd=workspace_root,
        timeout_seconds=timeout_seconds,
    )
    result["test_file"] = str(test_path)
    return result


def plan_web_build_tool(arguments: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    _ = workspace_root
    summary = str(arguments.get("summary", "")).strip()
    prompt_features = arguments.get("prompt_features", [])
    if not isinstance(prompt_features, list):
        raise ValueError("prompt_features must be an array")

    phases = [
        "Phase 1: clarify app purpose and audience",
        "Phase 2: lock MVP feature list and stretch features",
        "Phase 3: define layout, style, and interaction model",
        "Phase 4: implement HTML structure",
        "Phase 5: implement CSS styling",
        "Phase 6: implement JavaScript behavior",
        "Phase 7: add lightweight unit tests and validation",
        "Phase 8: final polish and completion check",
    ]

    return {
        "ok": True,
        "summary": summary,
        "prompt_features": [str(item) for item in prompt_features],
        "phases": phases,
        "serialized": json.dumps({"summary": summary, "phases": phases}, ensure_ascii=False),
    }
