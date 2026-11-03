"""What changed: the diff between a base commit and the working tree, untracked files included.

The default base is the merge-base with the main branch (so a feature branch is reviewed as a
whole, committed or not); on the main branch itself it is HEAD (only uncommitted work).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_MAIN_CANDIDATES = ("main", "master", "origin/main", "origin/master")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
MAX_UNTRACKED_BYTES = 200_000


class GitError(RuntimeError):
    pass


def git(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def is_repo(root: Path) -> bool:
    r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, capture_output=True, text=True, check=False)
    return r.returncode == 0 and r.stdout.strip() == "true"


@dataclass
class FileChange:
    path: str
    status: str                                   # added | modified | deleted | renamed
    added: list[tuple[int, str]] = field(default_factory=list)   # (new line number, text)
    removed: int = 0

    @property
    def changed_lines(self) -> set[int]:
        return {n for n, _ in self.added}


@dataclass
class Changes:
    base: str
    base_label: str
    files: list[FileChange]
    diff: str

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    def stats(self) -> dict:
        return {"files": len(self.files), "added": sum(len(f.added) for f in self.files),
                "removed": sum(f.removed for f in self.files)}


def resolve_base(root: Path, base: str | None = None) -> tuple[str, str]:
    """(sha, label) of the commit to diff against."""
    if base:
        return git(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip(), base
    head = git(root, "rev-parse", "--verify", "HEAD", check=False).strip()
    if not head:
        return "", "(no commits yet)"
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    if branch in ("main", "master"):
        return head, "HEAD"          # on the main branch: only uncommitted work
    for cand in _MAIN_CANDIDATES:
        if git(root, "rev-parse", "--verify", "--quiet", cand, check=False).strip():
            mb = git(root, "merge-base", "HEAD", cand, check=False).strip()
            if mb and mb != head:
                return mb, f"merge-base with {cand}"
    return head, "HEAD"


def parse_diff(diff: str) -> list[FileChange]:
    files: list[FileChange] = []
    cur: FileChange | None = None
    new_line = 0
    old_path = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            cur, old_path = None, None
            continue
        if line.startswith("--- "):
            old_path = None if line[4:].strip() == "/dev/null" else line[4:].strip().removeprefix("a/")
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                cur = FileChange(path=old_path or "?", status="deleted")
            else:
                path = target.removeprefix("b/")
                status = "added" if old_path is None else ("renamed" if old_path != path else "modified")
                cur = FileChange(path=path, status=status)
            files.append(cur)
            continue
        if cur is None:
            continue
        m = _HUNK.match(line)
        if m:
            new_line = int(m.group(1))
            continue
        if line.startswith("+"):
            cur.added.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith("-"):
            cur.removed += 1
        elif line.startswith(" "):
            new_line += 1
    return files


def _untracked(root: Path) -> list[tuple[FileChange, str]]:
    out = []
    for rel in git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        p = root / rel
        try:
            if not p.is_file() or p.stat().st_size > MAX_UNTRACKED_BYTES:
                continue
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = text.splitlines()
        change = FileChange(path=rel, status="added", added=[(i + 1, l) for i, l in enumerate(lines)])
        chunk = (f"diff --git a/{rel} b/{rel}\nnew file (untracked)\n--- /dev/null\n+++ b/{rel}\n"
                 f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{l}\n" for l in lines))
        out.append((change, chunk))
    return out


def collect(root: str | Path, base: str | None = None, paths: list[str] | None = None) -> Changes:
    root = Path(root).resolve()
    if not is_repo(root):
        raise GitError(f"{root} is not a git repository")
    sha, label = resolve_base(root, base)
    pathspec = ["--", *paths] if paths else []
    if sha:
        diff = git(root, "diff", "--no-color", "--no-ext-diff", "-M", sha, *pathspec)
    else:  # no commits: everything staged counts as added
        diff = git(root, "diff", "--no-color", "--no-ext-diff", "--cached", *pathspec)
    files = parse_diff(diff)
    known = {f.path for f in files}
    for change, chunk in _untracked(root):
        if change.path in known:
            continue
        if paths and not any(change.path == p or change.path.startswith(p.rstrip("/") + "/") for p in paths):
            continue
        files.append(change)
        diff += chunk
    return Changes(base=sha, base_label=label, files=files, diff=diff)
