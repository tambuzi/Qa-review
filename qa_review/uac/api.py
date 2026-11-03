"""The UAC workflow, shared by the MCP tools and the CLI:

  uac_sources → uac_draft_packet → (agent writes Gherkin) → uac_save
  → uac_tests_packet → (agent writes Playwright tests) → uac_run → report (pdf / html)
"""

from __future__ import annotations

from pathlib import Path

from ..config import load as load_config
from . import packets, report, runner, sources, store


def list_sources(root: Path) -> dict:
    cfg = load_config(root)
    from ..integrations import agenticos
    info, how = agenticos.discover(cfg.root)
    return {"kiro_specs": sources.kiro_specs(cfg.root),
            "agenticos": {"available": bool(info), "status": how,
                          "usage": "source='agenticos', ref='<task id>'"},
            "jira_linear": "read the issue with your Jira/Linear connector and pass it as task_text "
                           "(source='jira' or 'linear', ref='<issue key>')",
            "saved_uacs": store.all_slugs(cfg.root)}


def resolve_task(root: Path, source: str, ref: str | None = None, task_text: str | None = None,
                 title: str | None = None) -> sources.Task:
    source = (source or "").lower()
    if source == "kiro":
        if not ref:
            specs = [s["ref"] for s in sources.kiro_specs(root)]
            if len(specs) != 1:
                raise sources.SourceError(f"which Kiro spec? pass ref (found: {', '.join(specs) or 'none'})")
            ref = specs[0]
        task = sources.from_kiro(root, ref)
        if task_text:
            task.text += "\n\n## Extra context\n\n" + task_text
        return task
    if source in ("agenticos", "aos", "board"):
        if not ref or not str(ref).lstrip("#").split("#")[-1].isdigit():
            raise sources.SourceError("agenticOS needs ref = the board task id (a number)")
        return sources.from_agenticos(root, int(str(ref).split("#")[-1]))
    return sources.from_text(source or "text", ref or "", title or "", task_text or "")


def default_slug(task: sources.Task) -> str:
    return store.slugify(task.ref or task.title)


def draft_packet(root: Path, source: str, ref: str | None = None, task_text: str | None = None,
                 title: str | None = None, slug: str | None = None, base: str | None = None) -> str:
    root = load_config(root).root
    task = resolve_task(root, source, ref, task_text, title)
    return packets.draft(root, slug or default_slug(task), task, base)


def save(root: Path, slug: str, gherkin_text: str) -> dict:
    root = load_config(root).root
    path, feature = store.save(root, slug, gherkin_text)
    return {"saved": str(path.relative_to(root)), "feature": feature.name,
            "uacs": [{"id": s.uac_id, "name": s.name, "kinds": s.kinds, "source": s.tag_value("source")}
                     for s in feature.scenarios],
            "next": f"uac_tests_packet(slug='{store.slugify(slug)}')"}


def show(root: Path, slug: str) -> dict:
    root = load_config(root).root
    return store.load(root, slug).to_dict()


def tests_packet(root: Path, slug: str) -> str:
    return packets.tests(load_config(root).root, slug)


def run(root: Path, slug: str, host: str | None = None, formats: list[str] | str | None = None,
        allow_protected: bool = False, task_ref: str | None = None, attacks: bool | None = None) -> dict:
    root = load_config(root).root
    if formats:
        formats = store.parse_formats(formats)           # validate before spending time on the run
    data = runner.run(root, slug, host=host, allow_protected=allow_protected, task_ref=task_ref,
                      attacks_on=attacks)
    rep = report.write(root, data, formats)
    return summary(data, rep)


def report_again(root: Path, slug: str, run_id: str | None = None, formats: list[str] | str | None = None) -> dict:
    root = load_config(root).root
    data = runner.load_run(root, slug, run_id)
    return summary(data, report.write(root, data, store.parse_formats(formats) if formats else None))


def iterate_packet(root: Path, slug: str, run_id: str | None = None) -> str:
    root = load_config(root).root
    return packets.iterate(root, slug, runner.load_run(root, slug, run_id))


def summary(data: dict, rep: dict) -> dict:
    battery = data.get("battery") or {}
    return {"run_id": data["id"], "slug": data["slug"], "verdict": data["verdict"], "host": data["host"],
            "counts": data["counts"],
            "robustness": {"break_uacs": data["counts"].get("break_total", 0),
                           "held": data["counts"].get("break_held", 0),
                           "broken": data["counts"].get("break_broken", 0),
                           "attack_gaps": (data.get("coverage") or {}).get("gaps", []),
                           "battery": {k: battery.get(k) for k in ("ran", "reason", "cases", "broken", "note")},
                           "battery_breaks": [f"{r['endpoint']} · {r['label']} → {'; '.join(r['broke'])}"
                                              for r in battery.get("results", []) if r["broke"]][:20]},
            "next": "uac_iterate_packet: attack around what broke and cover the gaps",
            "uacs": [{"id": u["id"], "name": u["name"], "status": u["status"], "proposed": u.get("proposed", False),
                      "tests": [{"title": t["title"], "status": t["status"],
                                 **({"error": t["error"][:500]} if t["error"] else {})} for t in u["tests"]]}
                     for u in data["uacs"]],
            "orphan_tests": [t["title"] for t in data["orphans"]],
            "report": rep["files"], "report_errors": rep["errors"]}
