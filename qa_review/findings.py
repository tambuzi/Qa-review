from __future__ import annotations

from dataclasses import asdict, dataclass

SEVERITIES = ("info", "low", "medium", "high", "critical")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}


def rank(severity: str) -> int:
    return _RANK.get(severity, 0)


@dataclass
class Finding:
    source: str            # security | check | qa | review | impact
    severity: str          # info | low | medium | high | critical
    title: str
    file: str | None = None
    line: int | None = None
    detail: str = ""
    rule: str | None = None

    def __post_init__(self):
        if self.severity not in _RANK:
            self.severity = "medium"

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}

    @classmethod
    def from_dict(cls, d: dict, source: str = "review") -> Finding:
        line = d.get("line")
        try:
            line = int(line) if line not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        return cls(source=str(d.get("source") or source), severity=str(d.get("severity", "medium")).lower(),
                   title=str(d.get("title") or d.get("message") or "(no title)")[:300],
                   file=d.get("file"), line=line, detail=str(d.get("detail") or "")[:4000],
                   rule=d.get("rule"))
