"""How to break things: the categories of adversarial UACs (`@break:<category>`), the safe behaviour each
one must show, ready-made payloads, and a deterministic attack plan for a given attack surface."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    id: str
    title: str
    applies_to: tuple[str, ...]     # "api" (HTTP endpoints), "e2e" (UI)
    ideas: str                      # what to try
    safe: str                       # what the system must do instead of breaking


CATEGORIES: dict[str, Category] = {c.id: c for c in (
    Category("boundary", "Boundaries and limits", ("api", "e2e"),
             "0, -1, min-1, max+1, very large numbers, decimals with many digits, empty and 10k-character strings, "
             "empty and huge lists",
             "rejected with a 4xx and a clear message, or handled exactly as the rule says; never a 5xx, never a "
             "wrong result (negative totals, overflow, rounding drift)"),
    Category("types", "Wrong types and shapes", ("api",),
             "string where a number goes, null, arrays/objects instead of scalars, missing required fields, unknown "
             "extra fields, malformed JSON, wrong Content-Type",
             "4xx naming the field; unknown fields ignored or rejected, never applied (no mass assignment)"),
    Category("injection", "Injection", ("api", "e2e"),
             "SQL (' OR 1=1 --), XSS (<script>, <img onerror>), template ({{7*7}}, ${7*7}), command (; id), path "
             "traversal (../../etc/passwd), NoSQL ({\"$ne\": null}), CRLF in headers",
             "treated as plain data: stored/returned escaped, no extra rows, no evaluation, no file access, no 5xx"),
    Category("unicode", "Unicode and encoding", ("api", "e2e"),
             "emoji, RTL text, zero-width and combining characters, NUL byte, very long words, homoglyphs, "
             "mixed normalisation (é vs e + ´)",
             "accepted and shown intact, or rejected with a 4xx; never corrupted, truncated silently or a 5xx"),
    Category("auth", "Authentication and authorisation", ("api", "e2e"),
             "no token, expired/invalid token, another user's resource id (IDOR), role or owner fields in the body, "
             "admin-only actions as a normal user",
             "401/403 (or 404 for others' resources) and no data from other users; privilege fields ignored"),
    Category("http", "HTTP misuse", ("api",),
             "huge body (MBs), repeated query params, unexpected methods on the route, missing/garbage headers, "
             "very long URLs",
             "413/405/4xx as appropriate, fast; never a 5xx or a hang"),
    Category("concurrency", "Concurrency and repetition", ("api", "e2e"),
             "the same request N times in parallel, double-click on submit, retry after timeout, two users editing "
             "the same thing",
             "applied exactly once (idempotent) or the duplicates rejected; final state consistent"),
    Category("state", "State and order", ("api", "e2e"),
             "steps out of order, acting on deleted/expired things, going back and resubmitting, stale data",
             "a clear 4xx/409 and the state left unchanged"),
    Category("resilience", "Resilience of the UI", ("e2e",),
             "slow or failed network (page.route abort), server errors on a call, offline, very narrow screen, "
             "keyboard only",
             "a visible, understandable error and a way to recover; no blank page, no lost input, no broken layout"),
)}


# Payloads every attack helper and the automatic battery share.
PAYLOADS: dict[str, list] = {
    "injection": ["' OR '1'='1' --", "\"; DROP TABLE users; --", "<script>alert('qa')</script>",
                  "<img src=x onerror=alert('qa')>", "{{7*7}}", "${7*7}", "; id", "$(id)",
                  "../../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "%00", "\r\nX-Injected: qa"],
    "unicode": ["\U0001f600\U0001f44d\U0001f3fd\U0001f1ea\U0001f1f8", "\u202eevil\u202c", "zero\u200bwidth", "e\u0301",
                "\u0000", "\uff21\uff44\uff4d\uff49\uff4e", "\u00df" * 200,
                "\U0001d4fe\U0001d4f7\U0001d4f2\U0001d4ec\U0001d4f8\U0001d4ed\U0001d4ee"],
    "numbers": [0, -1, -0.0001, 1e-12, 2**31, 2**63, 1e308, -1e308, 0.1 + 0.2],
    "wrong_types": ["not-a-number", None, [], {}, True, "", " "],
    "strings": ["", " ", "a" * 10_000],
}

# Signs that an error page leaks internals (for both the battery and expectSafeError).
LEAK_PATTERNS = [
    r"Traceback \(most recent call last\)", r"\bat [\w.$<>]+\([\w./-]+:\d+(:\d+)?\)", r"File \"[^\"]+\", line \d+",
    r"(?i)sql(state|syntax|ite3?\.)|syntax error at or near|mysql_fetch|ORA-\d{5}|PG::|psycopg2",
    r"(?i)werkzeug debugger|whoops\\|symfony\\component|laravel|django\.(core|db)|Fatal error:|Stack trace:",
    r"/(home|Users|var/www|srv|app)/[\w.-]+/[\w./-]+\.(py|php|js|ts|rb|java|go)",
    r"(?i)\bexception\b.*\bat\b", r"(?i)debug\s*=\s*true",
]


def plan(surface: list[dict], has_ui: bool) -> list[dict]:
    """A deterministic list of attacks worth writing UACs for, by endpoint (and the UI when it changed)."""
    out = []
    for ep in surface:
        method = ep["method"].upper()
        writes = method in ("POST", "PUT", "PATCH", "DELETE", "ANY")
        fields = [f["name"] for f in ep.get("fields", [])]
        path_params = [p for p in ep.get("path_params", [])]
        cats = ["types", "injection", "boundary"] if (fields or writes) else []
        if path_params:
            cats += ["auth", "boundary"]
        if writes:
            cats += ["concurrency", "state", "http", "auth"]
        cats += ["unicode"] if fields else []
        seen = []
        for c in cats:
            if c not in seen:
                seen.append(c)
        for c in seen:
            cat = CATEGORIES[c]
            target = (f"fields {', '.join(fields[:8])}" if fields and c in ("types", "injection", "boundary", "unicode")
                      else f"path params {', '.join(path_params)}" if path_params and c in ("auth", "boundary")
                      else "the endpoint")
            out.append({"target": f"{method} {ep['path']}", "category": c, "on": target,
                        "try": cat.ideas, "expect": cat.safe})
    if has_ui:
        for c in ("resilience", "concurrency", "injection", "boundary"):
            cat = CATEGORIES[c]
            out.append({"target": "UI", "category": c, "on": "the changed pages and forms",
                        "try": cat.ideas, "expect": cat.safe})
    return out
