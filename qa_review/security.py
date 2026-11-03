"""Fixed, line-by-line security rules over the lines a change adds. No AI, so it is fast,
repeatable and free. It flags; the review decides. (Same spirit as agenticOS's review_focus,
re-implemented here so qa-review has no dependency on it.)"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .findings import Finding
from .gitdiff import Changes

_SENSITIVE_WORDS = {"auth", "authn", "authz", "oauth", "oauth2", "login", "logout", "session", "sessions",
                    "password", "passwords", "secret", "secrets", "crypto", "permission", "permissions",
                    "acl", "rbac", "jwt", "csrf", "credential", "credentials", "token", "tokens", "payment",
                    "payments", "billing"}
_LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock",
              "cargo.lock", "go.sum", "composer.lock", "gemfile.lock", "uv.lock"}
_DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
_TEST_HINT = re.compile(r"(^|/)(tests?|__tests__|spec|fixtures?)(/|$)|(_test|\.test|\.spec|Test)\.\w+$")

_SQL = re.compile(r"(?i)\b(select\s+\S.*?\s+from|insert\s+into|update\s+\w+\s+set|delete\s+from)\b")
_INTERP = re.compile(r"""f"[^"\n]*\{|f'[^'\n]*\{|\$\{|["']\s*(\+|%|\.)\s*\$?\w|\w\s*\+\s*["']|\.format\(|\{\$""")

# (rule id, severity, title, pattern)
_RULES = [
    ("secret-literal", "critical", "possible hard-coded secret",
     re.compile(r"""(?i)\b(api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret|password|passwd|private[_-]?key)\b"""
                r"""\s*[:=]\s*["'][^"'\s]{8,}["']""")),
    ("secret-token", "critical", "token-shaped secret in code",
     re.compile(r"sk-[A-Za-z0-9_-]{20,}|sk_live_[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}"
                r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("shell", "high", "runs a shell command",
     re.compile(r"""shell\s*=\s*True|os\.(system|popen)\s*\(|child_process\.(exec|execSync)\s*\(|"""
                r"""Runtime\.getRuntime\(\)\.exec|\b(shell_exec|passthru|proc_open)\s*\(|`[^`]*\$\{""")),
    ("eval", "high", "evaluates code at runtime",
     re.compile(r"(?<![\w.])(eval|exec)\s*\(|new\s+Function\s*\(")),
    ("deserialize", "high", "deserialises possibly untrusted data",
     re.compile(r"pickle\.loads?\s*\(|yaml\.load\s*\((?![^)\n]*SafeLoader)|\bunserialize\s*\(|marshal\.loads\s*\(")),
    ("weak-hash", "medium", "weak hash (MD5/SHA-1)",
     re.compile(r"hashlib\.(md5|sha1)\s*\(|(?<![\w.])(md5|sha1)\s*\(|createHash\(\s*['\"](md5|sha1)['\"]")),
    ("path-from-input", "high", "builds a file path from request input",
     re.compile(r"\b(open|os\.path\.join|Path|send_file|send_from_directory|file_get_contents|fopen|readFile(Sync)?)"
                r"\s*\([^)\n]*\b(request|req\.|params|args|input|query|filename|\$_GET|\$_POST)\b")),
    ("tls-off", "high", "disables TLS verification",
     re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|CURLOPT_SSL_VERIFYPEER\s*,\s*(false|0)"
                r"|InsecureSkipVerify\s*:\s*true")),
    ("debug-on", "medium", "debug mode enabled",
     re.compile(r"(?i)\bdebug\s*=\s*True\b|app\.run\([^)\n]*debug\s*=\s*True|APP_DEBUG\s*=\s*true")),
    ("cors-any", "medium", "CORS open to any origin",
     re.compile(r"""Access-Control-Allow-Origin['"]?\s*[:,]\s*['"]\*|allow_origins\s*=\s*\[\s*['"]\*""")),
    ("xss-sink", "medium", "writes raw HTML",
     re.compile(r"\.innerHTML\s*=|dangerouslySetInnerHTML|\|\s*safe\b|mark_safe\s*\(|\{!!")),
]


SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".php", ".go", ".rb",
              ".java", ".kt", ".kts", ".scala", ".rs", ".cs", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp"}


def is_source(path: str) -> bool:
    """Program source (as opposed to config, data or docs): what tests are expected to cover."""
    return PurePosixPath(path.lower()).suffix in SOURCE_EXT


def is_code(path: str) -> bool:
    p = PurePosixPath(path.lower())
    return (p.name not in _LOCKFILES and p.suffix not in _DOC_SUFFIXES
            and not any(part in ("docs", "doc") for part in p.parts[:-1]))


def is_test(path: str) -> bool:
    return bool(_TEST_HINT.search(path))


def sensitive_path(path: str) -> bool:
    p = PurePosixPath(path.lower())
    if p.name.startswith(".env"):
        return True
    return any(w in _SENSITIVE_WORDS for w in re.split(r"[/_.\-]+", str(p)) if w)


def scan(changes: Changes) -> tuple[list[Finding], list[str]]:
    """(findings, sensitive paths). Test files only get the secret rules."""
    findings: list[Finding] = []
    sensitive: list[str] = []
    for f in changes.files:
        if f.status == "deleted":
            continue
        if sensitive_path(f.path):
            sensitive.append(f.path)
        if PurePosixPath(f.path).name.startswith(".env") and f.added:
            findings.append(Finding("security", "high", "environment file in the change (secrets?)",
                                    file=f.path, rule="env-file"))
        if not is_code(f.path):
            continue
        test = is_test(f.path)
        for n, text in f.added:
            if len(text) > 2000:
                continue
            for rule, sev, title, rx in _RULES:
                if test and not rule.startswith("secret"):
                    continue
                if rx.search(text):
                    detail = "(value hidden)" if rule.startswith("secret") else text.strip()[:200]
                    findings.append(Finding("security", sev, title, file=f.path, line=n,
                                            detail=detail, rule=rule))
            if not test and _SQL.search(text) and _INTERP.search(text):
                findings.append(Finding("security", "high", "SQL built from string interpolation",
                                        file=f.path, line=n, detail=text.strip()[:200], rule="sql-interp"))
    return findings, sensitive
