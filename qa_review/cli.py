"""Command line: `qa-review run` for humans and CI, `qa-review serve` for MCP, `qa-review init`
to wire a project into Claude Code and Kiro."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, engine, report
from .config import ConfigError
from .gitdiff import GitError

EXIT_PASS, EXIT_FAIL, EXIT_ERROR = 0, 1, 2


def cmd_run(a) -> int:
    only = [x.strip() for x in a.only.split(",") if x.strip()] if a.only else None
    run = engine.run(a.path, base=a.base, level=a.level, skip_checks=a.no_checks, only=only,
                     host=a.host, allow_protected=a.allow_protected)
    print(json.dumps(run, indent=1, default=str) if a.format == "json" else report.markdown(run))
    failing = {"block"} if a.fail_on == "block" else {"block", "warn"}
    return EXIT_FAIL if run["verdict"] in failing else EXIT_PASS


def cmd_serve(a) -> int:
    from .server import serve
    serve(a.path)
    return EXIT_PASS


def cmd_status(a) -> int:
    from . import qa
    from .checks import plan
    from .config import load
    cfg = load(a.path)
    print(f"project: {cfg.root}")
    print(f"config:  {'.qa-review.yaml' if (cfg.root / '.qa-review.yaml').exists() else '(none, auto-detect)'}")
    qa_checks, notes = qa.plan(cfg, cfg.host)
    print(f"host:    {cfg.host or '(none; pass --host)'}  -> env {', '.join(cfg.host_env)}, QA_REVIEW_HOST")
    if cfg.protected_hosts:
        print(f"protected hosts: {', '.join(cfg.protected_hosts)}")
    for c in plan(cfg) + qa_checks:
        print(f"  [{c.kind:6}] {c.name:14} {'blocking' if c.blocking else 'warn    '}  {c.cmd}")
    for n in notes:
        print(f"  note: {n}")
    for k, v in engine.integrations_status(cfg).items():
        print(f"{k}: {v}")
    return EXIT_PASS


def cmd_packet(a) -> int:
    from .review import packet
    print(packet(a.path, engine.load_run(a.path, a.run)))
    return EXIT_PASS


def cmd_report(a) -> int:
    run = engine.load_run(a.path, a.run)
    print(json.dumps(run, indent=1, default=str) if a.format == "json" else report.markdown(run))
    return EXIT_PASS


def cmd_init(a) -> int:
    from .wiring import init
    for line in init(Path(a.path), claude=not a.no_claude, kiro=not a.no_kiro, command=a.command):
        print(line)
    return EXIT_PASS


def cmd_uac(a) -> int:
    from .uac import api as uac
    root = Path(a.path)
    if a.action == "sources":
        print(json.dumps(uac.list_sources(root), indent=1, ensure_ascii=False))
    elif a.action == "draft":
        text = Path(a.text_file).read_text(encoding="utf-8") if a.text_file else None
        print(uac.draft_packet(root, a.source, a.ref, text, a.title, a.slug, a.base))
    elif a.action == "save":
        text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
        print(json.dumps(uac.save(root, a.slug, text), indent=1, ensure_ascii=False))
    elif a.action == "show":
        print(json.dumps(uac.show(root, a.slug), indent=1, ensure_ascii=False))
    elif a.action == "tests":
        print(uac.tests_packet(root, a.slug))
    else:
        if a.action == "run":
            res = uac.run(root, a.slug, host=a.host, formats=a.format, allow_protected=a.allow_protected)
        else:
            res = uac.report_again(root, a.slug, a.run, a.format)
        c = res["counts"]
        print(f"{res['verdict'].upper()}  {c['passed']}/{c['total']} UAC passed"
              + "".join(f", {c[k]} {k.replace('_', ' ')}" for k in ("failed", "flaky", "skipped", "not_covered") if c[k]))
        for u in res["uacs"]:
            print(f"  {u['id']:<7} {u['status']:<12} {u['name']}")
        for kind, path in res["report"].items():
            if kind != "pdf_method":
                print(f"{kind}: {path}")
        for err in res["report_errors"]:
            print(f"report: {err}", file=sys.stderr)
        if a.action == "run":
            return EXIT_PASS if res["verdict"] == "pass" else EXIT_FAIL
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qa-review", description="QA + code review in one step.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="run QA on the current change and print the report")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--base", help="commit/branch to diff against (default: merge-base with main)")
    s.add_argument("--level", choices=engine.LEVELS, default="full")
    s.add_argument("--format", choices=("md", "json"), default="md")
    s.add_argument("--fail-on", choices=("block", "warn"), default="block", help="exit 1 on this verdict or worse")
    s.add_argument("--ci", action="store_true", help="CI mode (same as default flags; kept for clarity)")
    s.add_argument("--no-checks", action="store_true", help="only security rules and impact")
    s.add_argument("--host", help="URL the tests target, e.g. https://staging.example.com or localhost:8000")
    s.add_argument("--only", help="comma-separated check names or kinds, e.g. e2e,api")
    s.add_argument("--allow-protected", action="store_true", help="allow a host listed in protected_hosts")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("serve", help="run the MCP server over stdio for one project")
    s.add_argument("path", nargs="?", default=".")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("status", help="show detected checks and integrations")
    s.add_argument("path", nargs="?", default=".")
    s.set_defaults(fn=cmd_status)

    for name, fn, help_ in (("packet", cmd_packet, "print the review packet of a run"),
                            ("report", cmd_report, "print the report of a run")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("path", nargs="?", default=".")
        s.add_argument("--run", help="run id (default: latest)")
        if name == "report":
            s.add_argument("--format", choices=("md", "json"), default="md")
        s.set_defaults(fn=fn)

    s = sub.add_parser("init", help="wire a project: .qa-review.yaml, Claude Code and Kiro MCP config, skill")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--no-claude", action="store_true")
    s.add_argument("--no-kiro", action="store_true")
    s.add_argument("--command", help="how to start qa-review (default: this Python with -m qa_review)")
    s.set_defaults(fn=cmd_init)

    u = sub.add_parser("uac", help="acceptance criteria: draft, save, tests, run with evidence, report")
    usub = u.add_subparsers(dest="action", required=True)
    s = usub.add_parser("sources", help="where UACs can come from in this project")
    s.add_argument("path", nargs="?", default=".")
    s = usub.add_parser("draft", help="print the packet to draft the UACs of a task")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--source", required=True, help="kiro | agenticos | jira | linear | text")
    s.add_argument("--ref", help="Kiro spec name, agenticOS task id or issue key")
    s.add_argument("--text-file", help="file with the issue text (Jira/Linear/text)")
    s.add_argument("--title")
    s.add_argument("--slug")
    s.add_argument("--base")
    s = usub.add_parser("save", help="validate and save a Gherkin file of UACs")
    s.add_argument("slug")
    s.add_argument("file", help="Gherkin file, or - for stdin")
    s.add_argument("path", nargs="?", default=".")
    for name, help_ in (("show", "show the saved UACs"), ("tests", "print the packet to write the UAC tests")):
        s = usub.add_parser(name, help=help_)
        s.add_argument("slug")
        s.add_argument("path", nargs="?", default=".")
    s = usub.add_parser("run", help="run the UAC tests against a host and write the report")
    s.add_argument("slug")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--host", help="URL to test (default: `host:` in .qa-review.yaml)")
    s.add_argument("--format", help="pdf, html or pdf,html (default: uac.report in .qa-review.yaml)")
    s.add_argument("--allow-protected", action="store_true")
    s = usub.add_parser("report", help="re-create the report of a run in another format")
    s.add_argument("slug")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--run", help="run id (default: latest)")
    s.add_argument("--format", help="pdf, html or pdf,html")
    for s in usub.choices.values():
        s.set_defaults(fn=cmd_uac)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (GitError, ConfigError, FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"qa-review: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
