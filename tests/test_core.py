import json
import sys
from pathlib import Path

from conftest import git

from qa_review import engine, gitdiff, impact, report, security
from qa_review.checks import Check, run_one


def change_pricing(root: Path, commit: bool = False):
    (root / "app" / "pricing.py").write_text(
        "import os\n\n\n"
        "def total(items, discount=0):\n"
        "    os.system('echo ' + str(discount))\n"
        "    return sum(i['price'] for i in items) - discount\n")
    if commit:
        git(root, "commit", "-qam", "discount")


def test_diff_includes_committed_uncommitted_and_untracked(repo):
    change_pricing(repo, commit=True)
    (repo / "app" / "cart.py").write_text((repo / "app" / "cart.py").read_text() + "\n# note\n")
    (repo / "app" / "new.py").write_text("def fresh():\n    return 1\n")
    ch = gitdiff.collect(repo)
    assert ch.base_label == "merge-base with main"
    files = {f.path: f for f in ch.files}
    assert set(files) == {"app/pricing.py", "app/cart.py", "app/new.py"}
    assert files["app/new.py"].status == "added"
    assert (5, "    os.system('echo ' + str(discount))") in files["app/pricing.py"].added


def test_on_main_only_uncommitted_work(repo):
    git(repo, "checkout", "-q", "main")
    assert gitdiff.collect(repo).files == []
    change_pricing(repo)
    assert gitdiff.collect(repo).base_label == "HEAD"


def test_security_rules(repo):
    change_pricing(repo)
    (repo / "app" / "db.py").write_text(
        "def find(cur, name):\n"
        "    cur.execute(f\"SELECT * FROM users WHERE name = '{name}'\")\n"
        "API_KEY = 'abcdef1234567890'\n")
    (repo / "tests" / "test_x.py").write_text("import os\nos.system('ls')\n")
    findings, _ = security.scan(gitdiff.collect(repo))
    rules = {(f.file, f.rule) for f in findings}
    assert ("app/pricing.py", "shell") in rules
    assert ("app/db.py", "sql-interp") in rules
    secret = next(f for f in findings if f.rule == "secret-literal")
    assert secret.severity == "critical" and "abcdef" not in secret.detail
    assert not any(f.file == "tests/test_x.py" for f in findings)   # tests only get secret rules


def test_sensitive_paths():
    assert security.sensitive_path("src/auth/login.py")
    assert security.sensitive_path(".env.local")
    assert not security.sensitive_path("src/pricing.py")


def test_impact_heuristic_finds_symbol_callers_and_tests(repo):
    change_pricing(repo)
    (repo / "app" / "orphan.py").write_text("def lonely():\n    return 0\n")
    imp = impact.analyse(repo, gitdiff.collect(repo))
    assert imp.source == "heuristic"
    assert {s["name"] for s in imp.symbols} == {"total", "lonely"}
    assert any(loc.startswith("app/cart.py:") for loc in imp.callers["total"])
    assert "tests/test_pricing.py" in imp.tests
    assert imp.untested == ["app/orphan.py"]


def test_check_results(tmp_path):
    ok = run_one(Check("ok", "true"), tmp_path, 5)
    bad = run_one(Check("bad", "exit 3"), tmp_path, 5)
    slow = run_one(Check("slow", "sleep 5"), tmp_path, 1)
    missing = run_one(Check("nope", "definitely-not-a-command-xyz"), tmp_path, 5)
    assert (ok.status, bad.status, bad.exit_code, slow.status, missing.status) == ("pass", "fail", 3, "timeout", "error")
    targeted = run_one(Check("t", "echo all", targeted_cmd="echo {targets}"), tmp_path, 5, ["a b.py", "c.py"])
    assert targeted.targeted and "a b.py c.py" in targeted.output


def write_config(root: Path, checks: list[dict]):
    import yaml
    (root / ".qa-review.yaml").write_text(yaml.safe_dump({"checks": checks}))


def test_run_verdicts_and_review(repo):
    change_pricing(repo)
    write_config(repo, [{"name": "tests", "cmd": f"{sys.executable} -m pytest -q", "kind": "tests"},
                        {"name": "style", "cmd": "exit 1", "kind": "lint", "blocking": False}])
    run = engine.run(repo)
    statuses = {c["name"]: c["status"] for c in run["checks"]}
    assert statuses == {"tests": "pass", "style": "fail"}
    assert run["verdict"] == "warn"                      # non-blocking failure + high regex flag
    assert any("os.system" in f.get("detail", "") for f in run["security"]["findings"])
    assert (repo / ".qa-review" / "runs" / f"{run['id']}.json").exists()
    assert (repo / ".qa-review" / ".gitignore").read_text() == "*\n"

    after = engine.submit_review(repo, run["id"], [{"severity": "high", "title": "discount can go negative",
                                                    "file": "app/pricing.py", "line": 6}], "risky", "warn")
    assert after["verdict"] == "block"
    assert "discount can go negative" in report.markdown(after)


def test_blocking_failure_blocks_and_cleared_flag(repo):
    change_pricing(repo)
    write_config(repo, [{"name": "tests", "cmd": "exit 1", "kind": "tests"}])
    run = engine.run(repo)
    assert run["verdict"] == "block" and "check `tests` fail" in run["reasons"]
    write_config(repo, [])
    run = engine.run(repo)
    assert run["verdict"] == "warn"                      # only the shell flag is left
    cleared = engine.submit_review(repo, run["id"], [], "ok", "pass", cleared_rules=["shell"])
    assert cleared["verdict"] == "pass"


def test_quick_level_targets_related_tests(repo):
    change_pricing(repo)
    write_config(repo, [{"name": "tests", "cmd": "echo FULL", "kind": "tests",
                         "targeted_cmd": "echo TARGETED {targets}"}])
    run = engine.run(repo, level="quick")
    assert "TARGETED tests/test_pricing.py" in run["checks"][0]["output"]


def test_auto_detect_python_checks(repo):
    (repo / ".venv" / "bin").mkdir(parents=True)
    exe = repo / ".venv" / "bin" / "pytest"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    from qa_review.checks import detect
    names = [c.name for c in detect(repo)]
    assert "pytest" in names
    assert next(c for c in detect(repo) if c.name == "pytest").cmd.startswith(".venv/bin/pytest")


def test_cli_exit_codes(repo, capsys):
    from qa_review.cli import main
    change_pricing(repo)
    write_config(repo, [{"name": "tests", "cmd": "exit 1", "kind": "tests"}])
    assert main(["run", str(repo)]) == 1
    write_config(repo, [{"name": "tests", "cmd": "true", "kind": "tests"}])
    assert main(["run", str(repo)]) == 0                 # warn passes by default
    assert main(["run", str(repo), "--fail-on", "warn"]) == 1
    assert main(["run", str(repo / "nope")]) == 2
    capsys.readouterr()
    assert main(["report", str(repo), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "warn"


def test_tests_for_naming_conventions():
    tests = ["tests/test_hardening_verify.py", "src/cart.test.ts", "tests/Unit/CartServiceTest.php",
             "tests/test_carts_extra.py", "tests/test_other.py"]
    assert impact._tests_for("aos/verify.py", tests) == ["tests/test_hardening_verify.py"]
    assert "src/cart.test.ts" in impact._tests_for("src/cart.ts", tests)   # extra matches only run more tests
    assert impact._tests_for("src/CartService.php", tests) == ["tests/Unit/CartServiceTest.php"]


def test_main_branch_ignores_unpushed_commits(repo):
    git(repo, "checkout", "-q", "main")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "x.py").write_text("a = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "unpushed")
    assert gitdiff.collect(repo).files == []          # committed-but-unpushed work on main is not "the change"
