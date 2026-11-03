"""`qa-review init`: wire a project into Claude Code and Kiro. Idempotent: existing files are
merged (other MCP servers are kept) and never overwritten wholesale."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from .config import CONFIG_NAME

CONFIG_TEMPLATE = """\
# qa-review settings. Everything is optional: without this file checks are auto-detected.
# base: main
# checks:
#   - {name: lint,  cmd: "ruff check .", kind: lint, blocking: false, targeted_cmd: "ruff check {targets}"}
#   - {name: tests, cmd: "pytest -q",    kind: tests, targeted_cmd: "pytest -q {targets}"}
# qa:
#   playwright: {cmd: "npx playwright test --reporter=line"}
#   openapi: {spec: openapi.yaml, base_url: "http://localhost:8000"}
# review:
#   rules:
#     - "Every endpoint validates its input"
# integrations:
#   graphskill: auto   # auto | off | "<command>", e.g. "/path/to/venv/bin/python -m graphskill"
#   agenticos: auto    # auto | off
# timeout_sec: 600
"""

SKILL = """\
---
name: qa-review
description: >
  QA and code review of the current change in one step. Use before saying a change is done,
  before committing or opening a PR, or when asked to "review", "QA", "test this change" or
  "is this ready to merge".
---

# qa-review

1. `qa_run` (level `quick` while iterating, `full` before finishing). It runs the project's checks,
   tests related to the change, e2e/API QA, fixed security rules and the impact analysis.
2. Fix blocking checks and real security flags, then run `qa_run` again.
3. `qa_review_packet` and review it. If you wrote the change, hand the packet to a fresh subagent:
   the reviewer must not inherit the author's assumptions.
4. `qa_submit_review` with verdict, summary, findings and `cleared_rules` for flags that are false positives.
5. `qa_report` and tell the user the verdict and what is left. Never claim "done" on a `block`.
"""

STEERING = """\
---
inclusion: always
---
# QA before done

Before reporting a change as finished, use the `qa-review` MCP tools: `qa_run`, fix what blocks,
`qa_review_packet`, review, `qa_submit_review`, then share `qa_report`. Do not claim done on a `block` verdict.
"""


def _merge_mcp(path: Path, name: str, entry: dict) -> str:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            return f"skipped {path}: not valid JSON, add the '{name}' server by hand"
    servers = data.setdefault("mcpServers", {})
    action = "updated" if name in servers else "added"
    if servers.get(name) == entry:
        return f"unchanged {path}"
    servers[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"{action} '{name}' in {path}"


def _write_new(path: Path, text: str) -> str:
    if path.exists():
        return f"kept existing {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"wrote {path}"


def init(root: Path, claude: bool = True, kiro: bool = True, command: str | None = None) -> list[str]:
    root = Path(root).expanduser().resolve()
    parts = shlex.split(command) if command else [sys.executable, "-m", "qa_review"]
    entry = {"command": parts[0], "args": [*parts[1:], "serve", str(root)], "env": {}}
    out = [_write_new(root / CONFIG_NAME, CONFIG_TEMPLATE)]
    if claude:
        out.append(_merge_mcp(root / ".mcp.json", "qa-review", entry))
        out.append(_write_new(root / ".claude" / "skills" / "qa-review" / "SKILL.md", SKILL))
    if kiro:
        out.append(_merge_mcp(root / ".kiro" / "settings" / "mcp.json", "qa-review", entry))
        out.append(_write_new(root / ".kiro" / "steering" / "qa-review.md", STEERING))
    out.append("agenticOS (optional): to make board workers pass qa-review before 'done', set this project's "
               f"worker.verify_command to: {shlex.join([*parts, 'run', '.', '--ci'])}  "
               "(it runs in the task's worktree)")
    return out
