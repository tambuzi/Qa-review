"""Where acceptance criteria come from.

- **Kiro specs**: `.kiro/specs/<feature>/requirements.md` (user stories + EARS criteria), plus
  design.md and tasks.md for context.
- **agenticOS board**: the task's title and spec, the feature brief and contract, read-only from
  `~/.agenticos/data/board.db`.
- **Jira / Linear / anything else**: qa-review cannot reach them; the agent reads the issue with its
  own connector and passes the text (with its key) to `uac_draft_packet`.
- **The code**: what the change actually does (endpoints, pages, symbols), to complete the criteria.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..integrations import agenticos

_REQ = re.compile(r"^#{2,4}\s*(?:Requirement|Requisito|Req\.?)\s*([\w.-]+)\s*:?\s*(.*)$", re.IGNORECASE)
_STORY = re.compile(r"^\s*\*{0,2}(User Story|Historia de usuario)\*{0,2}\s*:?\*{0,2}\s*(.*)$", re.IGNORECASE)
_AC_HEAD = re.compile(r"^#{2,5}\s*(Acceptance Criteria|Criterios de aceptaci[oó]n)", re.IGNORECASE)
_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.*)$")

# endpoints and pages added or touched by the change
_ROUTE_PATTERNS = [
    re.compile(r"@(?:\w+\.)?(get|post|put|patch|delete|route|api_route)\(\s*[\"']([^\"']+)[\"']"),   # FastAPI/Flask
    re.compile(r"\b(?:app|router|api)\.(get|post|put|patch|delete|all)\(\s*[\"'`]([^\"'`]+)"),        # Express
    re.compile(r"Route::(get|post|put|patch|delete|any|match)\(\s*[\"']([^\"']+)"),                  # Laravel
    re.compile(r"#\[Route\(\s*[\"']([^\"']+)[\"'][^\]]*?(?:methods:\s*\[\s*[\"'](\w+))?"),            # Symfony
    re.compile(r"@(Get|Post|Put|Patch|Delete)(?:Mapping)?\(\s*[\"']?([^\"')]*)"),                     # Nest/Spring
    re.compile(r"\bpath\(\s*[\"']([^\"']*)[\"']"),                                                    # Django
]
_UI_EXT = (".tsx", ".jsx", ".vue", ".svelte", ".html", ".htm", ".twig", ".erb", ".hbs", ".ejs", ".pug", ".njk",
           ".blade.php", ".astro")
_UI_DIRS = re.compile(r"(^|/)(pages|views|templates|screens|components)/")
_BACKEND_EXT = (".py", ".go", ".java", ".kt", ".rb", ".rs", ".cs")


def is_ui_file(path: str) -> bool:
    """A file that renders UI: by extension, or inside a UI folder unless it is backend code."""
    p = path.lower()
    return p.endswith(_UI_EXT) or (bool(_UI_DIRS.search(p)) and not p.endswith(_BACKEND_EXT))


class SourceError(ValueError):
    pass


@dataclass
class Task:
    source: str                  # kiro | agenticos | jira | linear | text
    ref: str                     # spec name, task id, issue key
    title: str
    text: str                    # everything the agent should read, as markdown
    criteria: list[dict] = field(default_factory=list)   # pre-existing criteria found in the source

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ Kiro
def kiro_specs(root: Path) -> list[dict]:
    base = root / ".kiro" / "specs"
    if not base.is_dir():
        return []
    out = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        files = [f for f in ("requirements.md", "design.md", "tasks.md") if (d / f).exists()]
        if files:
            out.append({"source": "kiro", "ref": d.name, "files": files})
    return out


def parse_kiro_requirements(text: str) -> list[dict]:
    """[{requirement, story, criterion_id, text}] from a Kiro requirements.md."""
    out, req, title, story, in_ac, n = [], None, "", "", False, 0
    for line in text.splitlines():
        if m := _REQ.match(line.strip()):
            req, title, story, in_ac, n = m.group(1), m.group(2).strip(), "", False, 0
            continue
        if req is None:
            continue
        if m := _STORY.match(line):
            story = m.group(2).strip()
            continue
        if _AC_HEAD.match(line.strip()):
            in_ac = True
            continue
        if line.startswith("#"):
            in_ac = False
            continue
        if in_ac and (m := _ITEM.match(line)):
            n += 1
            out.append({"requirement": req, "requirement_title": title, "story": story,
                        "id": f"{req}.{n}", "text": m.group(1).strip()})
    return out


def from_kiro(root: Path, spec: str) -> Task:
    d = root / ".kiro" / "specs" / spec
    if not d.is_dir():
        known = ", ".join(s["ref"] for s in kiro_specs(root)) or "none"
        raise SourceError(f"no Kiro spec '{spec}' in .kiro/specs (found: {known})")
    parts, criteria = [], []
    for name in ("requirements.md", "design.md", "tasks.md"):
        p = d / name
        if p.exists():
            body = p.read_text(encoding="utf-8")
            if name == "requirements.md":
                criteria = parse_kiro_requirements(body)
            limit = 20_000 if name == "requirements.md" else 8_000
            parts.append(f"## {name}\n\n{body[:limit]}")
    return Task(source="kiro", ref=spec, title=spec.replace("-", " "), text="\n\n".join(parts), criteria=criteria)


# ------------------------------------------------------------------ agenticOS
def from_agenticos(root: Path, task_id: int) -> Task:
    info, how = agenticos.discover(root)
    if not info:
        raise SourceError(f"agenticOS {how}")
    db = Path(info["data"]) / "board.db"
    if not db.exists():
        raise SourceError(f"no agenticOS board at {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        t = con.execute("SELECT id, feature, project, title, spec FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
        f = con.execute("SELECT slug, title FROM features WHERE slug = ?", (t["feature"],)).fetchone() if t else None
    finally:
        con.close()
    if not t:
        raise SourceError(f"no task {task_id} on the agenticOS board")
    fdir = Path(info["data"]) / "features" / t["feature"]
    brief = (fdir / "brief.md").read_text(encoding="utf-8") if (fdir / "brief.md").exists() else ""
    contract = (fdir / "contract.md").read_text(encoding="utf-8") if (fdir / "contract.md").exists() else ""
    text = (f"## Task {t['id']} ({t['project']}): {t['title']}\n\n{t['spec']}\n\n"
            f"## Feature {t['feature']}: {f['title'] if f else ''}\n\n{brief[:8000]}"
            + (f"\n\n## Contract\n\n{contract[:8000]}" if contract.strip() else ""))
    return Task(source="agenticos", ref=f"{t['feature']}#{t['id']}", title=t["title"], text=text)


# ------------------------------------------------------------------ Jira / Linear / free text
def from_text(source: str, ref: str, title: str, text: str) -> Task:
    if not (text or "").strip():
        raise SourceError("task_text is empty: paste the issue (description, acceptance criteria, comments)")
    return Task(source=source or "text", ref=ref or "", title=title or ref or "task", text=text[:30_000])


# ------------------------------------------------------------------ code
def code_signals(changes) -> dict:
    """Endpoints and pages the change adds or touches: what the criteria must cover."""
    endpoints, pages = [], []
    for f in changes.files:
        if f.status == "deleted":
            continue
        if is_ui_file(f.path):
            pages.append(f.path)
        for n, line in f.added:
            for rx in _ROUTE_PATTERNS:
                m = rx.search(line)
                if not m:
                    continue
                groups = [g for g in m.groups() if g]
                path = next((g for g in groups if g.startswith("/") or "<" in g or "{" in g), groups[-1])
                method = next((g.upper() for g in groups if g.lower() in
                               ("get", "post", "put", "patch", "delete", "all", "any", "route", "api_route", "match")
                               ), "ANY")
                endpoints.append({"method": method, "path": path, "file": f.path, "line": n})
                break
    return {"endpoints": endpoints, "pages": sorted(set(pages))}
