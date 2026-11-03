"""Acceptance criteria someone already wrote: the existing tests (and .feature files) that exercise the
endpoints of the change. Each test that encodes a business rule is a UAC candidate (`@source:tests`)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

from ..security import is_test
from . import store
from .surface import _PARAM

MAX_BYTES = 300_000
_DECLS = [
    ("python", re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")),
    ("js", re.compile(r"""\b(?:test|it)(?:\.only|\.skip|\.fixme)?\(\s*(['"`])(.+?)\1""")),
    ("php", re.compile(r"\bfunction\s+(test\w+)\s*\(")),
    ("gherkin", re.compile(r"^\s*(?:Scenario Outline|Scenario|Example|Escenario|Esquema del escenario)\s*:\s*(.+)$")),
    ("go", re.compile(r"^\s*func\s+(Test\w+)\s*\(")),
    ("jvm", re.compile(r"\b(?:void|fun)\s+(`[^`]+`|\w+)\s*\(")),
]
_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".php", ".go", ".java", ".kt", ".feature", ".rb")


def _candidate_files(root: Path, exclude: str) -> list[str]:
    r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root,
                       capture_output=True, text=True, check=False)
    out = []
    for f in r.stdout.splitlines():
        p = PurePosixPath(f)
        if f.startswith(exclude) or "node_modules/" in f or not f.endswith(_SUFFIXES):
            continue
        if is_test(f) or p.suffix == ".feature" or any(part in ("e2e", "acceptance", "features", "specs", "cypress")
                                                         for part in p.parts[:-1]):
            out.append(f)
    return out


def _path_regex(path: str) -> re.Pattern:
    any_value = r"[^\"'`/?\s]+"
    body = _PARAM.sub(lambda _m: any_value, re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
    return re.compile(body + r"(?![\w/-])")


def _declarations(lines: list[str]) -> list[tuple[int, str]]:
    decls = []
    for i, line in enumerate(lines, 1):
        for _kind, rx in _DECLS:
            m = rx.search(line)
            if m:
                name = m.groups()[-1].strip().strip("`")
                decls.append((i, name))
                break
    return decls


def find(root: Path, endpoints: list[dict], limit: int = 40) -> list[dict]:
    """[{file, endpoint, tests: [{name, line}]}] for every endpoint some existing test calls."""
    if not endpoints:
        return []
    uac_tests = store.settings(root).tests_dir.strip("/") + "/"
    patterns = {f"{e['method']} {e['path']}": _path_regex(e["path"]) for e in endpoints}
    found = []
    for rel in _candidate_files(Path(root), uac_tests):
        p = Path(root) / rel
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        decls = _declarations(lines)
        for endpoint, rx in patterns.items():
            hits = [i for i, line in enumerate(lines, 1) if rx.search(line)]
            if not hits:
                continue
            names = []
            for h in hits:                       # the test a call belongs to: the nearest declaration above it
                owner = next(((n, name) for n, name in reversed(decls) if n <= h), None)
                if owner and owner not in names:
                    names.append(owner)
            found.append({"file": rel, "endpoint": endpoint,
                          "tests": [{"name": name, "line": n} for n, name in names] or
                                   [{"name": "(calls it outside a named test)", "line": hits[0]}]})
            if len(found) >= limit:
                return found
    return found
