"""A minimal JSON-RPC client for `codex app-server --listen stdio://`.

Deliberately NOT the `openai-codex` SDK: its default approval handler
auto-accepts every server request, and the harness must decide every one
itself (deny by default). This speaks the wire protocol directly: one JSON
object per line, `id`-matched responses, server-initiated requests (which
carry both `id` and `method`) answered by `on_request`, and notifications
queued for the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("swarm")

RequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
LINE_LIMIT = 64 * 1024 * 1024  # a turn/completed row can carry every item of a long turn


class RpcError(Exception):
    """The server answered a request with a JSON-RPC error."""

    def __init__(self, method: str, error: dict[str, Any]):
        self.method, self.error = method, error
        super().__init__(f"{method}: {error.get('code')}: {error.get('message')}")


class Refused(Exception):
    """A server request the client deliberately does not serve: answered with
    a JSON-RPC error, without a traceback in the log."""


class ServerGone(Exception):
    """The app-server process exited or its stdout closed."""


class AppServer:
    def __init__(self, argv: list[str], env: dict[str, str], cwd: str,
                 on_request: RequestHandler, on_stderr: Callable[[str], None] | None = None,
                 on_raw: Callable[[str, dict[str, Any]], None] | None = None):
        self.argv, self.env, self.cwd = argv, env, cwd
        self.on_request, self.on_stderr, self.on_raw = on_request, on_stderr, on_raw
        self.proc: asyncio.subprocess.Process | None = None
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending: dict[int, tuple[str, asyncio.Future[Any]]] = {}
        self._next_id = 1
        self._tasks: list[asyncio.Task[Any]] = []
        self._write_lock = asyncio.Lock()
        self.exited: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.env, cwd=self.cwd, limit=LINE_LIMIT,
            start_new_session=True)  # its own process group: close() takes every child with it
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    async def _send(self, obj: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.returncode is not None:
            raise ServerGone("app-server is not running")
        data = (json.dumps(obj) + "\n").encode()
        async with self._write_lock:
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                raise ServerGone(f"app-server stdin closed: {e}") from e

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 120.0) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = (method, fut)
        await self._send({"id": rid, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def _answer(self, rid: Any, method: str, params: dict[str, Any]) -> None:
        try:
            result = await self.on_request(method, params)
            reply: dict[str, Any] = {"id": rid, "result": result}
        except Refused as e:
            reply = {"id": rid, "error": {"code": -32601, "message": str(e)}}
        except Exception as e:  # a handler bug must answer, never hang the turn
            log.exception("server request %s failed", method)
            reply = {"id": rid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        try:
            await self._send(reply)
        except ServerGone:
            pass

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except ValueError:  # a line over LINE_LIMIT: skip it rather than die
                    log.warning("app-server: dropped an oversized line")
                    continue
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    if self.on_stderr:
                        self.on_stderr(f"[non-JSON stdout] {line[:500].decode(errors='replace')}")
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:  # a server request: always answered
                    if self.on_raw:
                        self.on_raw("server_request", msg)
                    self._tasks.append(asyncio.create_task(
                        self._answer(msg["id"], msg["method"], msg.get("params") or {})))
                elif "method" in msg:
                    await self.notifications.put(msg)
                elif "id" in msg:
                    entry = self._pending.get(msg["id"])
                    if entry is None:
                        continue
                    method, fut = entry
                    if fut.done():
                        continue
                    if "error" in msg:
                        fut.set_exception(RpcError(method, msg["error"] or {}))
                    else:
                        fut.set_result(msg.get("result"))
        finally:
            self.exited.set()
            for method, fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ServerGone(f"app-server exited during {method}"))
            await self.notifications.put({"method": "_server_gone", "params": {}})

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            try:
                line = await self.proc.stderr.readline()
            except ValueError:
                continue
            if not line:
                break
            if self.on_stderr:
                self.on_stderr(line.decode(errors="replace").rstrip("\n"))

    async def next_notification(self, timeout: float) -> dict[str, Any]:
        return await asyncio.wait_for(self.notifications.get(), timeout)

    async def close(self) -> None:
        """Stop the server and everything it started (its process group)."""
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            for sig in ("TERM", "KILL"):
                try:
                    os.killpg(proc.pid, 15 if sig == "TERM" else 9)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                    break
                except asyncio.TimeoutError:
                    continue
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
