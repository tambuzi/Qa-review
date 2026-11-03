"""UAC files live in the project, versioned with the code: `<uac.dir>/<slug>.feature` (default qa/uac).
Settings come from the `uac:` section of .qa-review.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import CONFIG_NAME
from . import gherkin

FORMATS = ("pdf", "html")


@dataclass
class UacSettings:
    dir: str = "qa/uac"                          # .feature files
    tests_dir: str = "tests/uac"                 # generated Playwright tests
    playwright_config: str = "playwright.uac.config.ts"
    report: list[str] = field(default_factory=lambda: ["pdf", "html"])
    output_dir: str = ".qa-review/uac"           # runs, results and reports
    language: str = "en"                         # language of the UACs and the report (en | es)
    attacks: str = "local"                       # automatic battery: local (local hosts only) | always | off
    attack_timeout_sec: float = 10               # per request of the automatic battery
    component: dict = field(default_factory=dict)  # {role, owns, delegates}: what this component is responsible for


def settings(root: Path) -> UacSettings:
    p = Path(root) / CONFIG_NAME
    data = {}
    if p.exists():
        data = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("uac") or {}
    s = UacSettings(**{k: v for k, v in data.items() if k in UacSettings.__dataclass_fields__})
    s.report = parse_formats(s.report)
    if isinstance(s.attacks, bool):                   # YAML reads `off`/`on` as booleans
        s.attacks = "always" if s.attacks else "off"
    s.attacks = str(s.attacks).lower()
    if s.attacks not in ("local", "always", "off"):
        raise ValueError(f"uac.attacks must be local, always or off, not {s.attacks!r}")
    return s


def parse_formats(value) -> list[str]:
    items = value.split(",") if isinstance(value, str) else list(value or [])
    out = [str(x).strip().lower() for x in items if str(x).strip()]
    if out == ["both"]:
        out = list(FORMATS)
    bad = [x for x in out if x not in FORMATS]
    if bad or not out:
        raise ValueError(f"report format must be pdf, html or both, not {value!r}")
    return out


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if not s:
        raise ValueError("empty slug")
    return s[:80]


def path(root: Path, slug: str) -> Path:
    return Path(root) / settings(root).dir / f"{slugify(slug)}.feature"


def save(root: Path, slug: str, text: str) -> tuple[Path, gherkin.Feature]:
    from . import scope
    feature = gherkin.parse(text)
    problems = gherkin.validate(feature) + scope.problems(root, feature)
    if problems:
        raise gherkin.GherkinError("the UACs are not valid yet:\n- " + "\n- ".join(problems))
    p = path(root, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.rstrip() + "\n", encoding="utf-8")
    return p, feature


def load(root: Path, slug: str) -> gherkin.Feature:
    p = path(root, slug)
    if not p.exists():
        raise FileNotFoundError(f"no UACs for '{slug}' ({p.relative_to(root)}): draft them first")
    return gherkin.parse(p.read_text(encoding="utf-8"))


def all_slugs(root: Path) -> list[str]:
    d = Path(root) / settings(root).dir
    return sorted(p.stem for p in d.glob("*.feature")) if d.is_dir() else []
