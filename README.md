# qa-review

QA and code review **in one step** for the change you are working on. It works as:

- an **MCP server** for Claude Code and Kiro: the agent uses it before calling a piece of work done;
- a **CLI** for the terminal and CI: one command, one report, one exit code.

It works **on its own**. If it finds **graphskill** or **agenticOS** on your machine it uses them to sharpen the
result; if not, it works just the same.

---

## Contents

1. [How it works](#1-how-it-works)
2. [Requirements](#2-requirements)
3. [Installation (once per machine)](#3-installation-once-per-machine)
4. [Connect a project](#4-connect-a-project)
5. [Using it from Claude Code](#5-using-it-from-claude-code)
6. [Using it from Kiro](#6-using-it-from-kiro)
7. [Using it from the terminal](#7-using-it-from-the-terminal)
8. [Acceptance criteria (UAC) with evidence](#8-acceptance-criteria-uac-with-evidence)
9. [Configuration: `.qa-review.yaml`](#9-configuration-qa-reviewyaml)
10. [What it checks](#10-what-it-checks)
11. [The verdict](#11-the-verdict)
12. [graphskill integration](#12-graphskill-integration)
13. [agenticOS integration](#13-agenticos-integration)
14. [CI (GitHub Actions)](#14-ci-github-actions)
15. [MCP tools reference](#15-mcp-tools-reference)
16. [Troubleshooting](#16-troubleshooting)
17. [Development](#17-development)
18. [Known limits](#18-known-limits)

---

## 1. How it works

```
 your change (git)
      │
      ├─► security rules (fixed, no AI)
      ├─► impact: changed symbols → who calls them → which tests cover them
      ├─► project checks: lint, types, tests  (+ e2e with Playwright, API with Schemathesis)
      │
      ▼
  provisional verdict: pass / warn / block
      │
      ├─► "review packet": diff + impact + flags + team rules
      │        │
      │        ▼
      │   AI review (done by YOUR agent: Claude Code, Kiro or an agenticOS reviewer)
      │        │
      └────────┴─► final verdict + report
```

Two key ideas:

- **qa-review never calls a model.** It runs the deterministic part (checks, tests, rules, impact) and prepares
  a review packet. The agent you already use does the review and sends it back with `qa_submit_review`. That is
  why it works the same in Claude Code, in Kiro and in CI (in CI only the deterministic part runs).
- **It reviews "the change", not the whole repo.** By default it compares against the *merge-base* with `main`,
  so on a feature branch it reviews the whole branch (committed or not, including new untracked files). On
  `main`/`master` itself it reviews only uncommitted work.

### A real example, step by step

**The case:** in a Python project with an API and tests (FastAPI + pytest + Playwright), a developer asks
Claude Code: *"add an optional discount to the cart total"*. Claude changes `app/pricing.py` (`total()` now
accepts `discount`) and adds a test.

**1. Before saying "done", the agent runs quick QA** (on its own, because of the skill):

```
qa_run(level="quick")
```

qa-review works out the change (branch `feature/discount` against `main`), applies the security rules and asks
graphskill who calls `total()`: `checkout()` in `app/cart.py` and `invoice()` in `app/billing/invoice.py`. With
that it picks the tests to run: `tests/test_pricing.py` and `tests/test_cart.py`, not the project's 400. Result:

```
verdict: block
- check `pytest` fail     → test_cart.py::test_checkout_total  (expected 50, got 45)
```

Without the impact analysis that test would not have run: it does not test `pricing.py` directly, but one of
its callers.

**2. The agent fixes it and runs again.** `checkout()` passed an argument by position that now landed in
`discount`. It fixes it and runs `qa_run(level="quick")` again: the tests pass.

**3. Full QA against staging**, before opening the PR:

```
qa_run(level="full", host="staging.myapp.com")
```

Unit tests run locally. Playwright gets `BASE_URL=https://staging.myapp.com` and Schemathesis fires hundreds of
requests at that host based on its `swagger.yml`. Everything passes.

**4. Independent review.** The agent asks for `qa_review_packet()` and hands it to a fresh subagent. The packet
carries the diff, the callers of `total()`, the project rules and an agenticOS lesson: *"amounts are always
integer cents"*. The reviewer answers:

```
qa_submit_review(verdict="warn", summary="Correct; the discount is not bounded",
  findings=[{severity: "medium", title: "A discount larger than the total makes it negative",
             file: "app/pricing.py", line: 6}])
```

**5. Final fix and wrap-up.** The agent adds `max(0, …)` with its test and runs `qa_run` again. The review then
passes and it shares the `qa_report`:

```
qa-review ✅ PASS · Target host: https://staging.myapp.com
Checks: pytest ✅ · ruff ✅ · playwright ✅ · schemathesis ✅
Review: pass — discount bounded and tested
```

**6. On the PR**, GitHub Actions repeats the deterministic part (`qa-review run`) and blocks the merge if
something breaks again.

If anyone asked for `host="myapp.com"` (production, listed in `protected_hosts`), qa-review would refuse unless
you explicitly allowed `allow_protected`.

---

## 2. Requirements

| Required | For |
|---|---|
| Python 3.10 or later | qa-review |
| git | Working out the change |

| Optional | For |
|---|---|
| The tools your project already uses (ruff, pytest, npm scripts, phpunit, phpstan, go) | The checks. Only installed ones run |
| Node + `@playwright/test` | End-to-end tests and UAC tests |
| Chrome, Chromium, Edge or Brave | UAC report as PDF (when Playwright's Chromium is not available) |
| `schemathesis` | Fuzzing the API against its OpenAPI/Swagger spec |
| graphskill | More precise impact (see [section 12](#12-graphskill-integration)) |
| agenticOS | Team lessons and rules in the review (see [section 13](#13-agenticos-integration)) |

---

## 3. Installation (once per machine)

> On macOS, create the virtual environment **outside** `~/Desktop` and `~/Documents`: iCloud can break venvs.

```bash
# 1. Its own virtual environment
python3 -m venv ~/.qa-review/venv

# 2. Install qa-review (editable, with development dependencies)
~/.qa-review/venv/bin/pip install -e "$HOME/Projects/qa-review[dev]"

# 3. Put the command on your PATH
mkdir -p ~/.local/bin
ln -sf ~/.qa-review/venv/bin/qa-review ~/.local/bin/qa-review
#    (make sure ~/.local/bin is on your PATH)

# 4. Check
qa-review --version
cd ~/Projects/qa-review && ~/.qa-review/venv/bin/pytest -q     # all tests should pass
```

---

## 4. Connect a project

```bash
qa-review init ~/path/to/your-project
```

`init` is idempotent: you can run it as often as you like. Existing files are kept, and in the `mcp.json` files
it **adds** the `qa-review` server without touching the others (for example `graphskill`).

| File it writes | For |
|---|---|
| `.qa-review.yaml` | Project settings (all optional; written commented out) |
| `.mcp.json` → `qa-review` server | Claude Code |
| `.claude/skills/qa-review/SKILL.md` | Tells Claude Code when and how to use it |
| `.claude/skills/qa-master/SKILL.md` | The QA judgment: how to model risk, triage failures, iterate and report bugs |
| `.kiro/settings/mcp.json` → `qa-review` server | Kiro |
| `.kiro/steering/qa-review.md` | Kiro rule: run QA before calling something done |
| `.kiro/steering/qa-master.md` | The same QA judgment for Kiro (`inclusion: manual`, `#qa-master`) |

Options:

```bash
qa-review init . --no-kiro                 # Claude Code only
qa-review init . --no-claude               # Kiro only
qa-review init . --command "qa-review"     # how to start the server (default: the venv's Python with -m qa_review)
```

Then:

1. Open (or restart) Claude Code or Kiro **in that project**.
2. Approve the `qa-review` MCP server when asked.
3. Check what it will run: `qa-review status` (or ask the agent for *"qa_status"*).

> Commit the files? `.qa-review.yaml`, the skill and the steering file, yes (they belong to the team). The
> `mcp.json` files contain absolute paths on your machine: share them only if the whole team uses the same path,
> or use `--command qa-review`.

---

## 5. Using it from Claude Code

Ask in your own words: *"run QA and review on this change"*, *"is this ready to merge?"*, *"review the branch"*.
The installed skill walks the agent through this flow:

1. **`qa_run`** with `level: quick` while iterating (only the tests related to the change, no e2e) and
   `level: full` before finishing.
2. Fix what blocks and run `qa_run` again.
3. **`qa_review_packet`** and review it. If the same agent wrote the code, it should hand the packet to a
   **fresh subagent**, so the review does not inherit the author's assumptions.
4. **`qa_submit_review`** with the verdict, summary, findings and `cleared_rules` (security flags checked and
   found to be false positives).
5. **`qa_report`** with the final report. Never call a change done on a `block` verdict.

---

## 6. Using it from Kiro

Same as Claude Code: the server is in `.kiro/settings/mcp.json` and the *steering* file
(`.kiro/steering/qa-review.md`, `inclusion: always`) reminds Kiro to run QA before finishing, and points it to
`.kiro/steering/qa-master.md` for acceptance testing. You can also pull that one into a chat with `#qa-master`.

To automate it further, create a Kiro **agent hook** (for example manual, or when a spec task finishes) with the
prompt: *"Run qa_run with level quick, fix what blocks and summarise the qa_report"*.

---

## 7. Using it from the terminal

```bash
qa-review status                      # detected checks and active integrations
qa-review run                         # QA of the current change; markdown report
qa-review run --level quick           # related tests only, no e2e/API
qa-review run --base develop          # compare against another branch or commit
qa-review run --no-checks             # security rules + impact only (seconds)
qa-review run --format json           # machine-readable output
qa-review run --fail-on warn          # stricter: also fail on warn
qa-review run --host staging.myapp.com --only e2e,api   # against another environment (see below)
qa-review packet                      # review packet of the latest run (to paste into an agent)
qa-review report                      # report of the latest run
qa-review report --run <id> --format json
qa-review serve                       # MCP server over stdio (your IDE starts it, not you)
```

They all take the project path as first argument (default: the current directory).

### Pointing the tests at a host

With `--host` (or `host` in `qa_run`) you choose which environment the tests run against, without touching the
configuration:

```bash
qa-review run --host localhost:8000                     # your machine → http://localhost:8000
qa-review run --host app:8080                           # docker compose service → http://app:8080
qa-review run --host staging.myapp.com --only e2e,api   # staging, e2e and API only → https://staging.myapp.com
```

Every check receives the host in two ways:

- **Environment variables:** `BASE_URL` (configurable with `host_env`) and always `QA_REVIEW_HOST`. Playwright
  reads it (`use: { baseURL: process.env.BASE_URL }` in `playwright.config`), and so can API tests and scripts.
- **`{host}` inside the command**, already shell-quoted: `cmd: "curl -fsS {host}/health"`. A check that uses
  `{host}` when no host was given is `skipped` instead of failing.

Schemathesis uses the host as its `--url` (the run's host wins over the configured `base_url`).

Rules:

- Without a scheme it is completed: `http://` for `localhost`, `127.0.0.1`, names without a dot (docker
  services), `.localhost`, `.local` and `.test`; `https://` for everything else. For an internal IP over plain
  HTTP, write the scheme yourself: `http://10.0.0.5:3000`.
- Only `http(s)` URLs without shell characters are accepted; anything else is an error (exit code 2).
- **Protected hosts (production):** list them in `protected_hosts` in `.qa-review.yaml`. A protected host (or a
  subdomain of it) only runs with `--allow-protected`. The agent is told never to use it unless you explicitly
  ask.

**Exit codes of `run`:** `0` pass or warn · `1` block (or warn with `--fail-on warn`) · `2` error (not a git
repo, invalid configuration…).

Every run is saved in `<project>/.qa-review/runs/<id>.json`. That folder has its own `.gitignore`, so it never
ends up in a commit.

---

## 8. Acceptance criteria (UAC) with evidence

qa-review can turn a task into **acceptance criteria (UAC) written in Gherkin**, generate from them a **suite of
Playwright tests (E2E and API)**, run it against the host you choose and deliver a **PDF and/or HTML report with
the evidence for every UAC**: screenshots, requests and responses, errors, and traces and video of failures.

The tests do not only **prove** that the change does what the task asks: they also try to **break** it. Every
round includes adversarial UACs (`@break`) built from the change's attack surface, plus an automatic battery of
hostile requests, and the next round attacks around whatever broke (see [Prove and break](#prove-and-break)).

As with the review, qa-review never calls a model: your agent writes the UACs and the tests from the packets
qa-review prepares, and qa-review validates, stores, runs and documents them.

### The qa-master skill

The tools do the mechanics; the **qa-master** skill (installed by `qa-review init`, source in
`qa_review/skills/qa-master.md`) gives the agent the judgment of a senior QA:

- **Model before testing:** what this component owns and what it delegates, actors, domain invariants (money
  in exact cents, time zones, text limits, identities) and who enforces each one, the state × action table, the
  oracle for each behaviour and where the risk is.
- **The owner test:** every UAC must pass *"if it fails, would the team that owns this component accept it as
  their bug?"*. If not, it is dropped or becomes a `@proposed` UAC.
- **UACs and tests that can fail:** concrete values, the Then as the oracle (status, body and state after),
  boundaries on both sides, assumptions written down, and "mutate it in your head" before trusting a test.
- **Triage every red:** product bug, test bug, environment, flaky or spec question, and reproduce outside the
  test before calling it a bug. Check the greens too: a `@break` that never reached the system proves nothing.
- **Iterate where it broke:** bugs cluster, ask what each bug implies, tours by domain, and when to stop.
- **Report like a QA lead:** confirmed bugs by severity with a repro, questions for product, important things
  outside the task, coverage in one line.

Projects wired before this skill existed get it by running `qa-review init` again (existing files are kept).

### Scope: only what this component is responsible for

A UAC is only worth having if, when it fails, it is a bug of **this** component. Two typical mistakes it
prevents: a payments **proxy** tested for "a negative amount is rejected" (the payments service behind it
validates that, not the proxy), and "changing the HTTP method fails" when no code of the change handles methods
(the framework does).

**Say what the component owns**, once per project:

```yaml
uac:
  component:
    role: proxy                     # proxy | gateway | bff | service | frontend
    owns: [routing, auth headers, timeouts, error mapping]
    delegates: {payment validation: payments-service}
```

The draft packet shows it with a rule: a delegated rule is not a UAC here; the UAC is that the input reaches the
next service intact and its answer comes back as it is. For an intermediary (`proxy`, `gateway`, `bff`…) the
attack plan changes to what it does own:

| Category | What it tries | What must happen |
|---|---|---|
| `passthrough` | values the next service must judge (negative or huge amounts, unicode, unknown fields) | forwarded byte for byte, not validated or rewritten here |
| `upstream` | the next service answers 4xx/5xx, slowly, not at all, or with invalid JSON | 4xx relayed as they are, failures mapped to 502/503/504 in time, nothing retried that must not be |
| `headers` | hop-by-hop, internal and credential headers, correlation and idempotency ids | ids forwarded, hop-by-hop removed, nothing internal leaked |
| `routing` | path traversal and double encoding towards other routes, another host (SSRF) | only the exposed routes reach the next service |

`auth` is added when `owns` mentions auth or tokens. When `uac.component` is not set and the change calls other
services (httpx, requests, fetch, axios, Guzzle, RestTemplate, `proxy_pass`…, detected per language), the packet
says so and asks the agent to confirm the role with you before drafting.

**Every UAC says what makes it required**, and `uac_save` rejects the ones that cannot:

| Tags | Meaning | Checked |
|---|---|---|
| `@source:task @ref:1.2` | the task asks for it | for a Kiro spec, `1.2` must be one of its criteria |
| `@source:code @ref:app/proxy.py:42` | the code of this component implements it | the file exists and has that line |
| `@source:tests @ref:tests/test_pay.py::test_x` | an existing test requires it | the file exists |
| `@proposed` + a `Why:` line | the QA thinks the team should want it | the `Why:` line is there |

A **proposed** UAC is run and reported like any other, but outside the verdict: if it fails it shows as **Not
met** and goes to the questions for the team, not to the defects.

**Closing a category on purpose.** An attack category that does not apply is closed with `@na:<category>` on
the Feature and the reason in its description (`N/A http: handled by the server in front`). It shows as "not
applicable" in the report instead of as a gap, so nobody writes a filler UAC to cover it.

### Where UACs come from

| Source | How to use it | What qa-review reads |
|---|---|---|
| **Kiro spec** | `source="kiro"`, `ref="<spec folder>"` | `.kiro/specs/<spec>/requirements.md` (with its numbered EARS criteria `1.1`, `1.2`…), `design.md` and `tasks.md` |
| **agenticOS board** | `source="agenticos"`, `ref="<task id>"` | Task title and spec, feature brief and contract (read-only from `~/.agenticos/data/board.db`) |
| **Jira / Linear** | The agent reads the issue with its connector and passes it as `task_text` (`source="jira"`, `ref="PROJ-123"`) | The text the agent passes |
| **The code** | Always, automatically | The branch diff: added endpoints (FastAPI, Flask, Express, Laravel, Symfony, Nest/Spring, Django), touched pages and changed symbols |
| **Existing tests** | Always, automatically | The project's tests, `.feature` files and e2e/acceptance specs that already call the endpoints of the change (Python, JS/TS, PHP, Go, Java/Kotlin, Ruby, Gherkin) |

The task decides **what** must happen. The code **completes** the criteria with what the task does not say
(validations, errors, endpoints). If the code does not do something the task asks for, the UAC is written
anyway: its test will fail, and that is exactly what the report must show.

**Existing tests are criteria someone already agreed on.** The draft packet lists, for each endpoint of the
change, the existing tests that call it (file, test name and line). The agent turns every one that encodes a
business rule into a UAC tagged `@source:tests @ref:<file>::<test>`, unless another UAC already covers it. When an
existing test contradicts the task, that contradiction is reported as a finding instead of being silently
resolved. qa-review's own generated tests (`tests/uac/`) are never used as a source.

### The flow

```
uac_sources ─► uac_draft_packet ─► (agent writes Gherkin) ─► uac_save
            ─► uac_tests_packet ─► (agent writes Playwright tests) ─► uac_run ─► PDF / HTML report
                                                                          │
            ◄──────────── uac_iterate_packet (next round: break more) ◄───┘
```

1. **Draft.** `uac_draft_packet(source="kiro", ref="cart-discount")` returns the task, the criteria it already
   has, what changed in the code and the rules for writing the UACs.
2. **Save.** The agent writes a `.feature` file (one `Scenario` per UAC) and saves it with `uac_save`, which
   validates it:
   - every scenario has a unique `@UAC-<n>` and `@e2e` (UI) and/or `@api` (HTTP);
   - what makes it required: `@source:task @ref:1.2`, `@source:code @ref:<file>:<line>` or
     `@source:tests @ref:<file>::<test>`, or `@proposed` with a `Why:` line (see [Scope](#scope-only-what-this-component-is-responsible-for));
   - adversarial scenarios also carry `@break:<category>` (for example `@break:boundary @break:injection`);
   - the `Feature` carries `@task:<ref>` so the report can be traced back to the task.

   It is saved in `qa/uac/<slug>.feature`, versioned with the code.
3. **Tests.** `uac_tests_packet` creates (if missing) `playwright.uac.config.ts` and `tests/uac/evidence.ts`, and
   explains how to write `tests/uac/<slug>.spec.ts`: every test title starts with `[UAC-n]`, and the evidence
   helpers record everything:
   - `shot(page, 'label')`: a screenshot after each visible *Then*;
   - `apiCall(request, 'POST', '/api/…', data)`: records request and response (sensitive headers hidden);
   - `note('text')`: any relevant data (ids created, values read).

   For `@break` UACs, `tests/uac/attacks.ts` adds the attack helpers: `PAYLOADS` (the same payloads as the
   battery), `expectSafeError(res, {status})` (a 4xx, no 5xx, no leaked internals), `expectNoLeak(res)`,
   `inParallel(n, fn)` (concurrency), `snapshot(request, url)` (state before/after), `failApi(page, pattern)` and
   `goOffline(page)` (UI resilience).
4. **Run.** `uac_run(slug, host="staging.myapp.com", format="pdf,html")` runs the tests against that host
   (`BASE_URL`), maps every result to its UAC, runs the automatic attack battery and writes the report.
5. **Iterate.** `uac_iterate_packet(slug)` prepares the next round from the last run: attack around what broke,
   escalate what held, cover the gaps of the attack plan and the UACs without tests. Back to step 2.

### Prove and break

Every draft packet includes the **attack surface** of the change and a deterministic **attack plan**:

- **Surface:** every endpoint the change adds or touches (from the code and, if configured, the OpenAPI spec),
  with its method, path parameters and the body/query fields it reads; plus whether UI changed.
- **Plan:** for every endpoint, the categories worth attacking, what to try and what the system must do instead:

| Category | What it tries | What must happen |
|---|---|---|
| `boundary` | 0, -1, min-1, max+1, huge numbers, empty and 10k strings | 4xx or the exact rule; never a 5xx or a wrong result |
| `types` | strings for numbers, null, arrays/objects, missing/extra fields, malformed JSON, wrong Content-Type | 4xx naming the field; no mass assignment |
| `injection` | SQL, XSS, template, command, path traversal, NoSQL, CRLF | treated as plain data, escaped, no 5xx |
| `unicode` | emoji, RTL, zero-width, combining characters, NUL | kept intact or a 4xx; never corrupted |
| `auth` | no/invalid token, another user's id (IDOR), role fields in the body | 401/403/404 and no foreign data |
| `http` | MB-sized bodies, repeated params, garbage headers, long URLs (only what the component parses itself) | 413/4xx, fast |
| `concurrency` | the same request N times in parallel, double submit | applied once, consistent state |
| `state` | steps out of order, deleted/expired things, resubmits | 4xx/409, state unchanged |
| `resilience` (UI) | failed or slow network, server errors, offline, narrow screen | a visible error and a way to recover |

The agent writes `@break` UACs for that plan next to the functional ones. In the report a failed `@break` UAC
shows as **Broken**: the attack succeeded, so it is a defect, not a test to rewrite.

**The automatic battery.** After the Playwright tests, `uac_run` sends a battery of hostile requests (malformed
JSON, wrong Content-Type, wrong types, boundary numbers, injection and unicode payloads, 2 MB bodies, hostile path
parameters) to every endpoint of the change and checks four invariants that hold for any API:

- no 5xx (and no dropped connection): bad input is the client's fault;
- no leaks: no stack traces, SQL errors, file paths or debug pages in the response;
- no reflected XSS: an injected `<script>` never comes back unescaped in HTML;
- no hang: every request answers within `uac.attack_timeout_sec`.

It needs no agent and no Playwright. Because it sends hostile input that may be written if the API accepts it,
**it only runs against local hosts by default** (localhost, private IPs, docker service names, `.local`, `.test`,
`.internal`). Control it with `uac.attacks` (`local` | `always` | `off`) or per run with `--attacks` /
`--no-attacks` (`attacks=True/False` in MCP). `QA_REVIEW_API_TOKEN` is sent with every request.

**Coverage.** `run.json` records the surface, the plan, which planned categories some `@break` UAC covers and
which are still gaps; the gaps are listed in the report and in the next round's packet. A gap is covered only
if it touches what the component owns; otherwise it is closed with `@na:<category>`. An intermediary gets its
own plan (see [Scope](#scope-only-what-this-component-is-responsible-for)).

### UAC status and verdict

| Status | When |
|---|---|
| Passed | All its tests pass |
| Failed | Any test fails |
| Broken | A `@break` UAC failed: the attack succeeded |
| Flaky | A test passed only after a retry |
| Skipped | All its tests were skipped |
| Not covered | No test carries its `[UAC-n]` |
| Not met | A `@proposed` UAC failed: a question for the team, outside the verdict |

Overall verdict (over the required UACs; proposals never change it): **ACCEPTED** if all pass and the battery
found nothing; **NOT ACCEPTED** if any UAC fails or breaks, or the battery broke the system; **INCOMPLETE** if any
is not covered, skipped or flaky. Tests that mention no UAC are listed separately as "tests without a criterion".

### The report

One self-contained HTML file (images embedded) from which the PDF is printed. It contains:

- **Header:** project, task, host, branch and commit (flags uncommitted changes), date, run id and duration.
- **Verdict** with the count per status, and a **summary table** of the UACs with type, source, reference and tests.
- **One section per UAC** (in the PDF, each on a new page): the Gherkin scenario and, for every test, its result,
  the error if it failed and its evidence (screenshots, request/response, notes, and paths to trace and video).
- **Robustness:** adversarial UACs that held and broke, the categories planned vs covered (gaps), and every
  request of the automatic battery that broke an invariant, with the response.
- **Run details:** the command and its output.

**Configurable output:** `uac.report` in `.qa-review.yaml` (`[pdf, html]` by default), or `--format` / `format`
on each run (`pdf`, `html`, `pdf,html` or `both`). `qa-review uac report <slug> --format html` re-creates the
report of the latest run without running the tests again.

**Report language:** English by default; `uac.language: es` writes the report (and asks the agent to write the
UACs) in Spanish. Gherkin files may use English or Spanish keywords (`# language: es`, `Dado/Cuando/Entonces`).

**How the PDF is made** (in order): with the project's Playwright Chromium and, if that is not available, with an
installed Chrome, Chromium, Edge or Brave (`QA_REVIEW_CHROME` forces a specific path). If none works you get the
HTML plus the reason; you are never left without a report.

Reports, screenshots and results are saved in `.qa-review/uac/<slug>/<run>/` (ignored by git).

### From the terminal

```bash
qa-review uac sources                                    # Kiro specs, agenticOS board, saved UACs
qa-review uac draft --source kiro --ref cart-discount    # packet to draft the UACs
qa-review uac draft --source jira --ref PROJ-123 --text-file issue.md
qa-review uac save cart-discount cart-discount.feature   # validate and save (or "-" for stdin)
qa-review uac show cart-discount
qa-review uac tests cart-discount                        # scaffolding + packet to write the tests
qa-review uac run cart-discount --host staging.myapp.com --format pdf,html
qa-review uac run cart-discount --host localhost:8000 --no-attacks   # skip the automatic battery
qa-review uac iterate cart-discount                      # next round: attack around what broke
qa-review uac report cart-discount --format html         # re-create the report of the latest run
```

`uac run` returns `0` when the verdict is ACCEPTED, `1` otherwise and `2` on an error (no host, no tests,
protected host…).

### Requirements for this part

UAC tests are **always Playwright Test (TypeScript)**, whatever framework the project uses (Karate, JUnit,
pytest…): `uac_run` only runs `tests/uac/<slug>.spec.ts`, and the evidence comes from Playwright. The project's
existing tests are read as a source of criteria, never as a template. If the agent writes them with another
framework, `uac_run` refuses and names the files it found.

- Node and `@playwright/test` in the project: `npm i -D @playwright/test && npx playwright install chromium`
  (or use your Chrome with `QA_REVIEW_BROWSER_CHANNEL=chrome`).
- To record video of failures Playwright needs `ffmpeg` (`npx playwright install ffmpeg`); or turn it off with
  `QA_REVIEW_VIDEO=off`.
- If the API needs a token: `QA_REVIEW_API_TOKEN` is sent as `Authorization: Bearer …` on every request.

## 9. Configuration: `.qa-review.yaml`

Everything is optional. Without this file, checks are **detected automatically** (see
[section 10](#10-what-it-checks)).

```yaml
# What to compare against (default: merge-base with main/master)
base: main

# The EXACT list of checks. When set, it replaces auto-detection.
checks:
  - name: lint
    cmd: "ruff check ."
    kind: lint                          # lint | types | tests | security | e2e | api | check
    blocking: false                     # on failure: warn instead of block
    targeted_cmd: "ruff check {targets}"  # version limited to the changed files (quick level)
  - name: tests
    cmd: "pytest -q"
    kind: tests
    targeted_cmd: "pytest -q {targets}"   # in quick, {targets} = tests related to the change

# Functional QA
qa:
  playwright: {cmd: "npx playwright test --reporter=line"}   # or "off"; detected if playwright.config.* exists
  openapi:
    spec: openapi.yaml
    base_url: "http://localhost:8000"   # the API must be running
    # cmd: "schemathesis run openapi.yaml --url http://localhost:8000"   # alternative: your own command

# Your own rules for the reviewer to check
review:
  rules:
    - "Every endpoint validates its input"
    - "Amounts are integer cents"

# Optional integrations
integrations:
  graphskill: auto        # auto | off | "<command>", e.g. "/path/venv/bin/python -m graphskill"
  agenticos: auto         # auto | off

# Maximum time per check, in seconds
timeout_sec: 600

# Default host for the tests (override on each run with --host)
host: http://localhost:8000
host_env: [BASE_URL, API_URL]     # variables that receive the host (QA_REVIEW_HOST always does)
protected_hosts: [myapp.com]      # production: only with --allow-protected

# Acceptance criteria (UAC)
uac:
  dir: qa/uac                     # .feature files (one per task), versioned
  tests_dir: tests/uac            # generated Playwright tests
  playwright_config: playwright.uac.config.ts
  report: [pdf, html]             # pdf, html or both
  output_dir: .qa-review/uac      # runs, evidence and reports
  language: en                    # language of the UACs and the report (en | es)
  attacks: local                  # automatic attack battery: local (local hosts only) | always | off
  attack_timeout_sec: 10          # per request of the battery
  component:                      # what this component owns: UACs stay inside it
    role: service                 # service | proxy | gateway | bff | frontend
    owns: [discount rules]
    delegates: {payment validation: payments-service}
```

**Levels:**

| Level | What runs |
|---|---|
| `quick` | Security + impact + checks; those with `targeted_cmd` are limited to the change. No e2e or API |
| `full` | Everything, unlimited. Includes Playwright and Schemathesis when configured |

---

## 10. What it checks

### Auto-detected checks

Only tools that **are installed** are used. It looks in the project's environment first (`.venv/bin`,
`venv/bin`, `node_modules/.bin`, `vendor/bin`) and then on the `PATH`.

| Stack (detected by) | Checks |
|---|---|
| Python (`pyproject.toml`, `setup.py`, `requirements.txt`) | `ruff` (non-blocking), `pytest` if there is a tests folder |
| Node (`package.json`) | scripts `lint` (non-blocking), `typecheck`/`type-check`, `test` with npm, pnpm or yarn depending on the lockfile |
| PHP (`composer.json`) | `phpstan` (non-blocking), `phpunit` |
| Go (`go.mod`) | `go vet`, `go test` |
| Playwright (`playwright.config.*`) | `playwright test` |
| OpenAPI/Swagger | Schemathesis if you configure `qa.openapi`; if it finds an unconfigured spec it suggests it |

### Security rules (on the added lines)

| Rule | Severity | Detects |
|---|---|---|
| `secret-literal` / `secret-token` | critical | Passwords, API keys, tokens or private keys written in the code (the value is never shown) |
| `sql-interp` | high | SQL built with interpolation or concatenation |
| `shell` | high | `os.system`, `shell=True`, `child_process.exec`, `shell_exec`… |
| `eval` | high | `eval`, `exec`, `new Function` |
| `deserialize` | high | `pickle.loads`, `yaml.load` without SafeLoader, `unserialize` |
| `path-from-input` | high | File paths built from request data |
| `tls-off` | high | TLS verification disabled |
| `env-file` | high | A `.env` file in the change |
| `weak-hash` | medium | MD5 or SHA-1 |
| `debug-on` | medium | Debug mode enabled |
| `cors-any` | medium | CORS open to any origin |
| `xss-sink` | medium | `innerHTML`, `dangerouslySetInnerHTML`, `|safe`, `{!!` |

In test files only the secret rules apply. And when the change touches sensitive paths (auth, login, session,
token, payment, `.env`…), the review packet adds a **security checklist**.

### Impact

- **Changed symbols:** functions and classes whose lines the change touches.
- **Callers:** who calls those symbols (with graphskill, through the graph; without it, with `git grep`).
- **Related tests:** those that mention the module in their name (`test_cart.py`, `cart.test.ts`,
  `CartTest.php`, `test_checkout_cart.py`…) plus those that are direct callers.
- **Untested:** changed code files for which no test was found.

---

## 11. The verdict

It is decided by fixed rules, so it is predictable:

| Verdict | When |
|---|---|
| ⛔ **block** | A blocking check fails (or times out, or cannot run), there is a `critical` finding, the reviewer reports something `high`, or the reviewer blocks |
| ⚠️ **warn** | A non-blocking check fails, there is a `medium` or higher finding, a security flag has not been cleared, or changed code has no test |
| ✅ **pass** | None of the above |

An automatic `high` flag (for example `shell`) gives **warn**, not block, because it is a heuristic. The reviewer
confirms it (as a finding) or clears it with `cleared_rules`. Secrets (`critical`) always block.

---

## 12. graphskill integration

With graphskill the impact is exact: real symbol ranges in any language it indexes, and the call graph up to
depth 2. qa-review opens a short MCP session with the project's graphskill server; it does not need to import
graphskill.

It is looked up in this order (first match wins):

1. `integrations.graphskill` in `.qa-review.yaml` set to a command: `"/path/venv/bin/python -m graphskill"`.
2. The `graphskill` entry in the project's `.mcp.json` (the one `graphskill setup` writes).
3. The `QA_REVIEW_GRAPHSKILL` environment variable set to a command.
4. A `graphskill` executable on the `PATH`.

If it is not found or does not answer, heuristics are used (`ast` for Python, definition regexes for other
languages and `git grep` for callers) and the report says so. **It never fails because graphskill is missing.**

Recommended steps:

```bash
graphskill setup ~/path/to/your-project     # indexes and writes the .mcp.json entry
qa-review init  ~/path/to/your-project      # adds qa-review next to graphskill
qa-review status ~/path/to/your-project     # should say "graphskill: on, from the project's .mcp.json"
```

---

## 13. agenticOS integration

### agenticOS → qa-review (read-only)

If the project is linked with `aos link`, the review packet includes:

- the project's **lessons**: confirmed ones and unreviewed ones with confidence ≥ 0.5 (rejected ones never);
- the **team rules** from `AGENTS.md`.

So the reviewer checks the change against what the team has already learned. qa-review only **reads** the files
in `~/.agenticos` (or `QA_REVIEW_AGENTICOS_HOME`); it does not write lessons, because agenticOS's learner
validates those.

The UAC flow can also start from a board task (`source="agenticos"`, see [section 8](#8-acceptance-criteria-uac-with-evidence)).

### qa-review → agenticOS (board)

To make board workers pass qa-review before calling a task done, set the project's `verify_command` in
`~/.agenticos/data/projects.yaml`:

```yaml
worker:
  verify_command: "~/.qa-review/venv/bin/qa-review run . --ci"
```

It runs in the task's worktree. On `block` (exit code 1) agenticOS rejects the attempt and the report becomes
the reason for the next one. `qa-review init` prints the exact line for your installation.

Check: `qa-review status` should show `agenticos: on, project '<slug>'`.

---

## 14. CI (GitHub Actions)

```yaml
jobs:
  qa-review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0                     # needed to compute the merge-base
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install "git+https://github.com/<your-org>/qa-review.git"   # or wherever you keep it
      - run: pip install -r requirements.txt   # your project's tools (pytest, ruff…)
      - name: qa-review
        run: |
          status=0
          qa-review run --base "origin/${{ github.base_ref }}" > qa-report.md || status=$?
          cat qa-report.md >> "$GITHUB_STEP_SUMMARY"
          exit $status
```

In CI only the deterministic part runs. The report shows up in the job summary and the step fails on `block`.

---

## 15. MCP tools reference

All of them accept `project` (a path) to work on a project other than the one the server was started for.

| Tool | Parameters | Returns |
|---|---|---|
| `qa_status` | — | Checks that would run, suggestions, integration status |
| `qa_run` | `base`, `level` (`quick`/`full`), `paths`, `only` (check names or kinds), `host`, `allow_protected` | Summary: `run_id`, verdict, reasons, checks (with the output of failing ones), flags, impact and next step |
| `qa_review_packet` | `run_id` (latest by default), `max_diff_chars` | Text with checklist, rules, lessons, flags, failing checks, impact and diff |
| `qa_submit_review` | `run_id`, `verdict` (`pass`/`warn`/`block`), `summary`, `findings`, `cleared_rules`, `reviewer` | Updated verdict |
| `qa_report` | `run_id`, `format` (`markdown`/`json`) | Full report |
| `qa_impact` | `base` | Impact only, no checks run |
| `qa_security_scan` | `base` | Security rules only, no checks run |
| `uac_sources` | — | Kiro specs, agenticOS status and saved UACs |
| `uac_draft_packet` | `source` (`kiro`/`agenticos`/`jira`/`linear`/`text`), `ref`, `task_text`, `title`, `slug`, `base` | Task, existing criteria, code changes, existing tests of the endpoints, attack surface and plan, and rules for writing the UACs |
| `uac_save` | `slug`, `gherkin` | Validated and saved UACs (or the list of problems) |
| `uac_show` | `slug` | The saved UACs, parsed |
| `uac_tests_packet` | `slug` | Creates the scaffolding and explains how to write the tests |
| `uac_run` | `slug`, `host`, `format`, `allow_protected`, `attacks` | Verdict, status per UAC, robustness (battery and gaps) and report paths |
| `uac_iterate_packet` | `slug`, `run_id` | The next round: what broke, what held, plan gaps and UACs without tests |
| `uac_report` | `slug`, `run_id`, `format` | Re-creates the report of a run in another format |

Format of each finding in `findings`:

```json
{"severity": "high", "title": "The discount can make the total negative",
 "file": "app/pricing.py", "line": 6, "detail": "missing max(0, ...)"}
```

Severities: `info`, `low`, `medium`, `high`, `critical`.

---

## 16. Troubleshooting

| Symptom | What to do |
|---|---|
| `qa-review: ... is not a git repository` | Run it inside a git repo, or pass the project path |
| "no checks detected" | The tools are not installed in the project's environment. Install them or define `checks:` in `.qa-review.yaml` |
| A check is `error` | The command does not exist (exit 127). Check `qa-review status` and the tool's path |
| A check is `timeout` | Raise `timeout_sec`, or limit the check with `targeted_cmd` and use `--level quick` |
| `graphskill: off` although it is installed | Run `graphskill setup <project>` or set `integrations.graphskill` to the command |
| `graphskill unavailable, used heuristics` | The server did not answer: check the project is indexed (`graphskill index <project>`) |
| `agenticos: ... not linked` | Link the project with `aos link <project>` |
| The agent does not see the tools | Restart Claude Code or Kiro in the project and approve the `qa-review` server |
| The change includes things you did not expect | Look at the report's `Base:` line and use `--base` to pin it |
| `uac run`: "no tests yet" | Write `tests/uac/<slug>.spec.ts` (ask for `uac_tests_packet`) |
| `uac run`: "Playwright produced no results" | `@playwright/test` or the browser is missing: `npm i -D @playwright/test && npx playwright install chromium` |
| UI tests fail mentioning "ffmpeg" | `npx playwright install ffmpeg`, or `QA_REVIEW_VIDEO=off` |
| A UAC is "Not covered" | No test title starts with `[UAC-n]`: add one |
| Only HTML, no PDF | No Chromium or Chrome found: install one or set its path in `QA_REVIEW_CHROME` (the reason is in `report_errors`) |
| "no task N on the agenticOS board" | Check the id with `aos board`; qa-review reads `~/.agenticos/data/board.db` |
| Battery: "not a local host" | It only attacks local hosts by default. Pass `--attacks` (only on an environment you can dirty) or set `uac.attacks: always` |
| Battery: "is not reachable" | The app is not running at the host, or not answering `GET /` |
| Battery: "connection refused" mid-run | An earlier attack killed the server: that request is the bug; restart the app and look at its log |
| `uac_save`: "@source:code must point to the code…" | Add `@ref:<file>:<line>` with the code of this component that does it. If none does, it is not this component's behaviour: drop it or mark it `@proposed` with a `Why:` line |
| `uac_save`: "@ref:1.4 is not a criterion of .kiro/specs/…" | The criterion does not exist in the spec: fix the reference or the UAC |
| The packet says "This change calls other services" | Set `uac.component` in `.qa-review.yaml` (role, owns, delegates) |
| A UAC shows "Not met" | A `@proposed` UAC failed: ask the team whether they want that behaviour; it does not change the verdict |
| A UAC shows "Broken" | A `@break` test failed: the attack succeeded. Fix the code, not the test |
| The draft packet lists no existing tests | No test file calls the endpoint paths literally (e.g. the URL is built dynamically) |
| "no test found" for a file that does have tests | The test's name does not mention the module. Rename it or ignore the warning (it is warn, not block) |

---

## 17. Development

```bash
cd ~/Projects/qa-review
~/.qa-review/venv/bin/pip install -e '.[dev]'
~/.qa-review/venv/bin/pytest -q
```

Layout:

```
qa_review/
  cli.py            run / serve / status / packet / report / init / uac commands
  server.py         MCP server (qa_* and uac_* tools)
  engine.py         the full pipeline and run storage
  gitdiff.py        what changed (merge-base, uncommitted, untracked)
  security.py       fixed security rules
  impact.py         symbols, callers and related tests
  checks.py         check detection and execution
  qa.py             Playwright and Schemathesis
  review.py         review packet
  report.py         verdict and markdown report
  wiring.py         `init`: Claude Code, Kiro and agenticOS
  target.py         --host: validation, protected hosts, environment variables
  uac/              acceptance criteria: gherkin, sources (Kiro, agenticOS, code), store,
                    packets, scaffold (Playwright + evidence), runner, report (HTML/PDF), api
  integrations/
    graphskill.py   graphskill MCP client (optional)
    agenticos.py    reads lessons and team rules (optional)
tests/              pytest; includes a fake graphskill server, a fake Playwright and end-to-end MCP tests
```

Design and decisions are in [DESIGN.md](DESIGN.md).

---

## 18. Known limits

- The security rules are line-based **heuristics**: they can give false positives (that is why they only warn
  and the reviewer clears them) and they do not replace a full SAST.
- Without graphskill, callers are found by name with `git grep`: no inheritance, imports or duplicate names.
- Code and tests are matched by file name: a test whose name does not mention the module is not matched (at
  most it gives a warn).
- Schemathesis and the UAC tests need the application running at the host; qa-review does not start it.
- The attack battery only knows the fields it can read from the code or the OpenAPI spec, and checks generic
  invariants (5xx, leaks, XSS, hangs); business-level breaks come from the `@break` UACs the agent writes.
- Existing tests are matched by the endpoint path written literally in the test; URLs built from variables or
  helpers are not found.
- The AI review depends on the agent doing it and sending it with `qa_submit_review`. In CI only the
  deterministic part runs.
