"""Blast radius of a change: which symbols changed, who calls them, which tests cover them.

With graphskill: exact symbol ranges and call graph (any language it indexes).
Without it: Python via `ast`, other languages via definition regexes, callers via `git grep`.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from .gitdiff import Changes
from .integrations import graphskill
from .security import is_source, is_test

_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|static\s+|async\s+|final\s+|abstract\s+)*"
    r"(?:function\s+(\w+)|class\s+(\w+)|interface\s+(\w+)|func\s+(?:\([^)]*\)\s*)?(\w+)"
    r"|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>)")
_SKIP_NAMES = {"__init__", "main", "setUp", "tearDown", "constructor", "render", "get", "set", "run"}


@dataclass
class Impact:
    source: str                                   # graphskill | heuristic
    symbols: list[dict] = field(default_factory=list)
    callers: dict[str, list[str]] = field(default_factory=dict)
    affected_files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    untested: list[str] = field(default_factory=list)   # changed code files with no test found
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _python_symbols(text: str, path: str, lines: set[int]) -> list[dict]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if any(node.lineno <= n <= end for n in lines):
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                out.append({"name": node.name, "kind": kind, "file": path, "line_start": node.lineno, "line_end": end})
    return out


def _regex_symbols(text: str, path: str, lines: set[int]) -> list[dict]:
    defs = []
    src = text.splitlines()
    for i, line in enumerate(src, 1):
        m = _DEF.match(line)
        if m:
            name = next(g for g in m.groups() if g)
            kind = "class" if m.group(2) or m.group(3) else "function"
            defs.append((i, name, kind))
    out = []
    for idx, (start, name, kind) in enumerate(defs):
        end = defs[idx + 1][0] - 1 if idx + 1 < len(defs) else len(src)
        if any(start <= n <= end for n in lines):
            out.append({"name": name, "kind": kind, "file": path, "line_start": start, "line_end": end})
    return out


def _local_symbols(root: Path, changes: Changes) -> list[dict]:
    out = []
    for f in changes.files:
        if f.status == "deleted" or not is_source(f.path) or is_test(f.path):
            continue
        try:
            text = (root / f.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        finder = _python_symbols if f.path.endswith(".py") else _regex_symbols
        out += finder(text, f.path, f.changed_lines)
    out.sort(key=lambda s: s["line_end"] - s["line_start"])
    return out[:40]


def _grep_callers(root: Path, name: str, own_file: str, limit: int = 30) -> list[str]:
    if len(name) < 4 or name in _SKIP_NAMES:
        return []
    r = subprocess.run(["git", "grep", "--untracked", "-n", "-I", "-E", "-e", f"(^|[^A-Za-z0-9_$]){re.escape(name)}\\("], cwd=root, capture_output=True, text=True, check=False)
    locs = []
    for line in r.stdout.splitlines():
        path, _, rest = line.partition(":")
        lineno, _, content = rest.partition(":")
        if re.match(r"\s*(async\s+)?(def|function|func|class)\s", content):
            continue  # the definition itself, not a call
        locs.append(f"{path}:{lineno}")
        if len(locs) >= limit:
            break
    return locs


def _tracked_files(root: Path) -> list[str]:
    r = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=False)
    return r.stdout.splitlines()


_WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def _tokens(name: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(name)]


def _tests_for(path: str, all_tests: list[str]) -> list[str]:
    """Tests whose file name mentions the module: test_cart.py, cart.test.ts, CartTest.php,
    test_checkout_cart.py all cover cart.py."""
    p = PurePosixPath(path)
    stem = p.stem if p.stem not in ("__init__", "index", "mod") else p.parent.name
    want = _tokens(stem)
    if not want:
        return []
    hits = []
    for t in all_tests:
        got = _tokens(PurePosixPath(t).name.split(".")[0])
        n = len(want)
        if any(got[i:i + n] == want for i in range(len(got) - n + 1)):
            hits.append(t)
    return hits


def analyse(root: Path, changes: Changes, graph_setting: str = "auto") -> Impact:
    root = Path(root)
    changed_code = {f.path: f.changed_lines for f in changes.files
                    if f.status != "deleted" and is_source(f.path) and not is_test(f.path) and f.added}
    impact = Impact(source="heuristic")
    server, how = graphskill.server_for(root, graph_setting)
    if server and changed_code:
        try:
            g = graphskill.changed_symbols_and_callers(root, server, changed_code)
            impact.source, impact.symbols, impact.callers = "graphskill", g["symbols"], g["callers"]
            impact.note = f"graphskill ({how})"
        except graphskill.GraphUnavailable as e:
            impact.note = f"graphskill unavailable, used heuristics: {e}"
    elif not server:
        impact.note = f"graphskill {how}; used heuristics"
    if impact.source == "heuristic":
        impact.symbols = _local_symbols(root, changes)
        for s in impact.symbols:
            if s["kind"] != "class":
                impact.callers[s["name"]] = _grep_callers(root, s["name"], s["file"])

    affected = set(changed_code)
    for locs in impact.callers.values():
        affected.update(loc.rsplit(":", 1)[0] for loc in locs if loc)
    rel = set()
    for a in affected:
        p = Path(a)
        rel.add(str(p.relative_to(root)) if p.is_absolute() and p.is_relative_to(root) else a)
    impact.affected_files = sorted(rel)

    all_tests = [t for t in _tracked_files(root) if is_test(t) and is_source(t)]
    all_tests += [f.path for f in changes.files if is_test(f.path) and f.status != "deleted" and f.path not in all_tests]
    tests = {t for t in impact.affected_files if is_test(t)}
    tests |= {f.path for f in changes.files if is_test(f.path) and f.status != "deleted"}
    for path in changed_code:
        found = _tests_for(path, all_tests)
        tests.update(found)
        if not found:
            impact.untested.append(path)
    for path in impact.affected_files:
        if path not in changed_code:
            tests.update(_tests_for(path, all_tests))
    impact.tests = sorted(t for t in tests if (root / t).exists())
    return impact
