"""The host the tests point at (local, docker, staging...): one URL, given per run.

Every check receives it as environment variables (`BASE_URL` and `QA_REVIEW_HOST` by default) and
as `{host}` in its command. Hosts listed in `protected_hosts` (production) only run with an explicit
`allow_protected`.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from urllib.parse import urlsplit

DEFAULT_ENV = ("BASE_URL",)
_SAFE = re.compile(r"^[A-Za-z0-9._~:/\[\]%@+-]+$")
_LOCAL = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal")


class HostError(ValueError):
    pass


def normalize(host: str) -> str:
    """'staging.app.com' → 'https://staging.app.com'; 'localhost:8000' → 'http://localhost:8000'."""
    h = (host or "").strip().rstrip("/")
    if not h:
        raise HostError("empty host")
    if "://" not in h:
        bare = h.split("/", 1)[0].split(":", 1)[0].lower()
        local = bare in _LOCAL or bare.endswith((".localhost", ".test", ".local")) or "." not in bare
        h = ("http://" if local else "https://") + h
    if not _SAFE.match(h):
        raise HostError(f"host {host!r} has characters that are not allowed in a URL")
    parts = urlsplit(h)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HostError(f"host {host!r} must be an http(s) URL, e.g. https://staging.example.com")
    if parts.query or parts.fragment:
        raise HostError(f"host {host!r} must not have a query or fragment")
    return h


def is_protected(host: str, patterns: list[str]) -> bool:
    """True when the host's name matches a pattern: exact, a parent domain, or a glob."""
    name = (urlsplit(host).hostname or "").lower()
    for p in patterns or []:
        p = str(p).strip().lower()
        if "://" in p:
            p = urlsplit(p).hostname or p
        if p and (name == p or name.endswith("." + p) or fnmatch.fnmatch(name, p)):
            return True
    return False


def env_for(host: str | None, names: list[str] | tuple[str, ...] = DEFAULT_ENV) -> dict[str, str]:
    if not host:
        return {}
    return {**{n: host for n in names}, "QA_REVIEW_HOST": host}


def needs_host(cmd: str | None) -> bool:
    return bool(cmd) and "{host}" in cmd


def apply(cmd: str, host: str | None) -> str:
    return cmd.replace("{host}", shlex.quote(host)) if host else cmd
