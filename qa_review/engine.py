"""The pipeline: changes → security rules → impact → checks + QA → verdict, saved as a run.

The AI review is not done here: qa-review never calls a model. The host agent (Claude Code,
Kiro, an agenticOS reviewer) gets a review packet, reviews, and submits its findings back,
which updates the run's verdict. In CI the deterministic part alone gives the exit code.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from . import checks as checks_mod
from . import gitdiff, report, security, target
from . import impact as impact_mod
from . import qa as qa_mod
from .config import Config, load
from .findings import Finding
from .integrations import agenticos, graphskill

LEVELS = ("quick", "full")


def _runs_dir(cfg: Config) -> Path:
    d = cfg.state_dir / "runs"
    d.mkdir(parents=True, exist_ok=True)
    gi = cfg.state_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    return d


def save(cfg: Config, run: dict) -> None:
    run["verdict"], run["reasons"] = report.verdict(run)
    path = _runs_dir(cfg) / f"{run['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(run, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def load_run(root: str | Path, run_id: str | None = None) -> dict:
    cfg = load(root)
    d = cfg.state_dir / "runs"
    if run_id:
        p = d / f"{run_id}.json"
        if not p.exists():
            raise FileNotFoundError(f"no run {run_id} in {d}")
    else:
        runs = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime_ns) if d.exists() else []
        if not runs:
            raise FileNotFoundError("no runs yet: call qa_run first")
        p = runs[-1]
    return json.loads(p.read_text(encoding="utf-8"))


def integrations_status(cfg: Config) -> dict:
    server, gs_how = graphskill.server_for(cfg.root, cfg.integrations.get("graphskill", "auto"))
    aos, aos_how = agenticos.discover(cfg.root, cfg.integrations.get("agenticos", "auto"))
    return {"graphskill": ("on, " if server else "off, ") + gs_how,
            "agenticos": ("on, " if aos and aos.get("slug") else "off, ") + aos_how}


def run(root: str | Path, base: str | None = None, level: str = "full", paths: list[str] | None = None,
        skip_checks: bool = False, only: list[str] | None = None, host: str | None = None,
        allow_protected: bool = False) -> dict:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    cfg = load(root)
    host = target.normalize(host or cfg.host) if (host or cfg.host) else None
    if host and target.is_protected(host, cfg.protected_hosts) and not allow_protected:
        raise target.HostError(f"{host} is a protected host (protected_hosts in .qa-review.yaml); "
                               "re-run with allow_protected / --allow-protected if you really mean it")
    started = time.monotonic()
    changes = gitdiff.collect(cfg.root, base or cfg.base, paths)
    run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")[:-3] + "-" + secrets.token_hex(2)
    sec_findings, sensitive = security.scan(changes)
    imp = impact_mod.analyse(cfg.root, changes, cfg.integrations.get("graphskill", "auto"))

    planned = checks_mod.plan(cfg)
    qa_checks, qa_notes = qa_mod.plan(cfg, host)
    if level == "full":
        planned += qa_checks
    elif qa_checks:
        qa_notes.append("quick level: e2e/API checks skipped (use level=full)")
    results = []
    if not skip_checks and changes.files:
        targets = None
        if level == "quick":
            code = [f.path for f in changes.files if f.status != "deleted" and security.is_code(f.path)]
            targets = {"tests": imp.tests, "lint": code}
            targets = {k: v for k, v in targets.items() if v}
        results = [r.to_dict() for r in checks_mod.run_all(planned, cfg.root, cfg.timeout_sec, targets, only,
                                                            host, target.env_for(host, cfg.host_env))]
    if not planned and not cfg.checks:
        qa_notes.append("no checks detected: add a `checks:` list to .qa-review.yaml (lint, tests...)")

    data = {
        "id": run_id,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(cfg.root),
        "level": level,
        "host": host,
        "base": changes.base,
        "base_label": changes.base_label,
        "stats": changes.stats(),
        "files": [{"path": f.path, "status": f.status, "added": len(f.added), "removed": f.removed}
                  for f in changes.files],
        "security": {"findings": [f.to_dict() for f in sec_findings], "sensitive_paths": sensitive},
        "impact": imp.to_dict(),
        "checks": results,
        "qa_notes": qa_notes,
        "integrations": integrations_status(cfg),
        "review": None,
        "duration_sec": round(time.monotonic() - started, 1),
    }
    save(cfg, data)
    return data


def submit_review(root: str | Path, run_id: str | None, findings: list[dict], summary: str = "",
                  verdict: str | None = None, cleared_rules: list[str] | None = None,
                  reviewer: str = "agent") -> dict:
    cfg = load(root)
    data = load_run(root, run_id)
    clean = [Finding.from_dict(f, "review").to_dict() for f in findings or []]
    if verdict not in (None, "pass", "warn", "block"):
        raise ValueError("verdict must be pass, warn or block")
    data["review"] = {"reviewer": reviewer, "summary": summary[:2000], "verdict": verdict,
                      "findings": clean, "cleared_rules": list(cleared_rules or []),
                      "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    save(cfg, data)
    return data
