"""The review packet: everything an AI reviewer needs, in one bounded block of text, so the
review is grounded in the diff, the impact, the failing checks and the team's own rules."""

from __future__ import annotations

from pathlib import Path

from . import gitdiff
from .config import load
from .integrations import agenticos
from .security import is_code

BASE_CHECKLIST = [
    "Correctness: does the code do what the change intends? Off-by-one, wrong conditions, wrong defaults.",
    "Edge cases: empty / null / very large input, concurrency, time zones, encodings.",
    "Error handling: failures are handled or surfaced, never swallowed; resources are released.",
    "Callers: every call site listed under Impact still works with the new behaviour or signature.",
    "Tests: the change is covered; a bug fix comes with a test that fails without it.",
    "Simplicity: no dead code, no duplicated logic, names say what things are.",
]
SECURITY_CHECKLIST = [
    "AuthN/AuthZ: every new entry point checks who the caller is and what they may do.",
    "Input validation and injection (SQL, shell, template, path traversal).",
    "Secrets: nothing hard-coded or logged; tokens not exposed in errors or URLs.",
    "Crypto: no MD5/SHA-1 for security, no home-made crypto, TLS verification on.",
]

OUTPUT_FORMAT = """When done, call `qa_submit_review` with:
- run_id: "{run_id}"
- verdict: "pass" | "warn" | "block"
- summary: 1-3 sentences
- findings: list of {{severity: info|low|medium|high|critical, title, file, line, detail}}
  Report only real problems you can point to in the diff; no style nitpicks a linter would catch.
  "high" means it should not merge as is.
- cleared_rules: ids of the automatic security flags below that you checked and are false positives."""


def _bounded_diff(diff: str, max_chars: int) -> tuple[str, bool]:
    """Code files first, docs and lockfiles last; cut at max_chars."""
    blocks, cur = [], []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and cur:
            blocks.append("".join(cur))
            cur = []
        cur.append(line)
    if cur:
        blocks.append("".join(cur))

    def path_of(block: str) -> str:
        first = block.split("\n", 1)[0]
        return first.rsplit(" b/", 1)[-1] if " b/" in first else first

    blocks.sort(key=lambda b: 0 if is_code(path_of(b)) else 1)
    out, size, cut = [], 0, False
    for b in blocks:
        if size + len(b) > max_chars:
            room = max_chars - size
            if room > 500:
                out.append(b[:room] + "\n[... diff truncated ...]\n")
            cut = True
            break
        out.append(b)
        size += len(b)
    return "".join(out), cut


def packet(root: str | Path, run: dict, max_diff_chars: int = 60_000) -> str:
    cfg = load(root)
    changes = gitdiff.collect(cfg.root, run.get("base") or None)
    diff, cut = _bounded_diff(changes.diff, max_diff_chars)
    sec = run.get("security") or {}
    imp = run.get("impact") or {}
    sensitive = bool(sec.get("sensitive_paths") or sec.get("findings"))

    parts = [f"# Code review — run {run['id']}",
             ("Review this change as an independent reviewer. If you wrote it in this session, prefer handing "
              "this packet to a fresh subagent so the review does not inherit the author's assumptions."), ""]
    parts += ["## Checklist", *[f"- {c}" for c in BASE_CHECKLIST]]
    if sensitive:
        parts += ["", "### Security (this change touches sensitive code)", *[f"- {c}" for c in SECURITY_CHECKLIST]]
    rules = list(cfg.review_rules)
    info, _ = agenticos.discover(cfg.root, cfg.integrations.get("agenticos", "auto"))
    lessons = agenticos.lessons(info) if info else []
    org = agenticos.org_rules(info) if info else ""
    if rules:
        parts += ["", "## Project rules (.qa-review.yaml)", *[f"- {r}" for r in rules]]
    if lessons:
        parts += ["", "## Lessons this team learned on this project (agenticOS) — check the change against them"]
        parts += [f"- [{l['status']}, {l['confidence']}] when {l['trigger']}: {l['action']}" for l in lessons]
    if org:
        parts += ["", "## Team rules (agenticOS AGENTS.md)", org.strip()]

    if sec.get("findings"):
        parts += ["", "## Automatic security flags (regex, may be false positives — confirm or clear each)"]
        parts += [f"- rule `{f.get('rule')}` {f.get('severity')}: {f.get('title')} at {f.get('file')}:{f.get('line')}"
                  for f in sec["findings"]]
    failing = [c for c in run.get("checks", []) if c["status"] in ("fail", "timeout", "error")]
    if failing:
        parts += ["", "## Failing checks"]
        for c in failing:
            tail = "\n".join((c.get("output") or "").splitlines()[-20:])
            parts += [f"### {c['name']} ({c['status']}) `{c['cmd']}`", "```", tail, "```"]

    if imp:
        parts += ["", f"## Impact (from {imp.get('source')})"]
        for s in imp.get("symbols", [])[:25]:
            callers = (imp.get("callers") or {}).get(s["name"], [])
            more = f"; called from {len(callers)} place(s): " + ", ".join(callers[:8]) if callers else ""
            parts.append(f"- `{s['name']}` ({s['kind']}) {s['file']}:{s['line_start']}-{s['line_end']}{more}")
        if imp.get("untested"):
            parts.append(f"- No test found for: {', '.join(imp['untested'])}")

    parts += ["", f"## Diff (base: {run.get('base_label')})" + (" — truncated, read the files for the rest" if cut else ""),
              "```diff", diff.rstrip(), "```", "", OUTPUT_FORMAT.format(run_id=run["id"])]
    return "\n".join(parts)
