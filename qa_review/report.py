"""Verdict and rendering. The verdict comes from fixed rules so it is predictable:

- block: a blocking check failed (or timed out / could not run), or any finding is critical,
         or the review reported a high finding;
- warn:  a non-blocking check failed, a medium+ finding, a security flag the review has not
         cleared, or changed code with no test found;
- pass:  none of the above.
"""

from __future__ import annotations

from .findings import Finding, rank


def all_findings(run: dict) -> list[Finding]:
    out = [Finding.from_dict(f, "security") for f in run.get("security", {}).get("findings", [])]
    review = run.get("review") or {}
    out += [Finding.from_dict(f, "review") for f in review.get("findings", [])]
    return out


def verdict(run: dict) -> tuple[str, list[str]]:
    reasons_block, reasons_warn = [], []
    for c in run.get("checks", []):
        if c["status"] in ("fail", "timeout", "error"):
            (reasons_block if c["blocking"] else reasons_warn).append(f"check `{c['name']}` {c['status']}")
    review = run.get("review") or {}
    cleared = set(review.get("cleared_rules") or [])
    for f in all_findings(run):
        r = rank(f.severity)
        where = f" ({f.file}:{f.line})" if f.file and f.line else (f" ({f.file})" if f.file else "")
        if f.source == "security" and f.rule in cleared and f.severity != "critical":
            continue
        if r >= rank("critical") or (f.source == "review" and r >= rank("high")):
            reasons_block.append(f"{f.severity}: {f.title}{where}")
        elif r >= rank("medium"):
            reasons_warn.append(f"{f.severity}: {f.title}{where}")
    if review.get("verdict") == "block" and not reasons_block:
        reasons_block.append("the reviewer blocked the change")
    untested = (run.get("impact") or {}).get("untested") or []
    if untested:
        reasons_warn.append(f"{len(untested)} changed file(s) without a test found: " + ", ".join(untested[:5]))
    if reasons_block:
        return "block", reasons_block + reasons_warn
    if reasons_warn:
        return "warn", reasons_warn
    return "pass", []


_ICON = {"pass": "✅", "warn": "⚠️", "block": "⛔", "fail": "❌", "timeout": "⏱", "error": "❗", "skipped": "➖"}


def markdown(run: dict) -> str:
    v, reasons = run.get("verdict", "?"), run.get("reasons", [])
    s = run.get("stats", {})
    lines = [f"# qa-review {_ICON.get(v, '')} {v.upper()}", "",
             (f"Base: `{run.get('base_label')}` · {s.get('files', 0)} files, "
              f"+{s.get('added', 0)} / -{s.get('removed', 0)} · run `{run.get('id')}`"), ""]
    if reasons:
        lines += ["## Why", *[f"- {r}" for r in reasons], ""]
    if run.get("checks"):
        lines += ["## Checks", "| | check | kind | time | cmd |", "|---|---|---|---|---|"]
        for c in run["checks"]:
            tag = " (targeted)" if c.get("targeted") else ""
            lines.append(f"| {_ICON.get(c['status'], c['status'])} | {c['name']}{tag} | {c['kind']} | "
                         f"{c['duration_sec']}s | `{c['cmd']}` |")
        for c in run["checks"]:
            if c["status"] in ("fail", "timeout", "error") and c.get("output"):
                tail = "\n".join(c["output"].splitlines()[-25:])
                lines += ["", f"<details><summary>{c['name']} output</summary>", "", "```", tail, "```", "</details>"]
        lines.append("")
    findings = sorted(all_findings(run), key=lambda f: -rank(f.severity))
    if findings:
        lines += ["## Findings"]
        for f in findings:
            where = f" `{f.file}:{f.line}`" if f.file and f.line else (f" `{f.file}`" if f.file else "")
            lines.append(f"- **{f.severity}** [{f.source}] {f.title}{where}" + (f" — {f.detail}" if f.detail else ""))
        lines.append("")
    imp = run.get("impact") or {}
    if imp:
        lines += ["## Impact", f"Source: {imp.get('source')}" + (f" — {imp.get('note')}" if imp.get("note") else "")]
        if imp.get("symbols"):
            names = ", ".join(f"`{x['name']}`" for x in imp["symbols"][:15])
            lines.append(f"- Changed symbols: {names}")
        ncallers = sum(len(v) for v in (imp.get("callers") or {}).values())
        if ncallers:
            lines.append(f"- Call sites of changed symbols: {ncallers} across {len(imp.get('affected_files', []))} files")
        if imp.get("tests"):
            lines.append(f"- Tests covering the change: {', '.join(imp['tests'][:10])}")
        if imp.get("untested"):
            lines.append(f"- No test found for: {', '.join(imp['untested'])}")
        lines.append("")
    review = run.get("review")
    lines += ["## Review", (f"Done by {review.get('reviewer', 'agent')}: {review.get('summary', '')}"
                            if review else "Pending: call `qa_review_packet` and then `qa_submit_review`."), ""]
    if run.get("qa_notes"):
        lines += ["## Suggestions", *[f"- {n}" for n in run["qa_notes"]], ""]
    integ = run.get("integrations") or {}
    if integ:
        lines += ["## Integrations", *[f"- {k}: {v}" for k, v in integ.items()], ""]
    return "\n".join(lines)
