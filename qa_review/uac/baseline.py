"""The automatic robustness battery: attacks every endpoint of the change over HTTP and checks invariants
that hold for any API, whatever the task says:

- no 5xx: bad input is the client's fault (4xx), never a crash;
- no leaks: error responses do not show stack traces, SQL errors, file paths or debug pages;
- no reflected XSS: an injected <script> never comes back unescaped in an HTML response;
- no hang: every request answers within the time limit.

It needs no agent and no Playwright. Because it sends hostile input that may be written if the API accepts
it, it runs by default only against local hosts (localhost, 127.0.0.1, docker service names, .local/.test).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

from .attacks import LEAK_PATTERNS, PAYLOADS

_LEAK = [re.compile(p) for p in LEAK_PATTERNS]
_XSS = "<script>alert('qa')</script>"
MODES = ("local", "always", "off")
_LOCAL_SUFFIXES = (".localhost", ".local", ".test", ".internal")


def is_local(host: str) -> bool:
    name = (urlsplit(host).hostname or "").lower()
    try:
        ip = ipaddress.ip_address(name)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return name == "localhost" or "." not in name or name.endswith(_LOCAL_SUFFIXES)


def should_run(host: str, mode: str, forced: bool | None) -> tuple[bool, str]:
    if forced is False or mode == "off":
        return False, "disabled"
    if forced or mode == "always":
        return True, "enabled"
    if is_local(host):
        return True, "local host"
    return False, "not a local host: pass attacks=True / --attacks to run the automatic battery against it"


def _fill(path: str, value: str = "1") -> str:
    return re.sub(r"\{\w+\}|<(?:\w+:)?\w+>|:(\w+)", quote(value, safe=""), path)


def cases(ep: dict) -> list[dict]:
    """The attacks for one endpoint: (label, category, method, path, body or raw, headers)."""
    method = ep["method"] if ep["method"] not in ("ANY", "ROUTE", "API_ROUTE", "MATCH", "ALL") else "POST"
    path = _fill(ep["path"])
    fields = [f["name"] for f in ep.get("fields", [])][:6]
    sends_body = method in ("POST", "PUT", "PATCH")
    valid = {f: 1 for f in fields}
    out = []
    if sends_body:
        out += [
            {"label": "malformed JSON", "category": "types", "method": method, "path": path,
             "raw": b'{"broken": ', "headers": {"Content-Type": "application/json"}},
            {"label": "wrong Content-Type", "category": "types", "method": method, "path": path,
             "raw": b"amount=1&x=<script>", "headers": {"Content-Type": "text/plain"}},
            {"label": "empty object", "category": "types", "method": method, "path": path, "json": {}},
            {"label": "JSON array instead of object", "category": "types", "method": method, "path": path,
             "json": [valid]},
            {"label": "2 MB body", "category": "http", "method": method, "path": path,
             "json": {**valid, "padding": "x" * 2_000_000}},
        ]
        for f in fields:
            for v in PAYLOADS["wrong_types"][:4]:
                out.append({"label": f"{f} = {json.dumps(v)}", "category": "types", "method": method, "path": path,
                            "json": {**valid, f: v}})
            for v in PAYLOADS["numbers"][:8]:
                out.append({"label": f"{f} = {v!r}", "category": "boundary", "method": method, "path": path,
                            "json": {**valid, f: v}})
            for v in PAYLOADS["injection"][:9]:
                out.append({"label": f"{f} = {v!r}", "category": "injection", "method": method, "path": path,
                            "json": {**valid, f: v}})
            out.append({"label": f"{f} = 10k chars", "category": "boundary", "method": method, "path": path,
                        "json": {**valid, f: "a" * 10_000}})
            out.append({"label": f"{f} = unicode", "category": "unicode", "method": method, "path": path,
                        "json": {**valid, f: PAYLOADS["unicode"][1] + PAYLOADS["unicode"][0]}})
    elif fields:
        for f in fields:
            for v in PAYLOADS["injection"][:6] + ["-1", "99999999999999999999", "a" * 5000]:
                q = f"{path}{'&' if '?' in path else '?'}{quote(f)}={quote(str(v), safe='')}"
                out.append({"label": f"?{f}={v!r}"[:80], "category": "injection" if v in PAYLOADS["injection"] else "boundary",
                            "method": method, "path": q})
    for v in ("-1", "0", "99999999999999999999", "abc", "../../etc/passwd", "%00", "'"):
        if ep.get("path_params"):
            out.append({"label": f"path param = {v!r}", "category": "boundary" if v[0].isdigit() or v[0] == "-" else "injection",
                        "method": "GET" if method == "DELETE" else method, "path": _fill(ep["path"], v),
                        **({"json": valid} if sends_body else {})})
    return out


def _send(host: str, case: dict, timeout: float, token: str | None) -> dict:
    url = host.rstrip("/") + case["path"]
    headers = {"Accept": "application/json, text/html;q=0.9, */*;q=0.1", "User-Agent": "qa-review-battery"}
    headers.update(case.get("headers") or {})
    data = case.get("raw")
    if "json" in case:
        data = json.dumps(case["json"]).encode()
        headers.setdefault("Content-Type", "application/json")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=case["method"], headers=headers)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:      # http(s) host validated upstream
            status, ctype, body = r.status, r.headers.get("Content-Type", ""), r.read(200_000)
    except urllib.error.HTTPError as e:
        status, ctype, body = e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read(200_000)
    except TimeoutError:
        return {"status": None, "error": f"no answer in {timeout:g}s", "ms": int(timeout * 1000)}
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ConnectionRefusedError):
            error = "connection refused: the server is down (did an earlier attack kill it?)"
        elif isinstance(reason, (ConnectionResetError, ConnectionAbortedError)) or "RemoteDisconnected" in type(e).__name__ \
                or "closed connection" in str(reason):
            error = "the server closed the connection without answering (it crashed on this input)"
        else:
            error = f"no answer: {type(reason).__name__}: {reason}"
        return {"status": None, "error": error, "ms": int((time.monotonic() - start) * 1000)}
    return {"status": status, "content_type": ctype, "body": body.decode("utf-8", "replace"),
            "ms": int((time.monotonic() - start) * 1000)}


def check(case: dict, res: dict) -> list[str]:
    """Which invariants the response breaks (empty = it held)."""
    broke = []
    if res.get("status") is None:
        broke.append(res.get("error") or "no response")
        return broke
    if res["status"] >= 500:
        broke.append(f"server error {res['status']}")
    body = res.get("body") or ""
    for rx in _LEAK:
        if m := rx.search(body):
            broke.append(f"leaks internals: {m.group(0)[:80]!r}")
            break
    sent = json.dumps(case.get("json", ""), ensure_ascii=False) + case["path"]
    if "html" in (res.get("content_type") or "") and _XSS in body and (_XSS in sent or quote(_XSS, safe="") in sent):
        broke.append("reflects <script> unescaped (XSS)")
    return broke


def reachable(host: str, timeout: float) -> str | None:
    """None when the host answers anything (any status); otherwise why not."""
    res = _send(host, {"method": "GET", "path": "/"}, timeout, None)
    return None if res.get("status") is not None else res.get("error")


def run(host: str, endpoints: list[dict], timeout: float = 10, max_cases: int = 400) -> dict:
    token = os.environ.get("QA_REVIEW_API_TOKEN")
    if why := reachable(host, timeout):
        return {"ran": False, "reason": f"{host} is not reachable: {why}", "results": [],
                "endpoints": len(endpoints), "cases": 0, "broken": 0}
    results, n = [], 0
    for ep in endpoints:
        for case in cases(ep):
            if n >= max_cases:
                break
            n += 1
            res = _send(host, case, timeout, token)
            broke = check(case, res)
            results.append({"endpoint": f"{ep['method']} {ep['path']}", "label": case["label"],
                            "category": case["category"], "request": {"method": case["method"], "path": case["path"][:300]},
                            "status": res.get("status"), "ms": res.get("ms"), "broke": broke,
                            "response": (res.get("body") or "")[:600] if broke else ""})
    return {"ran": True, "reason": "", "endpoints": len(endpoints), "cases": len(results),
            "broken": sum(1 for r in results if r["broke"]), "results": results}
