import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from qa_review import gitdiff
from qa_review.cli import main
from qa_review.uac import (
    api,
    attacks,
    baseline,
    gherkin,
    report,
    runner,
    scaffold,
    scope,
    store,
    surface,
)

API_PY = '''from fastapi import FastAPI
app = FastAPI()

@app.post("/api/cart/discount")
def discount(body: dict):
    amount = body.get("amount")
    coupon = body.get("coupon")
    return {"total": 45}

@app.get("/api/orders/{order_id}")
def order(order_id: int):
    return {"id": order_id}
'''

SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/api/cart/discount": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Discount"}}}}}},
        "/api/orders/{order_id}": {"get": {"parameters": [
            {"name": "order_id", "in": "path", "required": True, "schema": {"type": "integer"}}]}},
        "/api/health": {"get": {}},
    },
    "components": {"schemas": {"Discount": {"type": "object", "required": ["amount"], "properties": {
        "amount": {"type": "number", "minimum": 0.01, "maximum": 500},
        "coupon": {"type": "string", "maxLength": 20}}}}},
}


# ---------------------------------------------------------------- a deliberately fragile API
class Fragile(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if "order" in self.path and ("'" in self.path or "%27" in self.path):
            return self._send(500, 'psycopg2.errors.SyntaxError: syntax error at or near "\'"')
        self._send(200, json.dumps({"ok": True}))

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            self.close_connection = True
            return                                         # crash: no answer at all
        if not isinstance(body, dict):
            return self._send(500, 'Traceback (most recent call last):\n  File "/srv/app/api.py", line 9')
        coupon = body.get("coupon")
        if isinstance(coupon, str) and "<script>" in coupon:
            return self._send(200, f"<p>Coupon {coupon} applied</p>", "text/html")   # reflected XSS
        if not isinstance(body.get("amount"), (int, float)):
            return self._send(422, json.dumps({"error": "amount must be a number"}))
        self._send(200, json.dumps({"total": 45}))


@pytest.fixture
def fragile():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fragile)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def with_api(repo, spec=True):
    (repo / "app" / "api.py").write_text(API_PY)
    if spec:
        (repo / "openapi.json").write_text(json.dumps(SPEC))


# ---------------------------------------------------------------- surface and plan
def test_surface_from_code_and_spec(repo):
    with_api(repo)
    eps = {f"{e['method']} {e['path']}": e for e in surface.surface(repo, gitdiff.collect(repo))}
    assert set(eps) == {"POST /api/cart/discount", "GET /api/orders/{order_id}"}
    d = eps["POST /api/cart/discount"]
    assert d["source"] == "code+openapi"
    amount = next(f for f in d["fields"] if f["name"] == "amount")
    assert amount["required"] and amount["minimum"] == 0.01 and amount["maximum"] == 500
    assert eps["GET /api/orders/{order_id}"]["path_params"] == ["order_id"]


def test_surface_without_spec_reads_fields_from_code(repo):
    with_api(repo, spec=False)
    d = next(e for e in surface.from_code(repo, gitdiff.collect(repo)) if e["method"] == "POST")
    assert {f["name"] for f in d["fields"]} >= {"amount", "coupon"}


def test_spec_paths_used_by_custom_routers(repo):
    (repo / "openapi.json").write_text(json.dumps(SPEC))
    (repo / "app" / "server.py").write_text('def handle(path):\n    if path == "/api/cart/discount":\n        pass\n')
    eps = surface.surface(repo, gitdiff.collect(repo))
    assert [e["path"] for e in eps] == ["/api/cart/discount"]
    assert "path used in changed code" in eps[0]["source"]


def test_attack_plan():
    eps = [{"method": "POST", "path": "/d", "fields": [{"name": "amount"}], "path_params": []},
           {"method": "GET", "path": "/o/{id}", "fields": [], "path_params": ["id"]}]
    rows = attacks.plan(eps, has_ui=True)
    cats = {(r["target"], r["category"]) for r in rows}
    assert {("POST /d", c) for c in ("types", "injection", "boundary", "concurrency", "state", "http", "auth",
                                     "unicode")} <= cats
    assert {("GET /o/{id}", "auth"), ("GET /o/{id}", "boundary")} <= cats
    assert ("UI", "resilience") in cats
    assert all(r["expect"] for r in rows)


# ---------------------------------------------------------------- Gherkin
def test_break_tags():
    f = gherkin.parse("Feature: X\n  @UAC-1 @api @break:injection @break:unicode\n  Scenario: a\n    When b\n"
                      "    Then c\n  @UAC-2 @api @break:magic\n  Scenario: d\n    When e\n    Then f\n")
    assert f.scenarios[0].breaks == ["injection", "unicode"]
    assert "unknown @break category magic" in "\n".join(gherkin.validate(f))


# ---------------------------------------------------------------- battery
def test_is_local_and_modes():
    assert baseline.is_local("http://localhost:3000") and baseline.is_local("http://app:8080")
    assert baseline.is_local("http://10.0.0.5") and baseline.is_local("http://shop.test")
    assert not baseline.is_local("https://staging.myapp.com") and not baseline.is_local("http://8.8.8.8")
    assert baseline.should_run("http://localhost", "local", None)[0]
    run_, why = baseline.should_run("https://staging.myapp.com", "local", None)
    assert not run_ and "attacks=True" in why
    assert baseline.should_run("https://staging.myapp.com", "local", True)[0]
    assert not baseline.should_run("http://localhost", "always", False)[0]
    assert not baseline.should_run("http://localhost", "off", None)[0]


def test_settings_attacks(repo):
    assert store.settings(repo).attacks == "local"
    (repo / ".qa-review.yaml").write_text("uac:\n  attacks: off\n")
    assert store.settings(repo).attacks == "off"
    (repo / ".qa-review.yaml").write_text("uac:\n  attacks: sometimes\n")
    with pytest.raises(ValueError):
        store.settings(repo)


def test_cases_cover_categories():
    ep = {"method": "POST", "path": "/api/x/{id}", "fields": [{"name": "amount"}], "path_params": ["id"]}
    cs = baseline.cases(ep)
    cats = {c["category"] for c in cs}
    assert {"types", "boundary", "injection", "unicode", "http"} <= cats
    assert any(c["label"] == "malformed JSON" for c in cs)
    assert any("../../etc/passwd" in c["path"] or "..%2F" in c["path"] for c in cs)
    get_cases = baseline.cases({"method": "GET", "path": "/s", "fields": [{"name": "q"}], "path_params": []})
    assert all("json" not in c for c in get_cases) and any("?q=" in c["path"] for c in get_cases)


def test_check_invariants():
    case = {"path": "/x", "json": {"c": "<script>alert('qa')</script>"}}
    assert baseline.check(case, {"status": 200, "body": "{}"}) == []
    assert baseline.check(case, {"status": 503, "body": ""}) == ["server error 503"]
    assert "leaks internals" in baseline.check(case, {"status": 400, "body": 'File "/app/x.py", line 3'})[0]
    assert baseline.check(case, {"status": 200, "content_type": "text/html",
                                 "body": "<p><script>alert('qa')</script></p>"}) == ["reflects <script> unescaped (XSS)"]
    assert baseline.check(case, {"status": 200, "content_type": "text/html",
                                 "body": "&lt;script&gt;alert('qa')&lt;/script&gt;"}) == []
    assert baseline.check(case, {"status": None, "error": "the server closed the connection"}) == \
        ["the server closed the connection"]


def test_battery_breaks_a_fragile_api(fragile):
    eps = [{"method": "POST", "path": "/api/cart/discount", "fields": [{"name": "amount"}, {"name": "coupon"}],
            "path_params": []},
           {"method": "GET", "path": "/api/orders/{order_id}", "fields": [], "path_params": ["order_id"]}]
    r = baseline.run(fragile, eps, timeout=5)
    assert r["ran"] and r["cases"] > 40
    broke = {(x["label"], "; ".join(x["broke"])) for x in r["results"] if x["broke"]}
    text = "\n".join(f"{a} -> {b}" for a, b in broke)
    assert "malformed JSON -> the server closed the connection without answering (it crashed on this input)" in text
    assert "JSON array instead of object -> server error 500; leaks internals" in text
    assert "reflects <script> unescaped (XSS)" in text
    assert "path param = \"'\" -> server error 500; leaks internals" in text
    assert not any(x["broke"] for x in r["results"] if x["label"].startswith("amount = \"not-a-number\""))


def test_battery_unreachable():
    r = baseline.run("http://127.0.0.1:9", [{"method": "GET", "path": "/", "fields": [], "path_params": []}], timeout=2)
    assert not r["ran"] and "not reachable" in r["reason"]


# ---------------------------------------------------------------- the whole round, with a fake Playwright
FEATURE = """@task:T-1
Feature: Discounts

  @UAC-1 @api @source:task @ref:T-1
  Scenario: valid discount
    When I apply 5
    Then the total is 45

  @UAC-2 @api @source:code @ref:app/api.py:1 @break:types
  Scenario: wrong types are rejected
    When I send garbage
    Then the response is 422

  @UAC-3 @api @source:code @ref:app/api.py:1 @break:injection
  Scenario: injection is harmless
    When I send payloads
    Then nothing leaks
"""


def fake_results():
    def spec(title, status, err=None):
        return {"title": title, "file": "t.spec.ts", "line": 1, "tags": [],
                "tests": [{"projectName": "chromium", "status": status,
                           "results": [{"status": "passed", "duration": 5, "attachments": [],
                                        "error": {"message": err} if err else None}]}]}
    return {"suites": [{"title": "t", "file": "t.spec.ts", "suites": [], "specs": [
        spec("[UAC-1] ok", "expected"), spec("[UAC-2] types", "unexpected", "Expected: 422\nReceived: 200"),
        spec("[UAC-3] injection", "expected")]}]}


@pytest.fixture
def round_repo(repo, tmp_path):
    with_api(repo)
    api.save(repo, "t", FEATURE)
    scaffold.ensure(repo)
    (repo / "tests" / "uac" / "t.spec.ts").write_text("// tests")
    fake = tmp_path / "results.json"
    fake.write_text(json.dumps(fake_results()))
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "playwright").write_text(f'#!/bin/sh\ncp "{fake}" "$QA_REVIEW_UAC_JSON"\nexit 1\n')
    (bin_dir / "playwright").chmod(0o755)
    return repo


def test_round_with_battery_report_and_iteration(round_repo, fragile, monkeypatch):
    monkeypatch.setenv("QA_REVIEW_CHROME", "/nonexistent/chrome")
    res = api.run(round_repo, "t", host=fragile, formats="html")
    rb = res["robustness"]
    assert (rb["break_uacs"], rb["held"], rb["broken"]) == (2, 1, 1)
    assert rb["battery"]["ran"] and rb["battery"]["broken"] >= 3
    assert {"auth", "boundary", "concurrency", "http", "state", "unicode"} <= set(rb["attack_gaps"])
    assert res["verdict"] == "fail"
    html = Path(res["report"]["html"]).read_text(encoding="utf-8")
    assert "Robustness" in html and "Automatic attack battery" in html and ">Broken<" in html
    assert "break:types" in html and "it crashed on this input" in html

    text = api.iterate_packet(round_repo, "t")
    assert "## It broke here: attack around it" in text and "**UAC-2** wrong types are rejected" in text
    assert "## The automatic battery broke it" in text and "malformed JSON" in text
    assert "## It held: escalate" in text and "UAC-3 injection is harmless" in text
    assert "## Gaps in the attack plan" in text and "`auth` on GET /api/orders/{order_id}, POST /api/cart/discount" in text
    assert "UI" not in text.split("## Gaps in the attack plan")[1].split("##")[0]     # app/api.py is backend, not UI
    assert "ids continue from 4" in text


def test_battery_respects_host_and_switch(round_repo, fragile, monkeypatch):
    monkeypatch.setenv("QA_REVIEW_CHROME", "/nonexistent/chrome")
    off = api.run(round_repo, "t", host=fragile, formats="html", attacks=False)
    assert not off["robustness"]["battery"]["ran"] and off["robustness"]["battery"]["reason"] == "disabled"
    remote = api.run(round_repo, "t", host="https://staging.example.com", formats="html")
    assert not remote["robustness"]["battery"]["ran"]
    assert "not a local host" in remote["robustness"]["battery"]["reason"]


def test_scaffold_attack_helpers(repo):
    with_api(repo)
    api.save(repo, "t", FEATURE)
    text = api.tests_packet(repo, "t")
    assert "tests/uac/attacks.ts" in text and "expectSafeError" in text and "inParallel" in text
    ts = (repo / "tests" / "uac" / "attacks.ts").read_text()
    start = ts.index("export const PAYLOADS = { ...") + len("export const PAYLOADS = { ...")
    payloads = json.loads(ts[start:ts.index(", long: 'a'.repeat")])
    assert "<script>alert('qa')</script>" in payloads["injection"] and payloads["wrongTypes"][1] is None
    leaks = json.loads(ts[ts.index("const LEAKS = ") + len("const LEAKS = "):ts.index(".map((p) => new RegExp")])
    assert leaks and not any("(?i)" in p for p in leaks)


def test_draft_packet_has_attack_plan(repo):
    with_api(repo)
    text = api.draft_packet(repo, "jira", "T-1", "As a customer I want discounts")
    assert "## Attack surface" in text and "`amount` (number, min 0.01, max 500, required)" in text
    assert "## Attack plan" in text and "| POST /api/cart/discount | `injection` |" in text
    assert "@break:<category>" in text and "Try to break it" in text


def test_cli_iterate_and_attack_switch(round_repo, fragile, capsys, monkeypatch):
    monkeypatch.setenv("QA_REVIEW_CHROME", "/nonexistent/chrome")
    assert main(["uac", "run", "t", str(round_repo), "--host", fragile, "--format", "html"]) == 1
    out = capsys.readouterr().out
    assert "robustness: 2 adversarial UACs, 1 held, 1 broke it; battery" in out and "BROKE  POST" in out
    assert main(["uac", "run", "t", str(round_repo), "--host", fragile, "--format", "html", "--no-attacks"]) == 1
    assert "battery not run (disabled)" in capsys.readouterr().out
    assert main(["uac", "iterate", "t", str(round_repo)]) == 0
    assert "Next round" in capsys.readouterr().out


def test_settings_file_round_trip(repo):
    (repo / ".qa-review.yaml").write_text(yaml.safe_dump({"uac": {"attacks": "always", "attack_timeout_sec": 3}}))
    s = store.settings(repo)
    assert (s.attacks, s.attack_timeout_sec) == ("always", 3)


# ---------------------------------------------------------------- existing tests as a source of UACs
def test_existing_tests_of_the_endpoints(repo):
    with_api(repo)
    (repo / "tests" / "test_discount_api.py").write_text(
        "def test_discount_is_applied(client):\n"
        "    r = client.post('/api/cart/discount', json={'amount': 5})\n"
        "    assert r.json()['total'] == 45\n\n"
        "def test_unrelated():\n    assert 1\n\n"
        "def test_order_lookup(client):\n    client.get(f'/api/orders/{order_id}')\n")
    (repo / "e2e").mkdir()
    (repo / "e2e" / "cart.spec.ts").write_text(
        "test('rejects a negative discount', async ({ request }) => {\n"
        "  const r = await request.post(`/api/cart/discount`, { data: { amount: -1 } });\n"
        "});\n")
    (repo / "features").mkdir()
    (repo / "features" / "orders.feature").write_text(
        "Feature: Orders\n  Scenario: A customer sees only their orders\n    When I GET /api/orders/7\n    Then 200\n")
    (repo / "tests" / "uac").mkdir()
    (repo / "tests" / "uac" / "old.spec.ts").write_text("test('[UAC-1] x', () => request.post('/api/cart/discount'))\n")
    from qa_review.uac import existing
    eps = surface.surface(repo, gitdiff.collect(repo))
    found = {(h["file"], h["endpoint"]): [t["name"] for t in h["tests"]] for h in existing.find(repo, eps)}
    assert found[("tests/test_discount_api.py", "POST /api/cart/discount")] == ["test_discount_is_applied"]
    assert found[("tests/test_discount_api.py", "GET /api/orders/{order_id}")] == ["test_order_lookup"]
    assert found[("e2e/cart.spec.ts", "POST /api/cart/discount")] == ["rejects a negative discount"]
    assert found[("features/orders.feature", "GET /api/orders/{order_id}")] == ["A customer sees only their orders"]
    assert not any(f.startswith("tests/uac/") for f, _ in found)          # our own UAC tests are not a source

    text = api.draft_packet(repo, "jira", "T-1", "As a customer I want discounts")
    assert "## Existing tests of these endpoints" in text
    assert "`tests/test_discount_api.py` → POST /api/cart/discount: `test_discount_is_applied` (line 1)" in text
    assert "@source:tests" in text


# ---------------------------------------------------------------- scope: only what this component owns
PROXY_PY = """import httpx
from fastapi import FastAPI, Request
app = FastAPI()

@app.post("/api/payments")
async def pay(request: Request):
    r = await httpx.AsyncClient().post("http://payments:8000/payments", content=await request.body())
    return r.json()
"""

KIRO_REQ = """# Requirements
### Requirement 1: Pay
#### Acceptance Criteria
1. WHEN the client sends a payment THEN the proxy SHALL forward it to the payments service
2. WHEN the payments service answers THEN the proxy SHALL return that answer unchanged
"""


def scoped_feature(*scenarios: str, tags: str = "@task:pay") -> str:
    return f"{tags}\nFeature: Pay through the proxy\n\n" + "\n".join(scenarios)


def sc(n: int, tags: str, name: str = "x", desc: str = "") -> str:
    return (f"  {tags}\n  Scenario: {name} {n}\n" + (f"    {desc}\n" if desc else "")
            + "    When something\n    Then something else\n")


def proxy_repo(repo):
    (repo / "app" / "proxy.py").write_text(PROXY_PY)
    spec = repo / ".kiro" / "specs" / "pay"
    spec.mkdir(parents=True)
    (spec / "requirements.md").write_text(KIRO_REQ)
    return repo


def test_scenario_description_and_proposed():
    f = gherkin.parse(scoped_feature(sc(1, "@UAC-1 @api @proposed", desc="Why: double charges cost money.")))
    s = f.scenarios[0]
    assert s.proposed and s.description == "Why: double charges cost money." and len(s.steps) == 2
    assert "Why: double charges" in s.text
    bad = gherkin.parse(scoped_feature(sc(1, "@UAC-1 @api @proposed")))
    assert any("needs a 'Why:' line" in p for p in gherkin.validate(bad))
    na = gherkin.parse(scoped_feature(sc(1, "@UAC-1 @api @source:task @ref:1.1"), tags="@task:pay @na:auth @na:magic"))
    assert na.not_applicable == ["auth", "magic"]
    assert any("unknown @na category magic" in p for p in gherkin.validate(na))


def test_every_uac_must_be_traceable(repo):
    proxy_repo(repo)
    (repo / "tests" / "test_proxy.py").write_text("def test_forwards():\n    pass\n")
    feature = gherkin.parse(scoped_feature(
        sc(1, "@UAC-1 @api @source:task @ref:1.1"),                          # a real criterion
        sc(2, "@UAC-2 @api @source:task @ref:1.4"),                          # not in the spec
        sc(3, "@UAC-3 @api @source:code"),                                   # the negative-amount kind: no code
        sc(4, "@UAC-4 @api @source:code @ref:app/proxy.py:7"),               # points to the forwarding code
        sc(5, "@UAC-5 @api @source:code @ref:app/proxy.py:99"),
        sc(6, "@UAC-6 @api @source:tests @ref:tests/test_proxy.py::test_forwards"),
        sc(7, "@UAC-7 @api @source:tests @ref:tests/nope.py::t"),
        sc(8, "@UAC-8 @api"),                                                # no source at all
        sc(9, "@UAC-9 @api @proposed", desc="Why: retries must not double charge."),
        sc(10, "@UAC-10 @api @source:code @ref:1.2"),                        # a criterion under the wrong source
    ))
    problems = scope.problems(repo, feature)
    by = {p.split(" ")[0]: p for p in problems}
    assert set(by) == {"UAC-2", "UAC-3", "UAC-5", "UAC-7", "UAC-8", "UAC-10"}
    assert "use @source:task @ref:1.2" in by["UAC-10"]
    assert "@ref:1.4 is not a criterion of .kiro/specs/pay (it has 1.1, 1.2)" in by["UAC-2"]
    assert "not this component's behaviour" in by["UAC-3"]
    assert "app/proxy.py has 8 lines" in by["UAC-5"]
    assert "tests/nope.py does not exist" in by["UAC-7"]
    with pytest.raises(gherkin.GherkinError, match="UAC-3"):
        api.save(repo, "pay", scoped_feature(sc(1, "@UAC-1 @api @source:task @ref:1.1"),
                                             sc(3, "@UAC-3 @api @source:code")))


def test_intermediary_detection_and_component(repo):
    proxy_repo(repo)
    text = api.draft_packet(repo, "kiro", "pay")
    assert "## This change calls other services" in text and "`app/proxy.py:7`" in text
    assert "role: proxy" in text                                     # suggests the config
    assert "would the team that owns this component accept it as their bug?" in text
    assert "| `boundary` |" in text                                  # not configured yet: the default plan
    (repo / ".qa-review.yaml").write_text(yaml.safe_dump({"uac": {"component": {
        "role": "proxy", "owns": ["routing", "timeouts"], "delegates": {"payment validation": "payments-service"}}}}))
    text = api.draft_packet(repo, "kiro", "pay")
    assert "## What this component owns (uac.component)" in text and "This change calls other services" not in text
    assert "- payment validation → payments-service" in text
    assert "A rule this component delegates is not a UAC here" in text
    for c in ("passthrough", "upstream", "headers", "routing"):
        assert f"| `{c}` |" in text
    for c in ("boundary", "types", "auth"):
        assert f"| `{c}` |" not in text


def test_plan_by_role():
    ep = [{"method": "POST", "path": "/api/payments", "fields": [{"name": "amount"}], "path_params": []}]
    assert {r["category"] for r in attacks.plan(ep, False, intermediary=True)} == set(attacks.INTERMEDIARY_CATEGORIES)
    assert "auth" in {r["category"] for r in attacks.plan(ep, False, intermediary=True, owns_auth=True)}
    assert "unexpected methods" not in attacks.CATEGORIES["http"].ideas
    assert scope.owns_auth(scope.component(type("S", (), {"component": {"owns": ["auth headers"]}})()))
    comp = scope.component(type("S", (), {"component": {"role": "Proxy", "delegates": ["amount rules: payments"]}})())
    assert comp == {"role": "proxy", "owns": [], "delegates": {"amount rules": "payments"}}


def test_proposals_stay_outside_the_verdict():
    feature = gherkin.parse(scoped_feature(sc(1, "@UAC-1 @api @source:task @ref:1.1"),
                                           sc(2, "@UAC-2 @api @proposed @break:concurrency", desc="Why: idempotency.")))
    tests = [{"title": "[UAC-1] a", "uacs": ["UAC-1"], "status": "passed"},
             {"title": "[UAC-2] b", "uacs": ["UAC-2"], "status": "failed"}]
    uacs, _, counts = runner.summarize(feature, tests)
    assert counts["total"] == 1 and counts["failed"] == 0
    assert counts["proposed_total"] == 1 and counts["proposed_not_met"] == 1
    assert counts["break_total"] == 0 and counts["break_broken"] == 0          # a proposal never "broke" it
    assert runner.verdict(counts) == "pass"
    assert uacs[1]["proposed"] and "Not met" in report._status(uacs[1], report.LABELS["en"])
    cov = runner.coverage([{"breaks": ["types"]}], [{"category": "types"}, {"category": "auth"},
                                                     {"category": "state"}], ["auth"])
    assert cov["gaps"] == ["state"] and cov["not_applicable"] == ["auth"]


def test_outbound_detection_is_per_language(repo):
    (repo / "app" / "page.py").write_text('PAGE = "<script>fetch(\'/api/cart\')</script>"\n')
    (repo / "app" / "client.ts").write_text("export const pay = (b) => fetch('http://payments/pay', b);\n")
    (repo / "nginx.conf").write_text("location /pay { proxy_pass http://payments; }\n")
    calls = {c["file"] for c in scope.outbound_calls(gitdiff.collect(repo))}
    assert calls == {"app/client.ts", "nginx.conf"}
