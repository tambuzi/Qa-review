import sys

import pytest
import yaml

from qa_review import engine, report, target
from qa_review.checks import Check, run_one
from qa_review.cli import main
from qa_review.config import load


def change(root):
    (root / "app" / "pricing.py").write_text("def total(items):\n    return sum(i['price'] for i in items) + 0\n")


def config(root, **data):
    (root / ".qa-review.yaml").write_text(yaml.safe_dump(data))


def test_normalize():
    assert target.normalize("staging.app.com") == "https://staging.app.com"
    assert target.normalize("localhost:8000/") == "http://localhost:8000"
    assert target.normalize("app:8080") == "http://app:8080"                  # docker service name
    assert target.normalize("http://10.0.0.5:3000/api") == "http://10.0.0.5:3000/api"
    for bad in ("", "ftp://x.com", "https://x.com/?a=1", "x.com; rm -rf /", "https://x.com$(id)"):
        with pytest.raises(target.HostError):
            target.normalize(bad)


def test_protected():
    assert target.is_protected("https://miapp.com", ["miapp.com"])
    assert target.is_protected("https://api.miapp.com", ["miapp.com"])
    assert target.is_protected("https://prod-eu.miapp.io", ["prod-*.miapp.io"])
    assert not target.is_protected("https://staging.otra.com", ["miapp.com"])
    assert not target.is_protected("https://notmiapp.com", ["miapp.com"])


def test_check_gets_host_as_env_and_placeholder(tmp_path):
    env = target.env_for("https://staging.app.com", ["BASE_URL", "API_URL"])
    r = run_one(Check("e2e", 'echo "$BASE_URL|$API_URL|$QA_REVIEW_HOST|" {host}'), tmp_path, 5,
                host="https://staging.app.com", env=env)
    assert r.status == "pass"
    assert r.output == "https://staging.app.com|https://staging.app.com|https://staging.app.com| https://staging.app.com"


def test_check_needing_host_is_skipped_without_one(tmp_path):
    r = run_one(Check("e2e", "curl {host}/health"), tmp_path, 5)
    assert r.status == "skipped" and "--host" in r.output


def test_run_with_host(repo):
    change(repo)
    config(repo, checks=[{"name": "unit", "cmd": "true", "kind": "tests"},
                         {"name": "smoke", "cmd": 'test "$BASE_URL" = "https://staging.app.com"', "kind": "e2e"}])
    run = engine.run(repo, host="staging.app.com")
    assert run["host"] == "https://staging.app.com"
    assert {c["name"]: c["status"] for c in run["checks"]} == {"unit": "pass", "smoke": "pass"}
    assert "Target host: `https://staging.app.com`" in report.markdown(run)
    only = engine.run(repo, host="staging.app.com", only=["e2e"])
    assert [c["name"] for c in only["checks"]] == ["smoke"]


def test_default_host_from_config_and_override(repo):
    change(repo)
    config(repo, host="localhost:8000", checks=[{"name": "smoke", "cmd": 'echo "$BASE_URL"', "kind": "e2e"}])
    assert engine.run(repo)["checks"][0]["output"] == "http://localhost:8000"
    assert engine.run(repo, host="https://qa.app.com")["checks"][0]["output"] == "https://qa.app.com"


def test_protected_host_needs_explicit_permission(repo):
    change(repo)
    config(repo, protected_hosts=["miapp.com"], checks=[{"name": "smoke", "cmd": "true", "kind": "e2e"}])
    with pytest.raises(target.HostError, match="protected"):
        engine.run(repo, host="https://www.miapp.com")
    assert engine.run(repo, host="https://www.miapp.com", allow_protected=True)["checks"][0]["status"] == "pass"
    assert main(["run", str(repo), "--host", "miapp.com"]) == 2
    assert main(["run", str(repo), "--host", "miapp.com", "--allow-protected"]) == 0


def test_schemathesis_url_follows_host(repo):
    from qa_review import qa
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "schemathesis").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "schemathesis").chmod(0o755)
    config(repo, qa={"openapi": {"spec": "openapi.yaml", "base_url": "http://localhost:8000"}})
    cfg = load(repo)
    assert qa.plan(cfg)[0][0].cmd.endswith("--url http://localhost:8000")
    assert qa.plan(cfg, "https://staging.app.com")[0][0].cmd.endswith("--url {host}")
    config(repo, qa={"openapi": {"spec": "openapi.yaml"}})                 # no URL: only with a host
    r = run_one(qa.plan(load(repo))[0][0], repo, 5)
    assert r.status == "skipped"


def test_cli_host_and_only(repo, capsys):
    change(repo)
    config(repo, checks=[{"name": "unit", "cmd": "true", "kind": "tests"},
                         {"name": "smoke", "cmd": 'echo "$BASE_URL"', "kind": "e2e"}])
    assert main(["run", str(repo), "--host", "qa.app.com", "--only", "e2e", "--format", "json"]) == 0
    import json
    run = json.loads(capsys.readouterr().out)
    assert run["host"] == "https://qa.app.com" and [c["name"] for c in run["checks"]] == ["smoke"]
    assert main(["run", str(repo), "--host", "bad host"]) == 2


def test_mcp_qa_run_accepts_host(repo):
    import asyncio
    import json
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    change(repo)
    config(repo, checks=[{"name": "smoke", "cmd": 'test "$BASE_URL" = "https://qa.app.com"', "kind": "e2e"}])
    src = str(Path(__file__).resolve().parents[1])

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "qa_review", "serve", str(repo)],
                                       env={"PYTHONPATH": src, "PATH": "/usr/bin:/bin",
                                            "QA_REVIEW_AGENTICOS_HOME": str(repo / "none")})
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("qa_run", {"host": "qa.app.com"})
            return json.loads(res.content[0].text)

    summary = asyncio.run(go())
    assert summary["host"] == "https://qa.app.com"
    assert summary["checks"][0]["status"] == "pass"
