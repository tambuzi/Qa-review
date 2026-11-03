import base64
import json
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml
from conftest import git

from qa_review import gitdiff
from qa_review.cli import main
from qa_review.uac import api, gherkin, report, runner, scaffold, sources, store

FEATURE = """\
@task:PROJ-7
Feature: Cart discount
  As a customer I want to pay less.

  @UAC-1 @api @e2e @source:task @ref:1.1
  Scenario: A valid discount is subtracted from the total
    Given a cart of 50 €
    When I apply a 5 € discount
    Then the total is 45 €

  @UAC-2 @api @source:task @ref:1.2
  Scenario Outline: A non-positive discount is rejected
    Given a cart of 50 €
    When I apply <amount> €
    Then the response is 422
    And the total stays at 50 €

    Examples:
      | amount  |
      | 0       |
      | -3      |

  @UAC-3 @api @source:code
  Scenario: The total is never negative
    Given a cart of 10 €
    When I apply 15 €
    Then the total is 0 €
"""

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode()


# ---------------------------------------------------------------- Gherkin
def test_parse_feature():
    f = gherkin.parse(FEATURE)
    assert f.name == "Cart discount" and f.tags == ["@task:PROJ-7"]
    assert [s.uac_id for s in f.scenarios] == ["UAC-1", "UAC-2", "UAC-3"]
    s2 = f.scenarios[1]
    assert s2.outline and "| -3      |" in s2.text and s2.kinds == ["api"]
    assert [st.kind for st in s2.steps] == ["given", "when", "then", "then"]
    assert f.scenarios[0].tag_value("ref") == "1.1" and f.scenarios[2].tag_value("source") == "code"
    assert gherkin.validate(f) == []
    assert gherkin.next_id(f) == 4


def test_parse_spanish_keywords():
    f = gherkin.parse("# language: es\nCaracterística: Login\n\n  @UAC-1 @e2e\n  Escenario: Entra\n"
                      "    Dado un usuario\n    Cuando inicia sesión\n    Entonces ve su panel\n    Y su nombre\n")
    assert gherkin.validate(f) == []
    assert [s.kind for s in f.scenarios[0].steps] == ["given", "when", "then", "then"]


def test_validate_reports_every_problem():
    bad = ("Feature: X\n\n  @UAC-1 @api\n  Scenario: a\n    When x\n    Then y\n\n"
           "  @UAC-1\n  Scenario: b\n    When x\n    Then y\n\n"
           "  @api\n  Scenario: c\n    Given x\n")
    problems = gherkin.validate(gherkin.parse(bad))
    text = "\n".join(problems)
    assert "UAC-1 is already used" in text
    assert "tag it @e2e" in text
    assert "needs exactly one @UAC" in text
    assert "at least one When and one Then" in text


def test_parse_errors():
    with pytest.raises(gherkin.GherkinError, match="Feature"):
        gherkin.parse("Scenario: x")
    with pytest.raises(gherkin.GherkinError, match="step outside"):
        gherkin.parse("Feature: x\n  Given y\n")


# ---------------------------------------------------------------- sources
REQUIREMENTS = """# Requirements Document

## Requirements

### Requirement 1: Apply a discount
**User Story:** As a customer, I want to apply a discount, so that I pay less.

#### Acceptance Criteria
1. WHEN the customer applies a valid discount THEN the system SHALL subtract it
2. IF the discount is 0 THEN the system SHALL reject it

### Requirement 2
**User Story:** As an admin, I want to see the discounts.

#### Acceptance Criteria
1. WHEN the admin opens the list THEN the system SHALL show the discounts
"""


def kiro(root):
    d = root / ".kiro" / "specs" / "cart-discount"
    d.mkdir(parents=True)
    (d / "requirements.md").write_text(REQUIREMENTS)
    (d / "design.md").write_text("# Design\nPOST /api/cart/discount")


def test_kiro_spec(repo):
    kiro(repo)
    assert sources.kiro_specs(repo) == [{"source": "kiro", "ref": "cart-discount",
                                          "files": ["requirements.md", "design.md"]}]
    t = sources.from_kiro(repo, "cart-discount")
    assert [c["id"] for c in t.criteria] == ["1.1", "1.2", "2.1"]
    assert t.criteria[0]["story"].startswith("As a customer")
    assert "## design.md" in t.text
    with pytest.raises(sources.SourceError, match="found: cart-discount"):
        sources.from_kiro(repo, "nope")


def test_agenticos_board(repo, tmp_path, monkeypatch):
    home = tmp_path / "aos"
    data = home / "data"
    (data / "features" / "discounts").mkdir(parents=True)
    (data / "features" / "discounts" / "brief.md").write_text("Brief: discounts for carts")
    (data / "features" / "discounts" / "contract.md").write_text("POST /cart/discount -> {total}")
    (home / "config.yaml").write_text(yaml.safe_dump({"repo": str(tmp_path), "projects": {"proj": {"path": str(repo)}}}))
    con = sqlite3.connect(data / "board.db")
    con.executescript("CREATE TABLE features(slug TEXT, title TEXT); CREATE TABLE tasks(id INTEGER, feature TEXT, "
                      "project TEXT, title TEXT, spec TEXT);"
                      "INSERT INTO features VALUES('discounts','Discounts');"
                      "INSERT INTO tasks VALUES(12,'discounts','proj','Discount endpoint','Add POST /cart/discount');")
    con.commit()
    con.close()
    monkeypatch.setenv("QA_REVIEW_AGENTICOS_HOME", str(home))
    t = api.resolve_task(repo, "agenticos", "12")
    assert t.ref == "discounts#12" and "Add POST /cart/discount" in t.text and "Brief: discounts" in t.text
    assert "## Contract" in t.text
    with pytest.raises(sources.SourceError, match="no task 99"):
        api.resolve_task(repo, "agenticos", "99")
    with pytest.raises(sources.SourceError, match="task id"):
        api.resolve_task(repo, "agenticos", "abc")


def test_text_source_needs_text(repo):
    with pytest.raises(sources.SourceError, match="empty"):
        api.resolve_task(repo, "jira", "PROJ-7", "")
    t = api.resolve_task(repo, "jira", "PROJ-7", "As a customer...", "Discounts")
    assert (t.source, t.ref, t.title) == ("jira", "PROJ-7", "Discounts")


def test_code_signals(repo):
    (repo / "app" / "api.py").write_text('@router.post("/cart/discount")\ndef discount(): ...\n')
    (repo / "web").mkdir()
    (repo / "web" / "routes.js").write_text("app.get('/cart', handler)\n")
    (repo / "routes").mkdir()
    (repo / "routes" / "web.php").write_text("Route::delete('/cart/{id}', [CartController::class, 'destroy']);\n")
    (repo / "resources" / "views").mkdir(parents=True)
    (repo / "resources" / "views" / "cart.blade.php").write_text("<div>{{ $total }}</div>\n")
    sig = sources.code_signals(gitdiff.collect(repo))
    got = {(e["method"], e["path"]) for e in sig["endpoints"]}
    assert {("POST", "/cart/discount"), ("GET", "/cart"), ("DELETE", "/cart/{id}")} <= got
    assert sig["pages"] == ["resources/views/cart.blade.php"]          # routes and app/*.py are backend
    assert sources.is_ui_file("src/components/Cart.tsx") and sources.is_ui_file("app/page.tsx")
    assert not sources.is_ui_file("app/api.py") and not sources.is_ui_file("routes/web.php")


# ---------------------------------------------------------------- store and packets
def test_settings_and_formats(repo):
    assert store.settings(repo).report == ["pdf", "html"]
    (repo / ".qa-review.yaml").write_text(yaml.safe_dump({"uac": {"report": "html", "dir": "docs/uac"}}))
    s = store.settings(repo)
    assert s.report == ["html"] and s.dir == "docs/uac"
    assert store.parse_formats("both") == ["pdf", "html"]
    assert store.parse_formats("pdf, html") == ["pdf", "html"]
    with pytest.raises(ValueError):
        store.parse_formats("docx")


def test_save_validates_and_writes(repo):
    with pytest.raises(gherkin.GherkinError, match="not valid"):
        api.save(repo, "x", "Feature: X\n  @api\n  Scenario: a\n    When b\n    Then c\n")
    res = api.save(repo, "PROJ-7 Discounts", FEATURE)
    assert res["saved"] == "qa/uac/proj-7-discounts.feature"
    assert [u["id"] for u in res["uacs"]] == ["UAC-1", "UAC-2", "UAC-3"]
    assert api.show(repo, "proj-7-discounts")["uacs"][2]["source"] == "code"


def test_draft_packet(repo):
    kiro(repo)
    (repo / "app" / "api.py").write_text('@app.post("/cart/discount")\ndef discount(): ...\n')
    text = api.draft_packet(repo, "kiro", "cart-discount")
    assert "`1.2` IF the discount is 0" in text
    assert "POST /cart/discount (app/api.py:1)" in text
    assert 'uac_save(slug="cart-discount"' in text and "@task:cart-discount" in text
    assert "continue from 1" in text
    api.save(repo, "cart-discount", FEATURE)
    again = api.draft_packet(repo, "kiro", "cart-discount")
    assert "UACs already saved" in again and "continue from 4" in again


def test_tests_packet_scaffolds_once(repo):
    api.save(repo, "d", FEATURE)
    text = api.tests_packet(repo, "d")
    assert "Scaffolding created: `playwright.uac.config.ts`, `tests/uac/evidence.ts`" in text
    assert "tests/uac/d.spec.ts" in text and "[UAC-1]" in text
    cfg = (repo / "playwright.uac.config.ts").read_text()
    assert "testDir: './tests/uac'" in cfg and "process.env.BASE_URL" in cfg
    (repo / "playwright.uac.config.ts").write_text("// mine")
    assert scaffold.ensure(repo) == []
    assert (repo / "playwright.uac.config.ts").read_text() == "// mine"


# ---------------------------------------------------------------- run, with a fake Playwright
def fake_results():
    def spec(title, status, atts=(), err=None, line=1):
        return {"title": title, "file": "d.spec.ts", "line": line, "tags": [],
                "tests": [{"projectName": "chromium", "status": status,
                           "results": [{"status": "passed" if status == "expected" else "failed", "duration": 120,
                                        "error": {"message": err} if err else None, "attachments": list(atts)}]}]}
    api_att = {"name": "api: POST /api/cart/discount", "contentType": "application/json",
               "body": base64.b64encode(json.dumps({"request": {"method": "POST"},
                                                    "response": {"status": 200, "body": {"total": 45}}}).encode()).decode()}
    shot = {"name": "shot: total 45", "contentType": "image/png", "body": PNG}
    return {"suites": [{"title": "d.spec.ts", "file": "d.spec.ts", "specs": [
        spec("[UAC-1] api", "expected", [api_att]),
        spec("[UAC-1] ui", "expected", [shot, {"name": "screenshot", "contentType": "image/png", "body": PNG}]),
        spec("[UAC-2] rejects 0", "unexpected", err="\x1b[31mExpected: 422\nReceived: 200\x1b[39m"),
        spec("smoke without uac", "expected"),
    ], "suites": []}]}


@pytest.fixture
def uac_repo(repo, tmp_path):
    api.save(repo, "d", FEATURE)
    scaffold.ensure(repo)
    (repo / "tests" / "uac" / "d.spec.ts").write_text("// tests")
    fake = tmp_path / "results.json"
    fake.write_text(json.dumps(fake_results()))
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    pw = bin_dir / "playwright"
    pw.write_text(f'#!/bin/sh\necho "BASE=$BASE_URL"\ncp "{fake}" "$QA_REVIEW_UAC_JSON"\nexit 1\n')
    pw.chmod(0o755)
    return repo


def test_run_maps_results_to_uacs(uac_repo, monkeypatch):
    monkeypatch.setenv("QA_REVIEW_CHROME", "/nonexistent/chrome")
    res = api.run(uac_repo, "d", host="staging.app.com", formats="html")
    assert res["host"] == "https://staging.app.com"
    by_id = {u["id"]: u for u in res["uacs"]}
    assert by_id["UAC-1"]["status"] == "passed" and len(by_id["UAC-1"]["tests"]) == 2
    assert by_id["UAC-2"]["status"] == "failed"
    assert by_id["UAC-2"]["tests"][0]["error"] == "Expected: 422\nReceived: 200"     # ANSI colours removed
    assert by_id["UAC-3"]["status"] == "not_covered"
    assert res["verdict"] == "fail" and res["orphan_tests"] == ["smoke without uac"]
    assert set(res["report"]) == {"html"} and res["report_errors"] == []

    run = runner.load_run(uac_repo, "d")
    assert run["task_ref"] == "PROJ-7" and "BASE=https://staging.app.com" in run["output_tail"]
    html = Path(res["report"]["html"]).read_text(encoding="utf-8")
    assert "NOT ACCEPTED" in html and "Not covered" in html and "UAC-3" in html
    assert html.count("data:image/png;base64,") == 1          # the automatic screenshot is skipped: test took its own
    assert "&quot;total&quot;: 45" in html
    assert (uac_repo / ".qa-review" / "uac" / ".gitignore").read_text() == "*\n"


def test_report_in_spanish(uac_repo, monkeypatch):
    (uac_repo / ".qa-review.yaml").write_text(yaml.safe_dump({"uac": {"language": "es"}}))
    res = api.run(uac_repo, "d", host="localhost:3000", formats="html")
    html = Path(res["report"]["html"]).read_text(encoding="utf-8")
    assert "Informe de aceptación" in html and "NO ACEPTADO" in html and "Sin cubrir" in html


def test_pdf_failure_keeps_html(uac_repo, monkeypatch):
    monkeypatch.setenv("QA_REVIEW_CHROME", "/nonexistent/chrome")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    (uac_repo / "node_modules" / ".bin" / "playwright").chmod(0o755)
    res = api.run(uac_repo, "d", host="localhost:3000", formats="pdf")
    assert "pdf" not in res["report"] and "html" in res["report"]
    assert "could not print the PDF" in res["report_errors"][0]


@pytest.mark.skipif(not report.find_chrome() or report.find_chrome() == "/nonexistent/chrome", reason="no Chrome")
def test_pdf_with_chrome(uac_repo):
    res = api.run(uac_repo, "d", host="localhost:3000", formats="pdf,html")
    assert res["report"]["pdf"].endswith(".pdf") and res["report_errors"] == []
    assert Path(res["report"]["pdf"]).read_bytes()[:5] == b"%PDF-"
    again = api.report_again(uac_repo, "d", formats="html")
    assert set(again["report"]) == {"html"} and again["run_id"] == res["run_id"]


def test_run_guards(uac_repo):
    with pytest.raises(runner.UacRunError, match="need a host"):
        api.run(uac_repo, "d")
    (uac_repo / ".qa-review.yaml").write_text(yaml.safe_dump({"protected_hosts": ["miapp.com"]}))
    with pytest.raises(ValueError, match="protected"):
        api.run(uac_repo, "d", host="miapp.com")
    with pytest.raises(ValueError, match="report format"):
        api.run(uac_repo, "d", host="localhost:3000", formats="docx")
    api.save(uac_repo, "e", FEATURE)
    with pytest.raises(runner.UacRunError, match="no tests yet"):
        api.run(uac_repo, "e", host="localhost:3000")


def test_cli_uac(uac_repo, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("QA_REVIEW_CHROME", "/nonexistent/chrome")
    assert main(["uac", "run", "d", str(uac_repo), "--host", "localhost:3000", "--format", "html"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL  1/3 UAC passed, 1 failed, 1 not covered") and "html: " in out
    assert main(["uac", "report", "d", str(uac_repo), "--format", "html"]) == 0
    f = tmp_path / "f.feature"
    f.write_text(FEATURE)
    assert main(["uac", "save", "new-one", str(f), str(uac_repo)]) == 0
    assert main(["uac", "run", "nope", str(uac_repo), "--host", "x.com"]) == 2


def test_mcp_lists_uac_tools(repo):
    import asyncio
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    kiro(repo)
    src = str(Path(__file__).resolve().parents[1])

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "qa_review", "serve", str(repo)],
                                       env={"PYTHONPATH": src, "PATH": "/usr/bin:/bin",
                                            "QA_REVIEW_AGENTICOS_HOME": str(repo / "none")})
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            pkt = await s.call_tool("uac_draft_packet", {"source": "kiro", "ref": "cart-discount"})
            saved = await s.call_tool("uac_save", {"slug": "cart-discount", "gherkin": FEATURE})
            return names, pkt.content[0].text, json.loads(saved.content[0].text)

    names, pkt, saved = asyncio.run(go())
    assert {"uac_sources", "uac_draft_packet", "uac_save", "uac_show", "uac_tests_packet", "uac_run",
            "uac_report"} <= names
    assert "`1.1` WHEN the customer applies" in pkt
    assert saved["saved"] == "qa/uac/cart-discount.feature"


def test_git_untouched(repo):
    api.save(repo, "d", FEATURE)
    assert "qa/uac/d.feature" in git(repo, "status", "--porcelain", "--untracked-files=all")
