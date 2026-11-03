"""A small Gherkin reader for acceptance criteria: one Scenario per UAC, tagged @UAC-<n>.

Supports English and Spanish keywords, tags, Background, Scenario Outline with Examples, doc
strings and comments; the scenario's own text is kept verbatim so nothing the author wrote is lost.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

_FEATURE = re.compile(r"^\s*(Feature|Característica|Funcionalidad)\s*:\s*(.*)$", re.IGNORECASE)
_BACKGROUND = re.compile(r"^\s*(Background|Antecedentes|Contexto)\s*:", re.IGNORECASE)
_SCENARIO = re.compile(r"^\s*(Scenario Outline|Scenario Template|Scenario|Example|Esquema del escenario|"
                       r"Escenario|Ejemplo)\s*:\s*(.*)$", re.IGNORECASE)
_EXAMPLES = re.compile(r"^\s*(Examples|Scenarios|Ejemplos)\s*:", re.IGNORECASE)
_STEP = re.compile(r"^\s*(Given|When|Then|And|But|Dado|Dada|Dados|Dadas|Cuando|Entonces|Y|E|Pero|\*)\s+(.*)$", re.IGNORECASE)
_TAGS = re.compile(r"^\s*@")
UAC_TAG = re.compile(r"^@UAC-(\d+)$", re.IGNORECASE)
KINDS = ("e2e", "api")

_KIND_OF = {"given": "given", "dado": "given", "dada": "given", "dados": "given", "dadas": "given",
            "when": "when", "cuando": "when", "then": "then", "entonces": "then"}


class GherkinError(ValueError):
    pass


@dataclass
class Step:
    keyword: str        # as written: Given, And, Dado...
    kind: str           # given | when | then (And/But inherit the previous one)
    text: str


@dataclass
class Scenario:
    name: str
    tags: list[str]
    steps: list[Step]
    line: int
    text: str                           # verbatim, tags included
    outline: bool = False

    @property
    def uac_id(self) -> str | None:
        ids = [t for t in self.tags if UAC_TAG.match(t)]
        return ids[0][1:].upper() if len(ids) == 1 else None

    @property
    def kinds(self) -> list[str]:
        return [t[1:].lower() for t in self.tags if t[1:].lower() in KINDS]

    @property
    def breaks(self) -> list[str]:
        """Attack categories of an adversarial UAC (@break:<category>); empty for a functional one."""
        return [t.split(":", 1)[1].lower() for t in self.tags if t.lower().startswith("@break:")]

    def tag_value(self, prefix: str) -> str | None:
        for t in self.tags:
            if t.lower().startswith(f"@{prefix}:"):
                return t.split(":", 1)[1]
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(id=self.uac_id, kinds=self.kinds, source=self.tag_value("source"), ref=self.tag_value("ref"),
                 breaks=self.breaks)
        return d


@dataclass
class Feature:
    name: str
    description: str
    tags: list[str]
    background: list[Step] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)

    def by_id(self) -> dict[str, Scenario]:
        return {s.uac_id: s for s in self.scenarios if s.uac_id}

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "tags": self.tags,
                "background": [asdict(s) for s in self.background],
                "uacs": [s.to_dict() for s in self.scenarios]}


def parse(text: str) -> Feature:
    feature: Feature | None = None
    pending_tags: list[str] = []
    pending_lines: list[str] = []
    current: Scenario | None = None
    in_background = in_doc = False
    last_kind = "given"
    desc: list[str] = []

    def close():
        nonlocal current
        if current is not None:
            current.text = current.text.rstrip("\n")
            feature.scenarios.append(current)
            current = None

    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith(('"""', "```")):
            in_doc = not in_doc
            if current is not None:
                current.text += raw + "\n"
            continue
        if in_doc:
            if current is not None:
                current.text += raw + "\n"
            continue
        if not stripped or stripped.startswith("#"):
            if current is not None:
                current.text += raw + "\n"
            continue
        if _TAGS.match(stripped):
            tags = stripped.split()
            bad = [t for t in tags if not t.startswith("@")]
            if bad:
                raise GherkinError(f"line {n}: tags must all start with @: {stripped!r}")
            pending_tags += tags
            pending_lines.append(raw)
            continue
        if m := _FEATURE.match(line):
            if feature is not None:
                raise GherkinError(f"line {n}: only one Feature per file")
            feature = Feature(name=m.group(2).strip(), description="", tags=pending_tags)
            pending_tags, pending_lines = [], []
            continue
        if feature is None:
            raise GherkinError(f"line {n}: the file must start with 'Feature:'")
        if _BACKGROUND.match(line):
            close()
            in_background = True
            continue
        if m := _SCENARIO.match(line):
            close()
            in_background = False
            current = Scenario(name=m.group(2).strip(), tags=pending_tags, steps=[], line=n,
                               text="".join(l + "\n" for l in pending_lines) + raw + "\n",
                               outline="outline" in m.group(1).lower() or "esquema" in m.group(1).lower()
                               or "template" in m.group(1).lower())
            pending_tags, pending_lines = [], []
            last_kind = "given"
            continue
        if _EXAMPLES.match(line) or stripped.startswith("|"):
            if current is None:
                raise GherkinError(f"line {n}: examples outside a scenario")
            current.text += raw + "\n"
            continue
        if m := _STEP.match(line):
            kw = m.group(1)
            kind = _KIND_OF.get(kw.lower(), last_kind)
            last_kind = kind
            step = Step(keyword=kw, kind=kind, text=m.group(2).strip())
            if current is not None:
                current.steps.append(step)
                current.text += raw + "\n"
            elif in_background:
                feature.background.append(step)
            else:
                raise GherkinError(f"line {n}: step outside a scenario")
            continue
        if current is None and not in_background:
            desc.append(stripped)
            continue
        raise GherkinError(f"line {n}: not a Gherkin line: {stripped!r}")
    if feature is None:
        raise GherkinError("empty: expected 'Feature:'")
    close()
    feature.description = "\n".join(desc)
    return feature


def validate(feature: Feature) -> list[str]:
    """Problems that would make a UAC untestable or untraceable. Empty list = valid."""
    problems = []
    if not feature.scenarios:
        problems.append("no scenarios: write one Scenario per acceptance criterion")
    seen: dict[str, int] = {}
    for s in feature.scenarios:
        where = f"scenario '{s.name}' (line {s.line})"
        ids = [t for t in s.tags if UAC_TAG.match(t)]
        if len(ids) != 1:
            problems.append(f"{where}: needs exactly one @UAC-<n> tag, has {len(ids)}")
        elif (uid := s.uac_id) in seen:
            problems.append(f"{where}: {uid} is already used on line {seen[uid]}")
        else:
            seen[uid] = s.line
        if not s.kinds:
            problems.append(f"{where}: tag it @e2e (UI) or @api (HTTP), or both")
        kinds = {st.kind for st in s.steps}
        if "when" not in kinds or "then" not in kinds:
            problems.append(f"{where}: needs at least one When and one Then")
        if not s.name:
            problems.append(f"{where}: give the scenario a name")
        from .attacks import CATEGORIES
        unknown = [b for b in s.breaks if b not in CATEGORIES]
        if unknown:
            problems.append(f"{where}: unknown @break category {', '.join(unknown)} "
                            f"(use {', '.join(CATEGORIES)})")
    return problems


def next_id(feature: Feature | None) -> int:
    nums = [int(UAC_TAG.match(t).group(1)) for s in (feature.scenarios if feature else [])
            for t in s.tags if UAC_TAG.match(t)]
    return max(nums, default=0) + 1
