"""Functional QA: end-to-end (Playwright) and API contract fuzzing (Schemathesis).
Both become ordinary checks, so they run, time out and report like the rest."""

from __future__ import annotations

from pathlib import Path

from .checks import _tool
from .config import Check, Config

_PW_CONFIGS = ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs", "playwright.config.cjs")
_SPEC_NAMES = ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.yaml", "swagger.yml", "swagger.json")


def find_spec(root: Path) -> str | None:
    for name in _SPEC_NAMES:
        for cand in (root / name, root / "docs" / name, root / "api" / name):
            if cand.exists():
                return str(cand.relative_to(root))
    return None


def plan(cfg: Config, host: str | None = None) -> tuple[list[Check], list[str]]:
    """(checks, notes). Notes explain what could be enabled and how."""
    root, qa = cfg.root, cfg.qa or {}
    checks, notes = [], []

    pw = qa.get("playwright")
    if pw is False or pw == "off":
        pass
    elif isinstance(pw, dict) and pw.get("cmd"):
        checks.append(Check("playwright", pw["cmd"], blocking=pw.get("blocking", True), kind="e2e"))
    elif any((root / c).exists() for c in _PW_CONFIGS):
        if exe := _tool(root, "playwright"):
            checks.append(Check("playwright", f"{exe} test --reporter=line", kind="e2e", source="auto"))
        else:
            notes.append("Playwright config found but Playwright is not installed: npm i -D @playwright/test")

    api = qa.get("openapi")
    if isinstance(api, dict) and api.get("spec") and not api.get("base_url") and not api.get("cmd"):
        api = {**api, "base_url": "{host}"}          # no fixed URL: test whatever host the run targets
    if isinstance(api, dict) and (api.get("cmd") or (api.get("spec") and api.get("base_url"))):
        if api.get("cmd"):
            cmd = api["cmd"]
        else:
            exe = _tool(root, "schemathesis") or _tool(root, "st")
            url = "{host}" if host else api["base_url"]   # an explicit host wins over the configured URL
            cmd = f"{exe} run {api['spec']} --url {url}" if exe else None
            if not exe:
                notes.append("openapi configured but Schemathesis is not installed: pip install schemathesis")
        if cmd:
            checks.append(Check("schemathesis", cmd, blocking=api.get("blocking", True), kind="api"))
    elif spec := find_spec(root):
        notes.append(f"API spec {spec} found: add `qa: {{openapi: {{spec: {spec}, base_url: ...}}}}` to "
                     ".qa-review.yaml to fuzz the API against it")
    return checks, notes
