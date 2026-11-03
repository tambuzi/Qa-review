"""Deterministic checks: detect what a project already uses (linters, type checkers, test
runners) and run it. Only tools that are actually installed are proposed."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import target
from .config import Check, Config


@dataclass
class CheckResult:
    name: str
    kind: str
    cmd: str
    status: str            # pass | fail | timeout | error | skipped
    blocking: bool
    exit_code: int | None = None
    duration_sec: float = 0.0
    output: str = ""       # tail of stdout+stderr
    targeted: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _tool(root: Path, name: str) -> str | None:
    """Prefer the project's own environment (.venv, node_modules, vendor) over PATH."""
    for rel in (f".venv/bin/{name}", f"venv/bin/{name}", f"node_modules/.bin/{name}", f"vendor/bin/{name}"):
        p = root / rel
        if p.exists() and os.access(p, os.X_OK):
            return rel
    return name if shutil.which(name) else None


def _node_runner(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
        return "pnpm run"
    if (root / "yarn.lock").exists() and shutil.which("yarn"):
        return "yarn"
    return "npm run"


def detect(root: Path) -> list[Check]:
    checks: list[Check] = []
    # Python
    if any((root / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")):
        if ruff := _tool(root, "ruff"):
            checks.append(Check("ruff", f"{ruff} check .", blocking=False, kind="lint", source="auto",
                                targeted_cmd=f"{ruff} check {{targets}}"))
        has_tests = (root / "tests").is_dir() or (root / "test").is_dir() or any(root.glob("test_*.py"))
        if has_tests and (pytest := _tool(root, "pytest")):
            checks.append(Check("pytest", f"{pytest} -q -x", kind="tests", source="auto",
                                targeted_cmd=f"{pytest} -q -x {{targets}}"))
    # Node
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {})
        except (json.JSONDecodeError, OSError):
            scripts = {}
        run = _node_runner(root)
        for script, kind, blocking in (("lint", "lint", False), ("typecheck", "types", True),
                                       ("type-check", "types", True), ("test", "tests", True)):
            body = scripts.get(script, "")
            if body and "no test specified" not in body:
                checks.append(Check(f"{script}", f"{run} {script}", blocking=blocking, kind=kind, source="auto"))
    # PHP
    if (root / "composer.json").exists():
        if (root / "vendor/bin/phpstan").exists():
            checks.append(Check("phpstan", "vendor/bin/phpstan analyse --no-progress", blocking=False,
                                kind="types", source="auto"))
        if (root / "vendor/bin/phpunit").exists():
            checks.append(Check("phpunit", "vendor/bin/phpunit", kind="tests", source="auto",
                                targeted_cmd="vendor/bin/phpunit {targets}"))
    # Go
    if (root / "go.mod").exists() and shutil.which("go"):
        checks.append(Check("go vet", "go vet ./...", kind="lint", source="auto"))
        checks.append(Check("go test", "go test ./...", kind="tests", source="auto"))
    return checks


def plan(cfg: Config) -> list[Check]:
    return cfg.checks if cfg.checks is not None else detect(cfg.root)


def _tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run_one(check: Check, root: Path, timeout_sec: int, targets: list[str] | None = None,
            host: str | None = None, env: dict[str, str] | None = None) -> CheckResult:
    cmd, targeted = check.cmd, False
    if targets and check.targeted_cmd:
        cmd = check.targeted_cmd.replace("{targets}", " ".join(shlex.quote(t) for t in targets))
        targeted = True
    if target.needs_host(cmd) and not host:
        return CheckResult(check.name, check.kind, cmd, "skipped", check.blocking,
                           output="needs a host: pass --host (or set `host:` in .qa-review.yaml)", targeted=targeted)
    cmd = target.apply(cmd, host)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True,
                                env={**os.environ, **(env or {})})
    except OSError as e:
        return CheckResult(check.name, check.kind, cmd, "error", check.blocking, output=str(e), targeted=targeted)
    try:
        out, _ = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
        return CheckResult(check.name, check.kind, cmd, "timeout", check.blocking,
                           duration_sec=round(time.monotonic() - start, 1),
                           output=_tail(out or "") + f"\n[killed after {timeout_sec}s]", targeted=targeted)
    code = proc.returncode
    status = "pass" if code == 0 else "fail"
    if code == 127:
        status = "error"   # command not found
    if check.kind == "tests" and code == 5 and "pytest" in cmd:
        status = "skipped"  # pytest: no tests collected
    return CheckResult(check.name, check.kind, cmd, status, check.blocking, exit_code=code,
                       duration_sec=round(time.monotonic() - start, 1), output=_tail(out or ""),
                       targeted=targeted)


def run_all(checks: list[Check], root: Path, timeout_sec: int, targets: dict[str, list[str]] | None = None,
            only: list[str] | None = None, host: str | None = None,
            env: dict[str, str] | None = None) -> list[CheckResult]:
    """`targets` maps a check kind (e.g. "tests", "lint") to the files to limit it to."""
    results = []
    for c in checks:
        if only and c.name not in only and c.kind not in only:
            continue
        results.append(run_one(c, root, timeout_sec, (targets or {}).get(c.kind), host, env))
    return results
