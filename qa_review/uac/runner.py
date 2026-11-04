"""Run the UAC tests of one slug with Playwright against a host and map every result, with its
evidence, back to its acceptance criterion."""

from __future__ import annotations

import base64
import json
import re
import secrets
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from .. import gitdiff, target
from ..checks import Check, _tool, run_one
from ..config import load as load_config
from . import attacks, baseline, gherkin, scope, store, surface

_ID_IN_TITLE = re.compile(r"\bUAC-(\d+)\b", re.IGNORECASE)
_EXT = {"image/png": "png", "image/jpeg": "jpg", "application/json": "json", "text/plain": "txt",
        "text/html": "html", "video/webm": "webm", "application/zip": "zip"}
STATUSES = ("passed", "failed", "flaky", "skipped", "not_covered")


class UacRunError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _walk(suite: dict, out: list, file: str = ""):
    file = suite.get("file") or file
    for spec in suite.get("specs") or []:
        out.append((spec, spec.get("file") or file))
    for child in suite.get("suites") or []:
        _walk(child, out, file)


def _save_attachment(att: dict, evidence_dir: Path, n: int, run_dir: Path) -> dict | None:
    name, ctype = att.get("name") or "attachment", att.get("contentType") or "application/octet-stream"
    item = {"name": name, "contentType": ctype}
    if att.get("body") is not None:
        raw = att["body"]
        try:
            data = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw)
        except (ValueError, TypeError):
            data = str(raw).encode()
        evidence_dir.mkdir(parents=True, exist_ok=True)
        p = evidence_dir / f"{n:03d}.{_EXT.get(ctype, 'bin')}"
        p.write_bytes(data)
        item["path"] = str(p.relative_to(run_dir))
    elif att.get("path"):
        p = Path(att["path"])
        if not p.exists():
            return None
        try:
            item["path"] = str(p.resolve().relative_to(run_dir.resolve()))
        except ValueError:
            item["path"] = str(p)
    else:
        return None
    return item


def parse_results(results: dict, run_dir: Path) -> list[dict]:
    """Playwright JSON report → one entry per test (last retry), attachments saved as files."""
    specs: list = []
    for s in results.get("suites") or []:
        _walk(s, specs)
    tests, n = [], 0
    evidence_dir = run_dir / "evidence"
    for spec, file in specs:
        title = spec.get("title", "")
        ids = {f"UAC-{m}" for m in _ID_IN_TITLE.findall(title)}
        ids |= {t.lstrip("@").upper() for t in spec.get("tags") or [] if _ID_IN_TITLE.search(t)}
        for t in spec.get("tests") or []:
            results_ = t.get("results") or [{}]
            last = results_[-1]
            outcome = t.get("status")           # expected | unexpected | flaky | skipped
            status = {"expected": "passed", "unexpected": "failed", "flaky": "flaky",
                      "skipped": "skipped"}.get(outcome, last.get("status") or "failed")
            atts = []
            for att in last.get("attachments") or []:
                n += 1
                if item := _save_attachment(att, evidence_dir, n, run_dir):
                    atts.append(item)
            err = last.get("error") or ((last.get("errors") or [None])[0]) or {}
            tests.append({"title": title, "uacs": sorted(ids), "file": file, "line": spec.get("line"),
                          "project": t.get("projectName") or "", "status": status,
                          "duration_ms": last.get("duration", 0),
                          "error": _clean(err.get("message") or err.get("value") or "")[:3000],
                          "attachments": atts})
    return tests


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return _ANSI.sub("", text or "")


def _uac_status(tests: list[dict]) -> str:
    if not tests:
        return "not_covered"
    st = [t["status"] for t in tests]
    if any(s in ("failed", "timedOut", "interrupted") for s in st):
        return "failed"
    if all(s == "skipped" for s in st):
        return "skipped"
    if any(s == "flaky" for s in st):
        return "flaky"
    return "passed"


def summarize(feature: gherkin.Feature, tests: list[dict]) -> tuple[list[dict], list[dict], dict]:
    uacs = []
    known = set()
    for sc in feature.scenarios:
        uid = sc.uac_id
        known.add(uid)
        linked = [t for t in tests if uid in t["uacs"]]
        uacs.append({"id": uid, "name": sc.name, "kinds": sc.kinds, "source": sc.tag_value("source"),
                     "ref": sc.tag_value("ref"), "breaks": sc.breaks, "proposed": sc.proposed, "text": sc.text,
                     "status": _uac_status(linked), "tests": linked})
    orphans = [t for t in tests if not t["uacs"] or not set(t["uacs"]) & known]
    required = [u for u in uacs if not u["proposed"]]           # @proposed UACs stay outside the verdict
    counts = {s: sum(1 for u in required if u["status"] == s) for s in STATUSES}
    counts["total"] = len(required)
    proposed = [u for u in uacs if u["proposed"]]
    counts["proposed_total"] = len(proposed)
    counts["proposed_not_met"] = sum(1 for u in proposed if u["status"] == "failed")
    adversarial = [u for u in required if u["breaks"]]
    counts["break_total"] = len(adversarial)
    counts["break_held"] = sum(1 for u in adversarial if u["status"] == "passed")
    counts["break_broken"] = sum(1 for u in adversarial if u["status"] == "failed")
    return uacs, orphans, counts


def missing_tests(root: Path, tests_dir: str, spec: str) -> str:
    """Why there is nothing to run, naming tests written with another framework if that is what happened."""
    d = Path(root) / tests_dir
    other = sorted(str(p.relative_to(root)) for p in d.rglob("*") if p.is_file()
                   and not p.name.endswith((".spec.ts", ".spec.js")) and p.name not in ("evidence.ts", "attacks.ts")
                   ) if d.is_dir() else []
    msg = f"no tests yet: write {spec} with Playwright Test (see uac_tests_packet)"
    if other:
        msg += ("; found " + ", ".join(other[:8]) + (" ..." if len(other) > 8 else "")
                + ": UAC tests are always Playwright (.spec.ts), never Karate, JUnit, pytest or other frameworks")
    return msg


def coverage(uacs: list[dict], plan: list[dict], not_applicable: list[str] | None = None) -> dict:
    """Which categories of the attack plan have at least one @break UAC, which were closed on purpose (@na) and
    which are gaps."""
    covered = sorted({c for u in uacs for c in u["breaks"]})
    planned = sorted({r["category"] for r in plan})
    na = sorted(set(not_applicable or []) - set(covered))
    return {"planned": planned, "covered": covered, "not_applicable": na,
            "gaps": [c for c in planned if c not in covered and c not in na]}


def verdict(counts: dict, battery: dict | None = None) -> str:
    if counts.get("failed") or (battery or {}).get("broken"):
        return "fail"
    if counts.get("not_covered") or counts.get("skipped") or counts.get("flaky"):
        return "incomplete"
    return "pass"


def run(root: Path, slug: str, host: str | None = None, allow_protected: bool = False,
        task_ref: str | None = None, attacks_on: bool | None = None) -> dict:
    cfg = load_config(root)
    st = store.settings(cfg.root)
    slug = store.slugify(slug)
    feature = store.load(cfg.root, slug)
    problems = gherkin.validate(feature)
    if problems:
        raise UacRunError("fix the UACs first:\n- " + "\n- ".join(problems))
    spec = f"{st.tests_dir.strip('/')}/{slug}.spec.ts"
    if not (cfg.root / spec).exists():
        raise UacRunError(missing_tests(cfg.root, st.tests_dir, spec))
    host = target.normalize(host or cfg.host) if (host or cfg.host) else None
    if not host:
        raise UacRunError("UAC tests need a host: pass --host / host (or set `host:` in .qa-review.yaml)")
    if target.is_protected(host, cfg.protected_hosts) and not allow_protected:
        raise target.HostError(f"{host} is a protected host; re-run with allow_protected if you really mean it")

    run_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    run_dir = cfg.root / st.output_dir / slug / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    gi = cfg.root / st.output_dir / ".gitignore"
    if st.output_dir.startswith(".qa-review") and not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    json_path = run_dir / "results.json"
    pw = _tool(cfg.root, "playwright")
    pw = pw if pw and pw != "playwright" else "npx --no-install playwright"
    cmd = f"{pw} test -c {shlex.quote(st.playwright_config)} {shlex.quote(spec)}"
    env = {**target.env_for(host, cfg.host_env), "QA_REVIEW_UAC_JSON": str(json_path),
           "QA_REVIEW_UAC_ARTIFACTS": str(run_dir / "artifacts")}
    started = datetime.now().astimezone()
    result = run_one(Check("uac", cmd, kind="e2e"), cfg.root, cfg.timeout_sec, host=host, env=env)
    if not json_path.exists():
        raise UacRunError(f"Playwright produced no results ({result.status}, exit {result.exit_code}):\n"
                          + result.output[-3000:])
    task_ref = task_ref or next((t.split(":", 1)[1] for t in feature.tags if t.lower().startswith("@task:")), None)
    tests = parse_results(json.loads(json_path.read_text(encoding="utf-8")), run_dir)
    uacs, orphans, counts = summarize(feature, tests)

    # the attack surface, and the automatic battery (after the tests, so it cannot disturb their data)
    try:
        changes = gitdiff.collect(cfg.root, cfg.base)
    except gitdiff.GitError:
        changes = None
    spec = (cfg.qa.get("openapi") or {}).get("spec") if isinstance(cfg.qa.get("openapi"), dict) else None
    eps = surface.surface(cfg.root, changes, spec) if changes else []
    has_ui = bool(changes) and any(surface.sources.is_ui_file(f.path) for f in changes.files if f.status != "deleted")
    comp = scope.component(st)
    plan = attacks.plan(eps, has_ui, scope.is_intermediary(comp), scope.owns_auth(comp))
    go, why = baseline.should_run(host, st.attacks, attacks_on)
    if go and eps:
        battery = baseline.run(host, eps, timeout=st.attack_timeout_sec)
    else:
        battery = {"ran": False, "reason": why if not go else "no HTTP endpoints in the change",
                   "endpoints": len(eps), "cases": 0, "broken": 0, "results": []}
    if scope.is_intermediary(comp) and battery.get("broken"):
        battery["note"] = ("this component is an intermediary: a 5xx may be the next service's answer relayed "
                           "as is; check which service failed before reporting it here")
    dirty = bool(_git(cfg.root, "status", "--porcelain"))
    data = {
        "id": run_id, "slug": slug, "feature": feature.name, "description": feature.description,
        "task_ref": task_ref, "created": started.isoformat(timespec="seconds"),
        "host": host, "branch": _git(cfg.root, "rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git(cfg.root, "rev-parse", "--short", "HEAD") + (" (+ uncommitted changes)" if dirty else ""),
        "project": cfg.root.name, "command": cmd, "exit_code": result.exit_code,
        "duration_sec": result.duration_sec, "output_tail": result.output[-4000:],
        "counts": counts, "verdict": verdict(counts, battery), "uacs": uacs, "orphans": orphans,
        "surface": eps, "attack_plan": plan, "coverage": coverage(uacs, plan, feature.not_applicable), "battery": battery,
        "component": comp,
        "run_dir": str(run_dir),
    }
    (run_dir / "run.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data


def load_run(root: Path, slug: str, run_id: str | None = None) -> dict:
    st = store.settings(root)
    d = Path(root) / st.output_dir / store.slugify(slug)
    runs = sorted(p for p in d.glob("*/run.json")) if d.is_dir() else []
    if run_id:
        runs = [p for p in runs if p.parent.name == run_id]
    if not runs:
        raise FileNotFoundError(f"no UAC runs for '{slug}'" + (f" with id {run_id}" if run_id else ""))
    return json.loads(runs[-1].read_text(encoding="utf-8"))
