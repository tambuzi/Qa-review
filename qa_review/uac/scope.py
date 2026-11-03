"""What this component is responsible for, and whether each UAC stays inside it.

A UAC is only worth having if, when it fails, the team that owns this component would accept it as their bug.
Two things decide that:

- **The component's role** (`uac.component` in .qa-review.yaml): a proxy or gateway owns forwarding, not the
  business rules of the service behind it. When it is not configured and the change calls other services, the
  draft packet says so and asks the agent to confirm the role with the user.
- **Traceability**: every UAC points to what makes it required: a criterion of the task, the code of this change
  that implements it, or an existing test. Anything else is the QA's proposal (`@proposed` + `Why:`), reported
  apart and outside the verdict.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import gherkin, sources

INTERMEDIARY_ROLES = ("proxy", "reverse-proxy", "gateway", "api-gateway", "bff", "router", "intermediary", "facade")

# outbound HTTP calls in added lines: the change talks to another service
_OUTBOUND = {   # by language, so a fetch() inside an HTML string of a Python file is not taken for a call
    (".py",): r"\b(httpx|requests|aiohttp|urllib3)\.(get|post|put|patch|delete|request|stream|AsyncClient|Client"
              r"|ClientSession)\b",
    (".js", ".mjs", ".cjs", ".ts"): r"\bfetch\(|\baxios(\.\w+)?\(|\bgot(\.\w+)?\(|\bundici\b|\bhttps?\.request\("
                                    r"|createProxyMiddleware|http-proxy|\bproxy\.web\(",
    (".php",): r"\bHttp::(get|post|put|patch|delete|withHeaders|withToken|send)\b|GuzzleHttp|\$client->request\(",
    (".java", ".kt"): r"\bRestTemplate\b|\bWebClient\b|\bHttpClient\b|@FeignClient|\.exchange\(",
    (".go",): r"\bhttp\.(Get|Post|NewRequest)\(|httputil\.NewSingleHostReverseProxy|\.Do\(req",
    (".rb",): r"\bFaraday\b|Net::HTTP|HTTParty",
    (".conf",): r"\bproxy_pass\b",
}
_OUTBOUND = {exts: re.compile(rx) for exts, rx in _OUTBOUND.items()}


def component(settings) -> dict:
    """The component as configured: {role, owns: [...], delegates: {rule: service}} (all optional)."""
    raw = getattr(settings, "component", None) or {}
    if not isinstance(raw, dict):
        raise TypeError("uac.component must be a mapping: {role, owns, delegates}")
    owns = raw.get("owns") or []
    owns = [owns] if isinstance(owns, str) else [str(x) for x in owns]
    delegates = raw.get("delegates") or {}
    if isinstance(delegates, list):                    # ["payment validation: payments-service", ...]
        delegates = dict((str(x).split(":", 1) + [""])[:2] for x in delegates)
    delegates = {str(k).strip(): str(v).strip() for k, v in delegates.items()}
    role = str(raw.get("role") or "").strip().lower() or None
    return {"role": role, "owns": owns, "delegates": delegates}


def is_intermediary(comp: dict) -> bool:
    return (comp.get("role") or "") in INTERMEDIARY_ROLES


def owns_auth(comp: dict) -> bool:
    return any(re.search(r"auth|token|permission|credential", o, re.IGNORECASE) for o in comp.get("owns", []))


def outbound_calls(changes, limit: int = 12) -> list[dict]:
    """Added lines (outside UI files) that call another service over HTTP."""
    out = []
    for f in getattr(changes, "files", []) or []:
        if f.status == "deleted" or sources.is_ui_file(f.path):
            continue
        rx = next((r for exts, r in _OUTBOUND.items() if f.path.lower().endswith(exts)), None)
        if rx is None:
            continue
        for n, line in f.added:
            if rx.search(line):
                out.append({"file": f.path, "line": n, "code": line.strip()[:160]})
                if len(out) >= limit:
                    return out
    return out


def section(comp: dict, calls: list[dict]) -> list[str]:
    """The part of the draft packet that says what this component owns (or asks to find out)."""
    if comp.get("role") or comp.get("owns") or comp.get("delegates"):
        parts = ["## What this component owns (uac.component)",
                 f"Role: **{comp.get('role') or 'not set'}**"]
        if comp.get("owns"):
            parts.append("Owns: " + ", ".join(comp["owns"]))
        if comp.get("delegates"):
            parts.append("Delegates (rules enforced by another service, NOT here):")
            parts += [f"- {rule} → {svc or 'another service'}" for rule, svc in comp["delegates"].items()]
        parts.append("A rule this component delegates is not a UAC here. Test instead what it owns about it: the "
                     "input reaches the next service intact and its answer comes back to the client as it is.")
        if is_intermediary(comp):
            parts.append("This component is an intermediary: the attack plan below covers forwarding, answers of "
                         "the next service, headers and routing, not the business rules behind it.")
        return parts + [""]
    if calls:
        parts = ["## This change calls other services",
                 "It may be an intermediary (proxy, gateway, BFF) that does not own the business rules behind it:"]
        parts += [f"- `{c['file']}:{c['line']}` {c['code']}" for c in calls[:8]]
        parts.append("Before drafting, confirm with the user what this component owns and what it delegates, and "
                     "suggest saving it in `.qa-review.yaml` so nobody has to guess again:")
        parts += ["```yaml", "uac:", "  component:", "    role: proxy        # proxy | gateway | bff | service | frontend",
                  "    owns: [routing, auth headers, timeouts, error mapping]",
                  "    delegates: {payment validation: payments-service}", "```"]
        return parts + [""]
    return []


def _criteria_ids(root: Path, feature: gherkin.Feature) -> tuple[str | None, set[str]]:
    task = next((t.split(":", 1)[1] for t in feature.tags if t.lower().startswith("@task:")), None)
    req = Path(root) / ".kiro" / "specs" / (task or "") / "requirements.md"
    if task and req.is_file():
        return task, {c["id"] for c in sources.parse_kiro_requirements(req.read_text(encoding="utf-8"))}
    return task, set()


def _code_ref(ref: str) -> tuple[str, int | None]:
    file, _, line = ref.rpartition(":")
    return (file, int(line)) if file and line.isdigit() else (ref, None)


def problems(root: Path, feature: gherkin.Feature) -> list[str]:
    """UACs that are not traceable to the task, to the code of this component or to an existing test."""
    root = Path(root)
    task, criteria = _criteria_ids(root, feature)
    out = []
    for s in feature.scenarios:
        if s.proposed:
            continue
        where = f"{s.uac_id or s.name} (line {s.line})"
        src, ref = (s.tag_value("source") or "").lower(), s.tag_value("ref")
        if not src:
            out.append(f"{where}: say what makes it required: @source:task, @source:code or @source:tests with its "
                       "@ref, or mark it @proposed with a 'Why:' line")
        elif src == "task":
            if not ref:
                out.append(f"{where}: @source:task needs @ref:<criterion or issue> (e.g. @ref:1.2, @ref:PROJ-123)")
            elif criteria and re.fullmatch(r"\d+(\.\d+)*", ref) and ref not in criteria:
                out.append(f"{where}: @ref:{ref} is not a criterion of .kiro/specs/{task} "
                           f"(it has {', '.join(sorted(criteria))})")
        elif src == "code":
            if not ref:
                out.append(f"{where}: @source:code must point to the code of this component that implements it "
                           "(@ref:<file>:<line>). If no code here does it, it is not this component's behaviour: drop "
                           "it, or mark it @proposed with a 'Why:' line")
            elif re.fullmatch(r"\d+(\.\d+)*|[A-Z][A-Z0-9]+-\d+", ref):
                out.append(f"{where}: @ref:{ref} is a criterion of the task, not code: use @source:task @ref:{ref}, "
                           "or point @source:code to the code that implements it (@ref:<file>:<line>)")
            else:
                file, line = _code_ref(ref)
                p = root / file
                if not p.is_file():
                    out.append(f"{where}: @ref:{ref}: {file} does not exist in the project")
                elif line is not None:
                    n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
                    if not 1 <= line <= n:
                        out.append(f"{where}: @ref:{ref}: {file} has {n} lines")
        elif src == "tests":
            file = (ref or "").split("::", 1)[0]
            if not file:
                out.append(f"{where}: @source:tests needs @ref:<file>::<test>")
            elif not (root / file).is_file():
                out.append(f"{where}: @ref:{ref}: {file} does not exist in the project")
    return out
