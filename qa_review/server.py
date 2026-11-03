"""MCP server (stdio). One server per project, like graphskill: `qa-review serve <project>`.

Typical flow for the agent:
  qa_run → (fix failing checks) → qa_review_packet → review → qa_submit_review → qa_report
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import checks as checks_mod
from . import engine, gitdiff, report, review, security
from . import impact as impact_mod
from . import qa as qa_mod
from .config import load


def _summary(run: dict) -> dict:
    sec = run.get("security") or {}
    review_done = bool(run.get("review"))
    return {
        "run_id": run["id"],
        "verdict": run["verdict"],
        "reasons": run["reasons"],
        "base": run["base_label"],
        "host": run.get("host"),
        "stats": run["stats"],
        "checks": [{k: c[k] for k in ("name", "kind", "status", "blocking", "duration_sec", "cmd")}
                   | ({"output_tail": "\n".join(c["output"].splitlines()[-15:])}
                      if c["status"] in ("fail", "timeout", "error") else {})
                   for c in run.get("checks", [])],
        "security_flags": sec.get("findings", []),
        "sensitive_paths": sec.get("sensitive_paths", []),
        "impact": {k: (run.get("impact") or {}).get(k) for k in ("source", "tests", "untested", "note")},
        "notes": run.get("qa_notes", []),
        "review": "done" if review_done else "pending",
        "next": None if review_done else
        "Fix blocking checks first. Then call qa_review_packet, review it (ideally in a fresh subagent) "
        "and send the result with qa_submit_review.",
    }


def build(default_root: Path) -> FastMCP:
    mcp = FastMCP("qa-review")

    def root_of(project: str | None) -> Path:
        return Path(project).expanduser().resolve() if project else default_root

    @mcp.tool()
    def qa_status(project: str | None = None) -> dict:
        """What qa-review will run here: detected checks, QA (Playwright/Schemathesis), integrations."""
        cfg = load(root_of(project))
        planned = checks_mod.plan(cfg)
        qa_checks, notes = qa_mod.plan(cfg)
        return {"root": str(cfg.root), "config": (cfg.root / ".qa-review.yaml").exists(),
                "checks": [{"name": c.name, "kind": c.kind, "cmd": c.cmd, "blocking": c.blocking, "source": c.source}
                           for c in planned + qa_checks],
                "notes": notes, "integrations": engine.integrations_status(cfg),
                "default_host": cfg.host, "host_env": cfg.host_env, "protected_hosts": cfg.protected_hosts}

    @mcp.tool()
    def qa_run(base: str | None = None, level: str = "full", paths: list[str] | None = None,
               only: list[str] | None = None, host: str | None = None, allow_protected: bool = False,
               project: str | None = None) -> dict:
        """Run QA on the current change: security rules, impact, checks/tests, e2e/API. level=quick runs
        only tests related to the change and skips e2e. base defaults to the merge-base with main.
        host: URL the tests target (e.g. https://staging.example.com); only=["e2e","api"] to run just those.
        Never set allow_protected unless the user explicitly asked to test that protected host."""
        return _summary(engine.run(root_of(project), base=base, level=level, paths=paths, only=only,
                                   host=host, allow_protected=allow_protected))

    @mcp.tool()
    def qa_review_packet(run_id: str | None = None, max_diff_chars: int = 60000, project: str | None = None) -> str:
        """Everything needed to review the change of a run (latest if omitted): checklist, team rules and
        lessons, security flags, failing checks, impact and the diff."""
        root = root_of(project)
        return review.packet(root, engine.load_run(root, run_id), max_diff_chars)

    @mcp.tool()
    def qa_submit_review(run_id: str, verdict: str, summary: str, findings: list[dict] | None = None,
                         cleared_rules: list[str] | None = None, reviewer: str = "agent",
                         project: str | None = None) -> dict:
        """Record a review. findings: [{severity, title, file, line, detail}]. cleared_rules: security flag
        rules confirmed as false positives. Returns the updated verdict."""
        run = engine.submit_review(root_of(project), run_id, findings or [], summary, verdict, cleared_rules, reviewer)
        return {"run_id": run["id"], "verdict": run["verdict"], "reasons": run["reasons"]}

    @mcp.tool()
    def qa_report(run_id: str | None = None, format: str = "markdown", project: str | None = None):
        """The full report of a run (latest if omitted), as markdown or json."""
        run = engine.load_run(root_of(project), run_id)
        return run if format == "json" else report.markdown(run)

    @mcp.tool()
    def qa_impact(base: str | None = None, project: str | None = None) -> dict:
        """Only the blast radius: changed symbols, their callers, tests to run, untested files. No checks run."""
        root = root_of(project)
        cfg = load(root)
        changes = gitdiff.collect(cfg.root, base or cfg.base)
        return impact_mod.analyse(cfg.root, changes, cfg.integrations.get("graphskill", "auto")).to_dict()

    @mcp.tool()
    def qa_security_scan(base: str | None = None, project: str | None = None) -> dict:
        """Only the fixed security rules over the lines the change adds. Fast, no checks run."""
        cfg = load(root_of(project))
        findings, sensitive = security.scan(gitdiff.collect(cfg.root, base or cfg.base))
        return {"findings": [f.to_dict() for f in findings], "sensitive_paths": sensitive}

    # ---------------------------------------------------------------- UAC
    from .uac import api as uac

    @mcp.tool()
    def uac_sources(project: str | None = None) -> dict:
        """Where acceptance criteria can come from here: Kiro specs, agenticOS board, Jira/Linear (via you)."""
        return uac.list_sources(root_of(project))

    @mcp.tool()
    def uac_draft_packet(source: str, ref: str | None = None, task_text: str | None = None, title: str | None = None,
                         slug: str | None = None, base: str | None = None, project: str | None = None) -> str:
        """Step 1. Task + code changes + rules to write the UACs in Gherkin. source: kiro (ref=spec name),
        agenticos (ref=task id), jira/linear/text (task_text=the issue you read with your connector, ref=key)."""
        return uac.draft_packet(root_of(project), source, ref, task_text, title, slug, base)

    @mcp.tool()
    def uac_save(slug: str, gherkin: str, project: str | None = None) -> dict:
        """Step 2. Validate and save the UACs (one Scenario per UAC, tagged @UAC-n and @e2e/@api)."""
        return uac.save(root_of(project), slug, gherkin)

    @mcp.tool()
    def uac_show(slug: str, project: str | None = None) -> dict:
        """The saved UACs of a slug, parsed."""
        return uac.show(root_of(project), slug)

    @mcp.tool()
    def uac_tests_packet(slug: str, project: str | None = None) -> str:
        """Step 3. Creates the Playwright scaffolding if missing and explains how to write one test per UAC."""
        return uac.tests_packet(root_of(project), slug)

    @mcp.tool()
    def uac_run(slug: str, host: str | None = None, format: str | None = None, allow_protected: bool = False,
                project: str | None = None) -> dict:
        """Step 4. Run the UAC tests against host and write the report with evidence for each UAC.
        format: pdf, html or "pdf,html" (default from .qa-review.yaml). Never set allow_protected unless the
        user explicitly asked to test that protected host."""
        return uac.run(root_of(project), slug, host=host, formats=format, allow_protected=allow_protected)

    @mcp.tool()
    def uac_report(slug: str, run_id: str | None = None, format: str | None = None,
                   project: str | None = None) -> dict:
        """Re-create the report of a UAC run (latest if omitted) in another format, without running the tests."""
        return uac.report_again(root_of(project), slug, run_id, format)

    return mcp


def serve(root: str | Path) -> None:
    build(Path(root).expanduser().resolve()).run()
