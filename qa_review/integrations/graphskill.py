"""graphskill integration: ask a project's code graph which symbols a change touches and who
calls them. Talks to graphskill's own MCP server over stdio (one short-lived session per run),
so it needs no import of graphskill and works with any install (venv, Docker, PATH).

Discovery, first match wins:
1. `integrations.graphskill` in .qa-review.yaml set to a command (e.g. "/path/python -m graphskill");
2. the `graphskill` entry of the project's .mcp.json (what `graphskill setup` writes);
3. the QA_REVIEW_GRAPHSKILL environment variable (a command);
4. a `graphskill` executable on PATH.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class GraphUnavailable(RuntimeError):
    pass


def server_for(root: Path, setting: str = "auto") -> tuple[dict | None, str]:
    """(server spec {command, args, env}, how it was found or why not)."""
    if setting == "off":
        return None, "disabled in .qa-review.yaml"
    if setting not in ("auto", "on", ""):
        parts = shlex.split(setting)
        return {"command": parts[0], "args": [*parts[1:], "serve", str(root)], "env": {}}, "from .qa-review.yaml"
    try:
        data = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        entry = (data.get("mcpServers") or {}).get("graphskill")
        if isinstance(entry, dict) and entry.get("command"):
            return {"command": entry["command"], "args": [str(a) for a in entry.get("args") or []],
                    "env": entry.get("env") or {}}, "from the project's .mcp.json"
    except (OSError, json.JSONDecodeError):
        pass
    if env := os.environ.get("QA_REVIEW_GRAPHSKILL"):
        parts = shlex.split(env)
        return {"command": parts[0], "args": [*parts[1:], "serve", str(root)], "env": {}}, "from QA_REVIEW_GRAPHSKILL"
    if exe := shutil.which("graphskill"):
        return {"command": exe, "args": ["serve", str(root)], "env": {}}, "graphskill on PATH"
    return None, "not found (no .mcp.json entry, QA_REVIEW_GRAPHSKILL or graphskill on PATH)"


def _decode(result) -> object:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    items = []
    for c in getattr(result, "content", None) or []:
        text = getattr(c, "text", None)
        if text is None:
            continue
        try:
            items.append(json.loads(text))
        except json.JSONDecodeError:
            items.append(text)
    if len(items) == 1:
        return items[0]
    return items


class GraphClient:
    """Run a batch of graphskill tool calls in one MCP session."""

    def __init__(self, root: Path, server: dict, timeout: float = 90):
        self.root, self.server, self.timeout = root, server, timeout

    async def _session(self, work):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.server["command"], args=self.server.get("args") or [],
            env={**os.environ, **{k: str(v) for k, v in (self.server.get("env") or {}).items()}},
            cwd=str(self.root))
        quiet = open(os.devnull, "w")  # noqa: ASYNC230,SIM115 - keep graphskill's stderr off ours
        try:
            async with stdio_client(params, errlog=quiet) as (read, write), ClientSession(read, write) as session:
                await session.initialize()

                async def call(tool: str, args: dict):
                    r = await session.call_tool(tool, args)
                    if getattr(r, "isError", False):
                        return None
                    return _decode(r)

                return await work(call)
        finally:
            quiet.close()

    def run(self, work):
        """`work` is `async def work(call)`; runs on its own loop in a thread, so it is safe to
        call from inside our own MCP server's event loop."""
        def target():
            return asyncio.run(asyncio.wait_for(self._session(work), self.timeout))

        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                return pool.submit(target).result(timeout=self.timeout + 5)
            except Exception as e:  # spawn failure, timeout, protocol error
                raise GraphUnavailable(f"graphskill did not answer: {type(e).__name__}: {e}") from e


def _as_list(value) -> list:
    """List-returning tools may come back as one object when they have a single row."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def changed_symbols_and_callers(root: Path, server: dict, changed: dict[str, set[int]],
                                depth: int = 2, max_symbols: int = 40) -> dict:
    """{"symbols": [...], "callers": {name: [locations]}} for the changed line ranges."""

    async def work(call):
        symbols = []
        for path, lines in changed.items():
            rows = _as_list(await call("symbols_in_file", {"path": path}))
            if not rows and not Path(path).is_absolute():
                rows = _as_list(await call("symbols_in_file", {"path": str(root / path)}))
            for r in rows:
                if not isinstance(r, dict):
                    continue
                loc = str(r.get("location", ""))
                try:
                    start, end = (int(x) for x in loc.rsplit(":", 1)[1].split("-"))
                except (ValueError, IndexError):
                    continue
                if any(start <= n <= end for n in lines):
                    symbols.append({"name": r.get("name"), "kind": r.get("kind"), "file": path,
                                    "line_start": start, "line_end": end, "signature": r.get("signature")})
        # innermost symbols first (methods before their class), capped
        symbols.sort(key=lambda s: s["line_end"] - s["line_start"])
        symbols = symbols[:max_symbols]
        callers = {}
        for s in symbols:
            if s["kind"] in ("class", "interface", "module"):
                continue
            rows = _as_list(await call("callers", {"name": s["name"], "depth": depth}))
            callers[s["name"]] = [r.get("location") for r in rows if isinstance(r, dict) and r.get("location")]
        return {"symbols": symbols, "callers": callers}

    return GraphClient(root, server).run(work)
