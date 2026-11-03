"""Packets for the host agent: one to draft the UACs, one to write their tests."""

from __future__ import annotations

from pathlib import Path

from .. import gitdiff
from ..config import load as load_config
from ..impact import analyse
from ..review import _bounded_diff
from . import gherkin, scaffold, sources, store

_LANG = {"en": "English", "es": "Spanish"}

DRAFT_RULES = """\
## How to write them

Write ONE Gherkin file (one `Feature`, one `Scenario` per acceptance criterion) and save it with
`uac_save(slug="{slug}", gherkin=...)`. Write the scenarios in {language}. Gherkin keywords stay in
English (Given/When/Then) unless the file starts with `# language: es` (Dado/Cuando/Entonces).

Tag the `Feature` with `@task:{ref}` so the report can trace it back to the task.

Every scenario needs these tags:
- `@UAC-<n>`: unique, numbered from 1 (keep the numbers of existing UACs; new ones continue from {next_id});
- `@e2e` if it is checked through the UI, `@api` if through HTTP, or both;
- `@source:task` when it comes from the task, `@source:code` when you add it because the code does it
  (endpoints, validations, errors) and the task does not say it;
- `@ref:<id>` pointing at the original criterion or issue when there is one (e.g. `@ref:1.2`, `@ref:PROJ-123`).

Rules:
- Observable and testable: concrete inputs, exact outcomes (status codes, messages, what appears on screen).
  "Then it works" is not a criterion.
- Cover the happy path, validation errors, permissions/auth and the edge cases the code shows.
- The task is the source of truth for WHAT must happen. If the code does not do something the task asks
  for, still write that UAC: its test will fail, and that is exactly what the report must show.
- Do not invent product behaviour that neither the task nor the code supports.
- Keep each scenario short (3-7 steps). Use `Scenario Outline` + `Examples` for the same rule with several inputs.

Example:
```gherkin
@task:PROJ-123
Feature: Cart discount
  As a customer I want to apply a discount so that I pay less.

  @UAC-1 @api @source:task @ref:PROJ-123
  Scenario: The discount is subtracted from the total
    Given a cart with products worth 50.00 €
    When I apply a 5.00 € discount with POST /cart/discount
    Then the response is 200
    And the cart total is 45.00 €

  @UAC-2 @api @source:code
  Scenario: A discount larger than the total leaves the total at 0
    Given a cart with products worth 10.00 €
    When I apply a 15.00 € discount
    Then the cart total is 0.00 €
```
"""


def draft(root: Path, slug: str, task: sources.Task, base: str | None = None,
          max_diff_chars: int = 40_000) -> str:
    cfg = load_config(root)
    st = store.settings(cfg.root)
    slug = store.slugify(slug)
    existing = None
    try:
        existing = store.load(cfg.root, slug)
    except FileNotFoundError:
        pass
    parts = [f"# Draft the acceptance criteria (UAC) for: {task.title}",
             f"Source: {task.source} `{task.ref}` · slug `{slug}` · file `{st.dir}/{slug}.feature`", ""]
    parts += ["## The task", task.text.strip(), ""]
    if task.criteria:
        parts += ["## Criteria already written in the task (turn each one into at least one UAC; keep @ref)"]
        parts += [f"- `{c['id']}` {c['text']}" for c in task.criteria]
        parts.append("")
    try:
        changes = gitdiff.collect(cfg.root, base or cfg.base)
    except gitdiff.GitError:
        changes = None
    if changes and changes.files:
        sig = sources.code_signals(changes)
        imp = analyse(cfg.root, changes, cfg.integrations.get("graphskill", "auto"))
        parts += [f"## What the code changed (base: {changes.base_label})"]
        if sig["endpoints"]:
            parts.append("Endpoints added or changed:")
            parts += [f"- {e['method']} {e['path']} ({e['file']}:{e['line']})" for e in sig["endpoints"]]
        if sig["pages"]:
            parts.append("UI files changed: " + ", ".join(sig["pages"][:20]))
        if imp.symbols:
            parts.append("Symbols changed: " + ", ".join(f"`{s['name']}`" for s in imp.symbols[:20]))
        diff, cut = _bounded_diff(changes.diff, max_diff_chars)
        parts += ["", "```diff", diff.rstrip(), "```" + (" (truncated)" if cut else ""), ""]
    else:
        parts += ["## The code", "No change found against the base: write the UACs from the task alone.", ""]
    if existing:
        parts += ["## UACs already saved for this slug (update them; do not renumber)", "```gherkin",
                  store.path(cfg.root, slug).read_text(encoding="utf-8").rstrip(), "```", ""]
    parts.append(DRAFT_RULES.format(slug=slug, language=_LANG.get(st.language, st.language),
                                    next_id=gherkin.next_id(existing), ref=task.ref or slug))
    return "\n".join(parts)


TESTS_RULES = """\
## How to write the tests

Write `{spec_file}` with Playwright Test (TypeScript). One or more tests per UAC; every test title starts
with the UAC id in brackets, e.g. `test('[UAC-1] the discount is subtracted from the total', ...)`. A UAC with no test
is reported as NOT COVERED.

- `@e2e` UACs: use `page` and relative URLs (`page.goto('/cart')`): the base URL is the host of the run.
- `@api` UACs: use the `request` fixture (`request.post('/api/cart/discount', {{ data }})`), same base URL.
- Evidence is collected automatically from what you attach. Import the helpers from `./evidence`:
  - `await shot(page, 'Then: total = 45 €')` after each Then that is visible on screen;
  - `await apiCall(request, 'POST', '/api/cart/discount', {{ amount: 5 }})` instead of calling `request`
    directly: it records request and response in the report and returns the response.
  - `await note('text')` for anything else worth showing (ids created, values read).
- Assert exactly what each Then says (status, body fields, visible texts). No assertion, no evidence.
- Each test must be independent: create the data it needs (API calls, fixtures); do not rely on order.
  If the app under test keeps shared state, add `test.describe.configure({{ mode: 'default' }})` so tests run
  one after another; never `mode: 'serial'`, which skips the rest after a failure and hides their result.
- Never hard-code hosts or credentials; read secrets from environment variables.

Then run them with `uac_run(slug="{slug}", host=...)`.
"""


def tests(root: Path, slug: str) -> str:
    cfg = load_config(root)
    st = store.settings(cfg.root)
    slug = store.slugify(slug)
    feature = store.load(cfg.root, slug)
    files = scaffold.ensure(cfg.root)
    spec_file = f"{st.tests_dir}/{slug}.spec.ts"
    existing = cfg.root / spec_file
    parts = [f"# Write the Playwright tests for the UACs of `{slug}`", ""]
    if files:
        parts.append("Scaffolding created: " + ", ".join(f"`{f}`" for f in files) + ".")
    parts += [f"Feature: {feature.name}", "", "```gherkin",
              store.path(cfg.root, slug).read_text(encoding="utf-8").rstrip(), "```", ""]
    if existing.exists():
        parts += [f"## Current `{spec_file}` (extend it; keep passing tests)", "```ts",
                  existing.read_text(encoding="utf-8")[:20_000].rstrip(), "```", ""]
    parts.append(TESTS_RULES.format(spec_file=spec_file, slug=slug))
    return "\n".join(parts)
