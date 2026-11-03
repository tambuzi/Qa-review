"""Packets for the host agent: one to draft the UACs, one to write their tests."""

from __future__ import annotations

from pathlib import Path

from .. import gitdiff
from ..config import load as load_config
from ..impact import analyse
from ..review import _bounded_diff
from . import attacks, existing, gherkin, scaffold, scope, sources, store, surface

_LANG = {"en": "English", "es": "Spanish"}

DRAFT_RULES = """\
## How to write them

Write ONE Gherkin file (one `Feature`, one `Scenario` per acceptance criterion) and save it with
`uac_save(slug="{slug}", gherkin=...)`. Write everything in {language}: the Feature title and description,
scenario names and steps. Translate when the task, the spec or the existing tests are written in another
language; only texts the product shows (quoted messages, labels, buttons) stay exactly as the product writes
them. Gherkin keywords stay in English (Given/When/Then) unless the file starts with `# language: es`
(Dado/Cuando/Entonces).

Tag the `Feature` with `@task:{ref}` so the report can trace it back to the task.

Every scenario needs these tags:
- `@UAC-<n>`: unique, numbered from 1 (keep the numbers of existing UACs; new ones continue from {next_id});
- `@e2e` if it is checked through the UI, `@api` if through HTTP, or both;
- what makes it required, with its `@ref` (`uac_save` checks it and rejects the UAC otherwise):
  - `@source:task @ref:<criterion or issue>`: the task asks for it (`@ref:1.2`, `@ref:PROJ-123`);
  - `@source:code @ref:<file>:<line>`: the code of THIS component implements it and the task does not say it
    (a validation, an error, a header it sets). The ref points to that code;
  - `@source:tests @ref:<file>::<test>`: an existing test of the project already requires it;
  - `@proposed` with a `Why:` line under the Scenario: none of the above, but you think the team should want it.
    Proposals are reported apart and do not change the verdict until someone accepts them.

## Scope: only what this component is responsible for

Before keeping a UAC, ask: **if it fails, would the team that owns this component accept it as their bug?**
If the answer is "it depends" or "no", drop it or make it `@proposed`.
- A rule enforced by another service is not a UAC here. A proxy that forwards payments does not validate the
  amount: the UAC is that the amount reaches the next service intact and its rejection comes back as it is.
- Behaviour the framework or the infrastructure gives for free (a method the route does not define, a route
  that does not exist) is not this change's behaviour unless the code of the change handles it.
- An attack category that does not apply to this task is closed with `@na:<category>` on the Feature and a line
  in its description saying why (`N/A auth: public endpoint, no user data`), never with a filler UAC.

Write two kinds of UAC:

1. **Prove it works** (functional): what the task asks for, plus what the code adds.
2. **Try to break it** (adversarial): tag them `@break:<category>` ({categories}). Use the attack plan
   above for the rows that touch what this component does, more where money, permissions or data writes are
   involved; they need a source like any other UAC. Each one states the hostile input AND the safe outcome the system must show ("Then the
   response is 422 with the message ... And the cart total is unchanged"), never just "it does not crash".
   Think like an attacker and like a careless user: what would make this code produce a wrong result,
   leak data, apply something twice or end in a 5xx?

Rules:
- Observable and testable: concrete inputs, exact outcomes (status codes, messages, what appears on screen).
  "Then it works" is not a criterion.
- Cover the happy path, and the validation errors, permissions and edge cases that the task or this
  component's code define.
- The task is the source of truth for WHAT must happen. If the code does not do something the task asks
  for, still write that UAC: its test will fail, and that is exactly what the report must show.
- Do not invent product behaviour that neither the task nor the code supports.
- Existing tests that call these endpoints are criteria someone already agreed on: turn each one that encodes
  a business rule into a UAC (unless another UAC already covers it), and say so explicitly when an existing test
  contradicts the task: that contradiction is a finding.
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

  @UAC-2 @api @source:task @ref:PROJ-123 @break:boundary
  Scenario: A discount larger than the total leaves the total at 0
    Given a cart with products worth 10.00 €
    When I apply a 15.00 € discount
    Then the cart total is 0.00 €

  @UAC-3 @api @source:code @ref:app/cart.py:41 @break:injection
  Scenario: A script in the coupon code is rejected by the coupon validation
    Given a cart with products worth 50.00 €
    When I apply the coupon "<script>alert(1)</script>"
    Then the response is 422 with the message "Invalid coupon"
    And no response shows a stack trace or a server error

  @UAC-4 @api @proposed @break:concurrency
  Scenario: The same discount sent twice at once is applied once
    Why: a double click on "Apply" would otherwise charge the discount twice; the task does not say it.
    Given a cart with products worth 50.00 €
    When I send the same 5.00 € discount 5 times in parallel
    Then the cart total is 45.00 €
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
        eps = surface.surface(cfg.root, changes, _spec(cfg))
        parts += scope.section(scope.component(st), scope.outbound_calls(changes))
        parts += existing_section(cfg.root, eps)
        parts += attack_section(cfg, changes, bool(sig["pages"]), eps)
    else:
        parts += ["## The code", "No change found against the base: write the UACs from the task alone.", ""]
    if existing:
        parts += ["## UACs already saved for this slug (update them; do not renumber)", "```gherkin",
                  store.path(cfg.root, slug).read_text(encoding="utf-8").rstrip(), "```", ""]
    parts.append(DRAFT_RULES.format(slug=slug, language=_LANG.get(st.language, st.language),
                                    next_id=gherkin.next_id(existing), ref=task.ref or slug,
                                    categories=", ".join(attacks.CATEGORIES)))
    return "\n".join(parts)


def _spec(cfg) -> str | None:
    api = cfg.qa.get("openapi")
    return api.get("spec") if isinstance(api, dict) else None


def existing_section(root: Path, eps: list[dict]) -> list[str]:
    """Existing tests and .feature files of the project that already exercise the endpoints of the change."""
    hits = existing.find(root, eps)
    if not hits:
        return []
    parts = ["## Existing tests of these endpoints (acceptance criteria already written)",
             ("Read them before drafting: each test that checks a business rule becomes a UAC with "
              "`@source:tests @ref:<file>::<test>`; if one contradicts the task, flag it.")]
    for h in hits:
        tests = ", ".join(f"`{t['name']}` (line {t['line']})" for t in h["tests"][:12])
        more = f" and {len(h['tests']) - 12} more" if len(h["tests"]) > 12 else ""
        parts.append(f"- `{h['file']}` → {h['endpoint']}: {tests}{more}")
    return parts + [""]


def attack_section(cfg, changes, has_ui: bool, eps: list[dict] | None = None) -> list[str]:
    """The attack surface of the change and a deterministic plan of what to try to break."""
    if eps is None:
        eps = surface.surface(cfg.root, changes, _spec(cfg))
    comp = scope.component(store.settings(cfg.root))
    rows = attacks.plan(eps, has_ui, scope.is_intermediary(comp), scope.owns_auth(comp))
    if not rows:
        return []
    parts = ["## Attack surface"]
    for e in eps:
        fields = ", ".join(_describe(f) for f in e.get("fields", [])) or "no input fields found"
        params = f" · path params: {', '.join(e['path_params'])}" if e.get("path_params") else ""
        parts.append(f"- {e['method']} {e['path']} ({e['source']}) · {fields}{params}")
    parts += ["", "## Attack plan (candidates: keep the rows this component is responsible for)",
              "| Target | Category | On | Try | Must |",
              "|---|---|---|---|---|"]
    parts += [f"| {r['target']} | `{r['category']}` | {r['on']} | {r['try']} | {r['expect']} |" for r in rows]
    return parts + [""]


def _describe(f: dict) -> str:
    bits = [str(f.get(k)) for k in ("type", "format") if f.get(k)]
    for k, label in (("minimum", "min"), ("maximum", "max"), ("minLength", "minLen"), ("maxLength", "maxLen")):
        if f.get(k) is not None:
            bits.append(f"{label} {f[k]}")
    if f.get("enum"):
        bits.append("one of " + "/".join(map(str, f["enum"][:6])))
    if f.get("required"):
        bits.append("required")
    return f"`{f['name']}`" + (f" ({', '.join(bits)})" if bits else "")


TESTS_RULES = """\
## How to write the tests

Write `{spec_file}` with Playwright Test (TypeScript). One or more tests per UAC; every test title starts
with the UAC id in brackets, e.g. `test('[UAC-1] the discount is subtracted from the total', ...)`. A UAC with no test
is reported as NOT COVERED. Test titles, `shot` labels and `note` texts appear in the report: write them in
{language}, like the UACs.

- `@e2e` UACs: use `page` and relative URLs (`page.goto('/cart')`): the base URL is the host of the run.
- `@api` UACs: use the `request` fixture (`request.post('/api/cart/discount', {{ data }})`), same base URL.
- Evidence is collected automatically from what you attach. Import the helpers from `./evidence`:
  - `await shot(page, 'Then: total = 45 €')` after each Then that is visible on screen;
  - `await apiCall(request, 'POST', '/api/cart/discount', {{ amount: 5 }})` instead of calling `request`
    directly: it records request and response in the report and returns the response. Pass a string to send
    a raw body byte for byte (`'{{"amount": NaN}}'`, malformed JSON, form data) with the headers you choose.
  - `await note('text')` for anything else worth showing (ids created, values read).
- Assert exactly what each Then says (status, body fields, visible texts). No assertion, no evidence.
- Each test must be independent: create the data it needs (API calls, fixtures); do not rely on order.
  If the app under test keeps shared state, add `test.describe.configure({{ mode: 'default' }})` so tests run
  one after another; never `mode: 'serial'`, which skips the rest after a failure and hides their result.
- Never hard-code hosts or credentials; read secrets from environment variables.

### Tests for `@break` UACs: try hard to break it

Import the attack helpers from `./attacks`:
- `PAYLOADS.injection`, `PAYLOADS.unicode`, `PAYLOADS.numbers`, `PAYLOADS.wrongTypes`, `PAYLOADS.strings`, `PAYLOADS.long`:
  loop over them (`for (const v of PAYLOADS.injection) {{ ... }}`) instead of trying one value.
- `await expectSafeError(res)`: the response is a 4xx (not a 5xx, not a 2xx that accepted garbage) and leaks no
  stack traces, SQL errors or file paths. Pass `{{ status: 422 }}` to require an exact status.
- `await expectNoLeak(res)`: for responses that may succeed but must not show internals.
- `await inParallel(5, () => apiCall(request, 'POST', ...))`: concurrency and double-submit attacks.
- `const before = await snapshot(request, '/api/cart')` … `expect(await snapshot(request, '/api/cart')).toEqual(before)`:
  a rejected request must leave the state unchanged.
- UI: `await failApi(page, '**/api/cart/**', 500)` and `await goOffline(page)` to check the UI recovers;
  double-click submit buttons; paste the injection payloads into inputs and check they render as text.

A `@break` UAC whose test fails means the attack broke the system: that is a finding for the report, so never
weaken the assertion to make it pass. The automatic battery also runs simple attacks on every endpoint on its
own (local hosts by default); your tests go deeper, following the business rules.

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
    parts.append(TESTS_RULES.format(spec_file=spec_file, slug=slug, language=_LANG.get(st.language, st.language)))
    return "\n".join(parts)


def iterate(root: Path, slug: str, run: dict) -> str:
    """The next round: attack around what broke, escalate what held, cover the gaps and the untested UACs."""
    st = store.settings(root)
    feature = store.load(root, slug)
    runs_dir = Path(root) / st.output_dir / store.slugify(slug)
    round_no = len(list(runs_dir.glob("*/run.json"))) + 1 if runs_dir.is_dir() else 2
    c = run["counts"]
    uacs = run["uacs"]
    parts = [f"# Next round ({round_no}) for `{slug}`: prove more, break more",
             (f"Last run `{run['id']}` against {run.get('host')}: **{run['verdict'].upper()}** · "
              f"{c['passed']}/{c['total']} UAC passed · {c.get('break_total', 0)} adversarial UACs, "
              f"{c.get('break_held', 0)} held, {c.get('break_broken', 0)} broke the system."), ""]

    proposals = [u for u in uacs if u.get("proposed") and u["status"] == "failed"]
    uacs = [u for u in uacs if not u.get("proposed")]
    broke = [u for u in uacs if u["breaks"] and u["status"] == "failed"]
    if broke:
        parts.append("## It broke here: attack around it")
        parts.append("Bugs cluster. For each one, add 2-3 `@break` UACs nearby: the same attack on the other fields "
                     "and endpoints, bigger/smaller/encoded variants, and combined with another category.")
        for u in broke:
            err = next((t["error"] for t in u["tests"] if t.get("error")), "")
            parts.append(f"- **{u['id']}** {u['name']} (`{', '.join(u['breaks'])}`): {err.splitlines()[0][:200] if err else ''}")
        parts.append("")
    failed = [u for u in uacs if not u["breaks"] and u["status"] == "failed"]
    if failed:
        parts.append("## Functional UACs that failed (defects to report, not to rewrite)")
        parts += [f"- **{u['id']}** {u['name']}" for u in failed]
        parts.append("")
    battery = run.get("battery") or {}
    hits = [r for r in battery.get("results", []) if r["broke"]]
    if hits:
        parts.append("## The automatic battery broke it: turn each finding into a @break UAC")
        parts.append("Write the safe behaviour the system must show for that input, so the finding becomes a "
                     "regression test that stays in the suite.")
        groups: dict[tuple, list[str]] = {}
        for r in hits:
            groups.setdefault((r["endpoint"], r["category"], "; ".join(r["broke"])), []).append(r["label"])
        for (endpoint, category, broke_), labels in groups.items():
            more = f" (+{len(labels) - 6} more)" if len(labels) > 6 else ""
            parts.append(f"- `{category}` {endpoint} · {', '.join(labels[:6])}{more} → {broke_}")
        parts.append("")
    elif not battery.get("ran"):
        parts += [f"Automatic battery did not run: {battery.get('reason') or 'unknown'}.", ""]
    held = [u for u in uacs if u["breaks"] and u["status"] == "passed"]
    if held:
        parts.append("## It held: escalate")
        parts.append("Push these harder: more parallel requests, longer and mixed payloads, other encodings "
                     "(URL, double URL, unicode escapes), the attack through the UI instead of the API, or chained "
                     "with a state change.")
        parts += [f"- {u['id']} {u['name']} (`{', '.join(u['breaks'])}`)" for u in held]
        parts.append("")
    if proposals:
        parts.append("## Proposals not met (questions for the team, not defects until they accept them)")
        parts += [f"- {u['id']} {u['name']}" for u in proposals]
        parts.append("")
    gaps = (run.get("coverage") or {}).get("gaps") or []
    if gaps:
        parts.append("## Gaps in the attack plan (no @break UAC yet)")
        parts.append("Cover a gap only if it touches what this component is responsible for; otherwise close it with "
                     "`@na:<category>` on the Feature and the reason in its description. Never add a filler UAC.")
        plan = run.get("attack_plan") or []
        for g in gaps:
            rows = [r for r in plan if r["category"] == g]
            targets = ", ".join(sorted({r["target"] for r in rows}))
            parts.append(f"- `{g}` on {targets}: {attacks.CATEGORIES[g].ideas}. Must: {attacks.CATEGORIES[g].safe}.")
        parts.append("")
    not_covered = [u for u in run["uacs"] if u["status"] == "not_covered"]
    if not_covered:
        parts.append("## UACs with no test yet")
        parts += [f"- {u['id']} {u['name']}" for u in not_covered]
        parts.append("")
    if len(parts) <= 3:
        parts += [("Nothing broke, nothing is missing. Escalate anyway: pick the riskiest endpoint and try the "
                   "categories it has not seen yet."), ""]
    parts += ["## Current UACs", "```gherkin", store.path(root, slug).read_text(encoding="utf-8").rstrip(), "```", "",
              "## Do this",
              (f"1. Add the new UACs (ids continue from {gherkin.next_id(feature)}) and save the whole file with "
               f"`uac_save(slug=\"{slug}\", ...)`."),
              f"2. Extend `{st.tests_dir}/{store.slugify(slug)}.spec.ts` for them (see `uac_tests_packet`).",
              f"3. Run again with `uac_run(slug=\"{slug}\", host=\"{run.get('host')}\")` and compare with this round."]
    return "\n".join(parts)
