import os
import subprocess
import sys
from pathlib import Path

import pytest


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    # never pick up the developer's real agenticOS or graphskill
    monkeypatch.setenv("QA_REVIEW_AGENTICOS_HOME", str(tmp_path / "no-agenticos"))
    monkeypatch.delenv("QA_REVIEW_GRAPHSKILL", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                                               if not (Path(p) / "graphskill").exists()))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


@pytest.fixture
def repo(tmp_path) -> Path:
    """A small Python project on `main`, then a feature branch with an uncommitted change."""
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='proj'\nversion='0'\n")
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "pricing.py").write_text(
        "def total(items):\n"
        "    return sum(i['price'] for i in items)\n")
    (root / "app" / "cart.py").write_text(
        "from app.pricing import total\n\n\n"
        "def checkout(items):\n"
        "    return {'total': total(items)}\n")
    (root / "tests" / "test_pricing.py").write_text(
        "from app.pricing import total\n\n\n"
        "def test_total():\n"
        "    assert total([{'price': 2}, {'price': 3}]) == 5\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    git(root, "checkout", "-q", "-b", "feature")
    return root


@pytest.fixture
def python():
    return sys.executable
