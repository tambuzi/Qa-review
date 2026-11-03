"""agenticOS integration (read-only): bring the team's rules and the project's learned lessons
into the review, so the reviewer checks what this team has already been corrected on.

Reads files directly; agenticOS does not need to be importable or running.
- ~/.agenticos/config.yaml → `repo` (org rules in <repo>/AGENTS.md) and linked `projects`;
- <data>/lessons/<project>/*.md → lessons with YAML frontmatter (status, confidence, trigger, action).

The other direction (qa-review inside agenticOS) needs no code: set the project's
`verify_command` to `qa-review run --ci` and board workers get QA + checks for free.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

MIN_CONFIDENCE = 0.5


def home() -> Path:
    return Path(os.environ.get("QA_REVIEW_AGENTICOS_HOME") or Path.home() / ".agenticos")


def _yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), parts[2].strip()


def discover(root: Path, setting: str = "auto") -> tuple[dict | None, str]:
    """({repo, data, slug}, how it was found or why not)."""
    if setting == "off":
        return None, "disabled in .qa-review.yaml"
    h = home()
    cfg = _yaml(h / "config.yaml")
    if not cfg:
        return None, f"not installed ({h / 'config.yaml'} not found)"
    data = Path(cfg.get("data_dir") or h / "data").expanduser()
    projects = dict(cfg.get("projects") or {})
    projects.update(_yaml(data / "projects.yaml").get("projects") or {})
    slug = None
    for name, p in projects.items():
        path = p.get("path") if isinstance(p, dict) else p
        if path and Path(str(path)).expanduser().resolve() == root.resolve():
            slug = name
            break
    repo = Path(cfg["repo"]).expanduser() if cfg.get("repo") else None
    how = f"project '{slug}'" if slug else "installed, but this project is not linked (aos link)"
    return {"repo": repo, "data": data, "slug": slug}, how


def org_rules(info: dict, limit: int = 4000) -> str:
    repo = info.get("repo")
    if not repo:
        return ""
    try:
        return (Path(repo) / "AGENTS.md").read_text(encoding="utf-8")[:limit]
    except OSError:
        return ""


def lessons(info: dict, limit: int = 25) -> list[dict]:
    slug = info.get("slug")
    if not slug:
        return []
    out = []
    for f in sorted((Path(info["data"]) / "lessons" / slug).glob("*.md")):
        try:
            meta, body = _frontmatter(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        if meta.get("status") not in ("confirmed", "unreviewed"):
            continue
        try:
            conf = float(meta.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if meta.get("status") != "confirmed" and conf < MIN_CONFIDENCE:
            continue
        out.append({"id": meta.get("id") or f.stem, "status": meta.get("status"), "confidence": conf,
                    "domain": meta.get("domain"), "trigger": meta.get("trigger"),
                    "action": meta.get("action") or body.strip()})
    out.sort(key=lambda l: (l["status"] != "confirmed", -l["confidence"]))
    return out[:limit]
