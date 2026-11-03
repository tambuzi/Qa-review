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

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (GitError, ConfigError, FileNotFoundError, ValueError) as e:
        print(f"qa-review: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
