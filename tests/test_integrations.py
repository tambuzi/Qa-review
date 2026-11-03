import asyncio
import json
import sys
from pathlib import Path

import yaml

from qa_review import engine, gitdiff, impact, review
from qa_review.integrations import agenticos, graphskill
from qa_review.wiring import init

FAKE = str(Path(__file__).parent / "fake_graphskill.py")


def change(root):
    (root / "app" / "pricing.py").write_text(
        "import os\n\n\n"
        "def total(items, discount=0):\n"
        "    os.system('echo ' + str(discount))\n"
        "    return sum(i['price'] for i in items) - discount\n")


# ---------------------------------------------------------------- graphskill
def test_graphskill_from_project_mcp_json(repo):
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"graphskill": {"command": sys.executable, "args": [FAKE]}}}))
    change(repo)
    imp = impact.analyse(repo, gitdiff.collect(repo))
    assert imp.source == "graphskill", imp.note
    assert imp.symbols[0]["name"] == "total"
    assert imp.callers["total"] == ["app/cart.py:4", "app/billing/invoice.py:10"]
    assert "app/billing/invoice.py" in imp.affected_files


def test_graphskill_off_and_broken_fall_back(repo, monkeypatch):
    change(repo)
    server, how = graphskill.server_for(repo, "off")
    assert server is None and "disabled" in how
    monkeypatch.setenv("QA_REVIEW_GRAPHSKILL", "/nonexistent/python -m graphskill")
    imp = impact.analyse(repo, gitdiff.collect(repo))
    assert imp.source == "heuristic" and "unavailable" in imp.note
    assert imp.symbols[0]["name"] == "total"            # heuristics still did the job


def test_graphskill_from_config_command(repo):
    (repo / ".qa-review.yaml").write_text(yaml.safe_dump({"checks": [], "integrations": {"graphskill": f"{sys.executable} {FAKE}"}}))
    change(repo)
    run = engine.run(repo)
    assert run["impact"]["source"] == "graphskill"
    assert run["integrations"]["graphskill"].startswith("on")


# ---------------------------------------------------------------- agenticOS
def fake_agenticos(home: Path, project: Path):
    (home / "data" / "lessons" / "proj").mkdir(parents=True)
    repo = home / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Org rules\n- Money is always integer cents.\n")
    (home / "config.yaml").write_text(yaml.safe_dump({"repo": str(repo), "projects": {"proj": {"path": str(project)}}}))
    lessons = home / "data" / "lessons" / "proj"
    for lid, status, conf, action in (("cents", "confirmed", 0.9, "Use integer cents for prices"),
                                      ("weak", "unreviewed", 0.3, "too weak to show"),
                                      ("gone", "rejected", 0.9, "rejected lesson")):
        (lessons / f"{lid}.md").write_text(
            "---\n" + yaml.safe_dump({"id": lid, "status": status, "confidence": conf, "domain": "api",
                                      "trigger": "touching prices", "action": action}) + "---\n" + action + "\n")


def test_agenticos_lessons_and_rules_reach_the_packet(repo, tmp_path, monkeypatch):
    home = tmp_path / "aos"
    fake_agenticos(home, repo)
    monkeypatch.setenv("QA_REVIEW_AGENTICOS_HOME", str(home))
    info, how = agenticos.discover(repo)
    assert info["slug"] == "proj" and "proj" in how
    assert [l["id"] for l in agenticos.lessons(info)] == ["cents"]
    (repo / ".qa-review.yaml").write_text(yaml.safe_dump({"checks": [], "review": {"rules": ["No floats for money"]}}))
    change(repo)
    run = engine.run(repo)
    text = review.packet(repo, run)
    assert "Use integer cents for prices" in text
    assert "too weak" not in text and "rejected lesson" not in text
    assert "Money is always integer cents" in text
    assert "No floats for money" in text
    assert "rule `shell`" in text and f'run_id: "{run["id"]}"' in text
    assert "```diff" in text and "os.system" in text


def test_agenticos_absent_or_unlinked(repo, tmp_path, monkeypatch):
    info, how = agenticos.discover(repo)
    assert info is None and "not installed" in how
    home = tmp_path / "aos2"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"repo": str(tmp_path), "projects": {}}))
    monkeypatch.setenv("QA_REVIEW_AGENTICOS_HOME", str(home))
    info, how = agenticos.discover(repo)
    assert info["slug"] is None and "not linked" in how
    assert agenticos.lessons(info) == []


# ---------------------------------------------------------------- wiring
def test_init_merges_configs(repo):
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"graphskill": {"command": "x"}}}))
    lines = init(repo, command="qa-review")
    data = json.loads((repo / ".mcp.json").read_text())
    assert set(data["mcpServers"]) == {"graphskill", "qa-review"}
    assert data["mcpServers"]["qa-review"] == {"command": "qa-review", "args": ["serve", str(repo)], "env": {}}
    kiro = json.loads((repo / ".kiro" / "settings" / "mcp.json").read_text())
    assert "qa-review" in kiro["mcpServers"]
    assert (repo / ".claude" / "skills" / "qa-review" / "SKILL.md").exists()
    assert (repo / ".kiro" / "steering" / "qa-review.md").exists()
    master = (repo / ".claude" / "skills" / "qa-master" / "SKILL.md").read_text()
    assert master.startswith("---\nname: qa-master\n") and "### Triage every red" in master
    steering = (repo / ".kiro" / "steering" / "qa-master.md").read_text()
    assert steering.startswith("---\ninclusion: manual\n---\n# qa-master") and "name: qa-master" not in steering
    assert "qa-master" in (repo / ".kiro" / "steering" / "qa-review.md").read_text()
    assert (repo / ".qa-review.yaml").exists()
    assert any("verify_command" in l for l in lines)
    again = init(repo, command="qa-review")
    assert any(l.startswith("unchanged") for l in again) and any(l.startswith("kept existing") for l in again)


# ---------------------------------------------------------------- MCP end to end
def test_mcp_server_end_to_end(repo):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    (repo / ".qa-review.yaml").write_text(yaml.safe_dump({"checks": [{"name": "tests", "cmd": "true", "kind": "tests"}]}))
    change(repo)
    src = str(Path(__file__).resolve().parents[1])

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "qa_review", "serve", str(repo)],
                                       env={"PYTHONPATH": src, "PATH": "/usr/bin:/bin",
                                            "QA_REVIEW_AGENTICOS_HOME": str(repo / "none")})
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            res = await s.call_tool("qa_run", {})
            summary = json.loads(res.content[0].text)
            pkt = await s.call_tool("qa_review_packet", {})
            sub = await s.call_tool("qa_submit_review", {"run_id": summary["run_id"], "verdict": "pass",
                                                         "summary": "fine", "cleared_rules": ["shell"]})
            rep = await s.call_tool("qa_report", {})
            return names, summary, pkt.content[0].text, json.loads(sub.content[0].text), rep.content[0].text

    names, summary, pkt, sub, rep = asyncio.run(go())
    assert {"qa_run", "qa_review_packet", "qa_submit_review", "qa_report", "qa_impact",
            "qa_security_scan", "qa_status"} <= names
    assert summary["verdict"] == "warn" and summary["review"] == "pending"
    assert "Code review" in pkt
    assert sub["verdict"] == "pass"
    assert "PASS" in rep and "fine" in rep
