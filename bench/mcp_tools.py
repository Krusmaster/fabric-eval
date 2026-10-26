"""Krusgraph's MCP tools for setup B: an MCP client over streamable HTTP against https://krusgraph.com/mcp with the
user's bearer token. The session lives on a background thread with its own event loop, so the synchronous agent loop
can call tools one at a time. Counts calls and failures per tool; a GraphQL call is a call to `query_graphql`.

Only the tools the server marks read-only (`readOnlyHint: true` in tools/list) are given to the model: a measurement
run never creates an API, a view, a semantic model or a data agent. The creating tools are listed in `excluded`, and a
call to one of them is refused here before it reaches the server. The token goes only to an https URL (or to a
loopback address, for tests). Written for the MCP Python SDK 2.x (`input_schema`, `is_error`, httpx2); the bench extra
pins it."""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Callable
from urllib.parse import urlparse

DEFAULT_URL = "https://krusgraph.com/mcp"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def check_url(url: str) -> str:
    """The URL the token may be sent to: https, or plain http to a loopback address only."""
    u = urlparse(url)
    if u.scheme == "https" and u.hostname:
        return url
    if u.scheme == "http" and u.hostname in _LOOPBACK:
        return url
    raise ValueError(f"refusing to send the Krusgraph token to {url!r}: use an https URL (default {DEFAULT_URL})")


def _get(obj, *names, default=None):
    """A field of an MCP type or of a plain dict, under either spelling (SDK 2.x snake_case or the wire's camelCase)."""
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if not isinstance(obj, dict) and getattr(obj, n, None) is not None:
            return getattr(obj, n)
    return default


def read_only(tool) -> bool:
    ann = _get(tool, "annotations")
    return bool(ann is not None and _get(ann, "read_only_hint", "readOnlyHint", default=False))


def split_tools(listed: list, reserved=()) -> tuple[list[dict], list[str], list[str]]:
    """(tool definitions for the Anthropic API, names left out because the server does not mark them read-only, names
    left out because the setup already has a tool of that name). Krusgraph has its own `list_tables`; setup B keeps
    setup A's, so the only difference between the setups is the tools A does not have, and tool names stay unique (the
    Messages API refuses a request with two tools of the same name)."""
    kept, excluded, shadowed = [], [], []
    for t in listed:
        name = _get(t, "name")
        if name in reserved:
            shadowed.append(name)
        elif read_only(t):
            kept.append({"name": name, "description": _get(t, "description", default="") or "",
                         "input_schema": _get(t, "input_schema", "inputSchema", default={"type": "object", "properties": {}})})
        else:
            excluded.append(name)
    return kept, excluded, shadowed


class McpTools:
    def __init__(self, url: str, token: str, timeout: float = 120.0, reserved=()) -> None:
        self.url, self.token, self.timeout = check_url(url), token, timeout
        self.reserved = tuple(reserved)
        self.tools: list[dict] = []
        self.excluded: list[str] = []
        self.shadowed: list[str] = []
        self.calls: dict[str, int] = {}
        self.failed: dict[str, int] = {}
        self.log: list[dict] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._stop: asyncio.Event | None = None
        self._session = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "McpTools":
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        if not self._ready.wait(self.timeout):
            raise TimeoutError(f"no answer from {self.url} within {self.timeout}s")
        if self._error:
            raise self._error
        return self

    async def _serve(self) -> None:
        try:
            import httpx2
            from mcp import ClientSession
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
            http = create_mcp_http_client(headers=self.headers(), timeout=httpx2.Timeout(self.timeout))
            async with streamable_http_client(self.url, http_client=http) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.tools, self.excluded, self.shadowed = split_tools(list(listed.tools), self.reserved)
                    if not self.tools:
                        raise RuntimeError(f"{self.url} marks none of its {len(self.excluded)} tools read-only; "
                                           "refusing to give the agent tools that write")
                    self._session = session
                    self._stop = asyncio.Event()
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as e:  # noqa: BLE001 — surfaced to start()/call()
            self._error = e
            self._ready.set()

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)

    # ---------------------------------------------------------------- calls
    def handles(self, name: str) -> bool:
        return any(t["name"] == name for t in self.tools)

    def call(self, name: str, args: dict, question_id: str, repeat: int) -> tuple[str, bool]:
        self.calls[name] = self.calls.get(name, 0) + 1
        if not self.handles(name):                       # an excluded (writing) tool, even if the model guesses its name
            self.failed[name] = self.failed.get(name, 0) + 1
            return f"refused: {name} is not available in this run (read-only tools only)", True
        fut = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, args), self._loop)
        try:
            result = fut.result(self.timeout)
        except Exception as e:  # noqa: BLE001
            self.failed[name] = self.failed.get(name, 0) + 1
            self.log.append({"question_id": question_id, "repeat": repeat, "tool": name, "args": args, "error": f"{type(e).__name__}: {e}"})
            return f"{type(e).__name__}: {e}", True
        text = "\n".join(_get(c, "text", default="") for c in (_get(result, "content", default=[]) or []) if _get(c, "type") == "text")
        is_error = bool(_get(result, "is_error", "isError", default=False)) or _tool_error(text)
        if is_error:
            self.failed[name] = self.failed.get(name, 0) + 1
        self.log.append({"question_id": question_id, "repeat": repeat, "tool": name, "args": args, "error": is_error, "chars": len(text)})
        return text, is_error


def _tool_error(text: str) -> bool:
    """Krusgraph answers failures as JSON with error/kind/hint and HTTP 200; count those as failed calls too."""
    try:
        d = json.loads(text)
    except ValueError:
        return False
    return isinstance(d, dict) and "error" in d and "kind" in d


class FakeMcpTools:
    """For tests: a dict of name -> callable(args) -> text (raise or return a {"error","kind"} JSON to fail)."""

    def __init__(self, functions: dict[str, Callable], descriptions: dict[str, str] | None = None) -> None:
        self.functions = functions
        self.tools = [{"name": n, "description": (descriptions or {}).get(n, n), "input_schema": {"type": "object", "properties": {}}} for n in functions]
        self.excluded: list[str] = []
        self.shadowed: list[str] = []
        self.calls: dict[str, int] = {}
        self.failed: dict[str, int] = {}
        self.log: list[dict] = []

    def handles(self, name: str) -> bool:
        return name in self.functions

    def call(self, name: str, args: dict, question_id: str, repeat: int) -> tuple[str, bool]:
        self.calls[name] = self.calls.get(name, 0) + 1
        try:
            text = self.functions[name](args)
        except Exception as e:  # noqa: BLE001
            self.failed[name] = self.failed.get(name, 0) + 1
            return f"{type(e).__name__}: {e}", True
        is_error = _tool_error(text)
        if is_error:
            self.failed[name] = self.failed.get(name, 0) + 1
        return text, is_error

    def close(self) -> None:
        pass
