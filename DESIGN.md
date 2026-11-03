# qa-review — design

QA + code review in one step, for Claude Code and Kiro, as an MCP server and a CLI.
It does not duplicate what already exists: it can lean on **agenticOS** (orchestration, review, lessons)
and **graphskill** (code graph), but it works without either of them.

## What it reuses (when available)

| Piece | Comes from | What for |
|---|---|---|
| Call graph, symbol ranges | graphskill (its MCP server, short stdio session) | Blast radius of a change |
| Lessons and team rules | agenticOS (`~/.agenticos`, read-only) | Review checklist grounded in what the team learned |
| Board tasks, briefs, contracts | agenticOS (`board.db`, read-only) | A source for acceptance criteria |
| Board workers | agenticOS (`verify_command`) | Workers must pass qa-review before `task_complete` |

The fixed security rules are inspired by agenticOS's `review_focus.py` but re-implemented, so qa-review has no
dependency on it.

## What qa-review adds

1. **Works on any diff**, not only board tasks (`qa-review run --base main`).
2. **Impact analysis**: changed symbols → callers → which tests, endpoints and e2e flows to run.
3. **Functional QA**: Playwright (UI + API) and Schemathesis (fuzzing against OpenAPI).
4. **One report** (JSON + Markdown): pass / warn / block verdict and findings by severity.
5. **Host targeting** (`--host`): point the tests at local, docker or staging; production is protected.
6. **Acceptance criteria (UAC)**: from the task (Kiro spec, agenticOS board, Jira/Linear) completed by the code,
   kept as Gherkin, turned into Playwright tests, run against a host, and reported in PDF/HTML with evidence per
   UAC.

## Pipelines

```
QA + review:
diff ─► fixed checks (lint, types, tests, security rules)
     ─► impact (graphskill or heuristics: callers of what changed)
     ─► targeted QA (related tests, Playwright, Schemathesis) against --host
     ─► independent AI review (review packet → host agent → qa_submit_review)
     ─► report (+ agenticOS lessons in the packet)

UAC:
task + code ─► uac_draft_packet ─► Gherkin (uac_save, validated)
            ─► uac_tests_packet ─► Playwright tests with evidence helpers
            ─► uac_run (host) ─► per-UAC results + evidence ─► PDF / HTML report
```

## Decisions

- **Standalone MCP** (2026-11-03): qa-review is its own package and MCP server. It works without graphskill or
  agenticOS; it detects them at run time and uses them when present.
- **qa-review never calls a model.** The host agent does the review (`qa_review_packet` → `qa_submit_review`)
  and writes UACs and tests from packets. Behaviour is the same in Claude Code, Kiro and CI.
- **The verdict comes from fixed rules**, so it is predictable. Regex security flags only warn; the reviewer
  confirms or clears them. Secrets always block.
- **agenticOS integration is read-only** (lessons, AGENTS.md, board tasks) plus `verify_command`. qa-review does
  not write lessons: agenticOS's learner validates those.
- **Hosts are validated** (http(s) only, no shell characters) and **protected hosts** need an explicit
  `allow_protected`; the agent is told never to set it on its own.
- **UACs live with the code** (`qa/uac/<slug>.feature`); runs and reports live in `.qa-review/` (git-ignored).
- **The report is one self-contained HTML**, printed to PDF with Playwright's Chromium or an installed Chrome.
  If no browser can print it, the HTML is still delivered with the reason. Output format is configurable.
- **English by default**; Spanish supported for Gherkin keywords and the UAC report (`uac.language: es`).

## Next

- Pilot on a real project (Knowledge-Graph has PHPUnit, PHPStan, a `swagger.yml` and docker-compose).
