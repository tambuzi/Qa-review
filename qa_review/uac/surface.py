"""The attack surface of a change: the HTTP endpoints it touches, with their path params and input fields.

Sources, merged:
- the code: routes declared in the changed files (FastAPI, Flask, Express, Laravel, Symfony, Nest/Spring,
  Django) and the input fields those files read;
- the OpenAPI/Swagger spec, when there is one: methods, parameters and request body fields with their types
  and limits (min, max, enum, required), for the endpoints the change touches.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from ..security import is_source, is_test
from . import sources

_PARAM = re.compile(r"\{(\w+)\}|<(?:\w+:)?(\w+)>|:(\w+)")
_FIELDS = [re.compile(p) for p in (
    r"""\[\s*["'](\w+)["']\s*\]""",                       # data["amount"]
    r"""\.get\(\s*["'](\w+)["']""",                         # body.get("amount")
    r"""\b(?:body|payload|data|params|query|form|json)\.(\w+)\b""",  # req.body.amount, body.amount
    r"""\b(?:input|query|post|get)\(\s*["'](\w+)["']""",    # $request->input('amount')
    r"""^\s*(\w+)\s*:\s*(?:int|float|str|bool|Decimal|list|dict|conint|confloat|constr)\b""",  # pydantic fields
)]
_NOT_FIELDS = {"self", "cls", "return", "id", "the", "get", "post", "json", "data", "body", "error", "status",
               "message", "headers", "method", "path", "query", "params", "form"}
_ROUTE_ROLE = re.compile(r"""@(?:\w+\.)?(get|post|put|patch|delete|route|api_route)\(|\b(?:app|router|api)\.(get|post|put|patch|delete|all)\(|Route::|#\[Route|@(Get|Post|Put|Patch|Delete)|\bpath\(""")


def norm(path: str) -> str:
    return _PARAM.sub("{}", path.rstrip("/") or "/")


def path_params(path: str) -> list[str]:
    return [next(g for g in m.groups() if g) for m in _PARAM.finditer(path)]


def _fields_in(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        for rx in _FIELDS:
            for m in rx.finditer(line):
                name = m.group(1)
                if name and name.lower() not in _NOT_FIELDS and not name.startswith("_") and name not in found:
                    found.append(name)
    return found[:20]


def from_code(root: Path, changes) -> list[dict]:
    """Every route declared in a changed source file (the whole file, not only added lines)."""
    endpoints = []
    for f in changes.files:
        if f.status == "deleted" or not is_source(f.path) or is_test(f.path):
            continue
        try:
            text = (Path(root) / f.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _ROUTE_ROLE.search(text):
            continue
        fake = type("C", (), {"files": [type("F", (), {"status": "added", "path": f.path,
                                                        "added": list(enumerate(text.splitlines(), 1))})()]})()
        fields = _fields_in(text)
        for ep in sources.code_signals(fake)["endpoints"]:
            endpoints.append({"method": ep["method"], "path": ep["path"], "file": ep["file"], "line": ep["line"],
                              "path_params": path_params(ep["path"]),
                              "fields": [{"name": n, "type": None, "in": "body"} for n in fields],
                              "source": "code"})
    return endpoints


def _load_spec(root: Path, spec: str | None) -> dict:
    from ..qa import find_spec
    rel = spec or find_spec(Path(root))
    if not rel:
        return {}
    p = Path(root) / rel
    try:
        text = p.read_text(encoding="utf-8")
        data = json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _resolve(spec: dict, node, depth: int = 0):
    if isinstance(node, dict) and "$ref" in node and depth < 5:
        target = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return _resolve(spec, target, depth + 1)
    return node if isinstance(node, dict) else {}


def from_openapi(root: Path, spec_path: str | None = None) -> list[dict]:
    spec = _load_spec(root, spec_path)
    base = ""
    if spec.get("basePath"):
        base = str(spec["basePath"]).rstrip("/")
    out = []
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        shared = ops.get("parameters") or []
        for method, op in ops.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                continue
            fields = []
            for prm in shared + (op.get("parameters") or []):
                prm = _resolve(spec, prm)
                if prm.get("in") in ("query", "body", "formData"):
                    sch = _resolve(spec, prm.get("schema") or prm)
                    if prm.get("in") == "body" and sch.get("properties"):
                        fields += _props(spec, sch)
                    else:
                        fields.append(_field(prm.get("name"), sch, prm.get("required"), prm.get("in")))
            body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            if body:
                fields += _props(spec, _resolve(spec, body))
            out.append({"method": method.upper(), "path": base + path, "path_params": path_params(path),
                        "fields": [f for f in fields if f.get("name")], "source": "openapi"})
    return out


def _field(name, sch: dict, required, where) -> dict:
    keep = {k: sch[k] for k in ("type", "format", "minimum", "maximum", "minLength", "maxLength", "enum", "pattern")
            if k in sch}
    return {"name": name, "required": bool(required), "in": where, **keep}


def _props(spec: dict, sch: dict) -> list[dict]:
    req = set(sch.get("required") or [])
    return [_field(n, _resolve(spec, s), n in req, "body") for n, s in (sch.get("properties") or {}).items()]


def _spec_paths_in_changed_files(root: Path, changes, spec_eps: list[dict]) -> list[dict]:
    """Spec endpoints whose path appears literally in a changed source file (custom routers, if/elif dispatch)."""
    texts = []
    for f in changes.files:
        if f.status != "deleted" and is_source(f.path) and not is_test(f.path):
            try:
                texts.append((Path(root) / f.path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                pass
    found = []
    for e in spec_eps:
        rx = re.compile(r"""["'`]""" + _PARAM.sub(r"[^\"'`/]+", re.escape(e["path"]).replace(r"\{", "{").replace(r"\}", "}"))
                        + r"""["'`/?]""")
        if any(rx.search(t) for t in texts):
            found.append({**e, "source": "openapi (path used in changed code)"})
    return found


def surface(root: Path, changes, spec_path: str | None = None) -> list[dict]:
    """Endpoints the change touches, enriched with the spec's fields when the spec describes them."""
    code = from_code(root, changes)
    spec_eps = from_openapi(root, spec_path)
    api = {(e["method"], norm(e["path"])): e for e in spec_eps}
    out = []
    for e in code:
        key = (e["method"], norm(e["path"]))
        match = api.get(key) or next((v for (m, p), v in api.items() if p == key[1] and e["method"] in ("ANY", m)), None)
        if match:
            e = {**e, "fields": match["fields"] or e["fields"], "path_params": match["path_params"] or e["path_params"],
                 "source": "code+openapi"}
        out.append(e)
    known = {(e["method"], norm(e["path"])) for e in out}
    out += [e for e in _spec_paths_in_changed_files(root, changes, spec_eps) if (e["method"], norm(e["path"])) not in known]
    return out
