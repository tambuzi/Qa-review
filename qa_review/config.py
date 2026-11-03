"""Project settings: `.qa-review.yaml` at the repo root. Everything is optional.

Example:

    base: main                      # what to diff against (default: auto)
    checks:                         # replaces auto-detected checks when given
      - {name: lint,  cmd: "ruff check ."}
      - {name: tests, cmd: "pytest -q", blocking: true}
    qa:
      playwright: {cmd: "npx playwright test"}      # auto-detected if a playwright config exists
      openapi: {spec: openapi.yaml, base_url: "http://localhost:8000"}
    review:
      rules: ["Every public endpoint validates its input"]
    integrations:
      graphskill: auto              # auto | off
      agenticos: auto               # auto | off
    timeout_sec: 600
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_NAME = ".qa-review.yaml"
STATE_DIR = ".qa-review"


@dataclass
class Check:
    name: str
    cmd: str
    blocking: bool = True
    kind: str = "check"          # lint | types | tests | security | e2e | api | check
    source: str = "config"       # config | auto
    targeted_cmd: str | None = None   # same check limited to {targets} (e.g. "pytest -q {targets}")


@dataclass
class Config:
    root: Path
    base: str | None = None
    checks: list[Check] | None = None
    qa: dict = field(default_factory=dict)
    review_rules: list[str] = field(default_factory=list)
    integrations: dict = field(default_factory=lambda: {"graphskill": "auto", "agenticos": "auto"})
    timeout_sec: int = 600
    host: str | None = None                       # default target for the tests
    host_env: list[str] = field(default_factory=lambda: ["BASE_URL"])
    protected_hosts: list[str] = field(default_factory=list)

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR


class ConfigError(ValueError):
    pass


def load(root: str | Path) -> Config:
    root = Path(root).resolve()
    path = root / CONFIG_NAME
    data: dict = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{CONFIG_NAME} is not valid YAML: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{CONFIG_NAME} must be a mapping")
    checks = None
    if data.get("checks") is not None:
        checks = []
        for i, c in enumerate(data["checks"]):
            if not isinstance(c, dict) or not c.get("cmd"):
                raise ConfigError(f"checks[{i}] needs at least a `cmd`")
            checks.append(Check(name=str(c.get("name") or f"check-{i + 1}"), cmd=str(c["cmd"]),
                                blocking=bool(c.get("blocking", True)), kind=str(c.get("kind", "check")),
                                targeted_cmd=c.get("targeted_cmd")))
    integrations = {"graphskill": "auto", "agenticos": "auto"}
    integrations.update({k: str(v) for k, v in (data.get("integrations") or {}).items()})
    return Config(root=root, base=data.get("base"), checks=checks, qa=data.get("qa") or {},
                  review_rules=[str(r) for r in ((data.get("review") or {}).get("rules") or [])],
                  integrations=integrations, timeout_sec=int(data.get("timeout_sec", 600)),
                  host=data.get("host") or None,
                  host_env=[str(n) for n in (data.get("host_env") or ["BASE_URL"])],
                  protected_hosts=[str(p) for p in (data.get("protected_hosts") or [])])
