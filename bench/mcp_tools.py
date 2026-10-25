"""Krusgraph's MCP tools for setup B: an MCP client over streamable HTTP against https://krusgraph.com/mcp with the
user's bearer token. The session lives on a background thread with its own event loop, so the synchronous agent loop
can call tools one at a time. Counts calls and failures per tool; a GraphQL call is a call to `query_graphql`."""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Callable


class McpTools:
    def __init__(self, url: str, token: str, timeout: float = 120.0) -> None:
        self.url, self.token, self.timeout = url, token, timeout
        self.tools: list[dict] = []
        self.calls: dict[str, int] = {}
        self.failed: dict[str, int] = {}
        self.log: list[dict] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._stop: asyncio.Event | None = None
        self._session = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

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
            from mcp import ClientSession
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
            import httpx
            http = create_mcp_http_client(headers={"Authorization": f"Bearer {self.token}"}, timeout=httpx.Timeout(self.timeout))
            async with streamable_http_client(self.url, http_client=http) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in listed.tools]
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
        fut = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, args), self._loop)
        try:
            result = fut.result(self.timeout)
        except Exception as e:  # noqa: BLE001
            self.failed[name] = self.failed.get(name, 0) + 1
            self.log.append({"question_id": question_id, "repeat": repeat, "tool": name, "args": args, "error": f"{type(e).__name__}: {e}"})
            return f"{type(e).__name__}: {e}", True
        text = "\n".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text")
        is_error = bool(getattr(result, "isError", False)) or _tool_error(text)
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
