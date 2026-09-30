"""Harness-hosted tools for the Codex backend.

The Codex agent gets the SAME tool surface as a Claude agent, under the
same names and argument keys (Bash, Read, Write, Edit, Glob, Grep,
TaskOutput, TaskStop, mcp__korax__*, mcp__lab__*), so the canon and the
guard apply unchanged, and the panel and every other transcript reader read
both backends' transcripts alike.
Every call is judged first by guard.py's decision logic (the same function
the Claude backend runs as its PreToolUse hook); only then is it executed:

- Bash runs in the harness's bwrap sandbox (codex_sandbox.py).
- Read/Glob run in-process (the guard has already refused credential paths);
  Grep runs `rg` inside the sandbox, so masked paths are invisible to it.
- Write/Edit write only inside the workspace (guard), never through a
  symlink that leaves it (resolved before writing).
- mcp__korax__* are forwarded to a korax-mcp process the HARNESS is the
  client of, so every board call passes the guard too (unscoped WARNs,
  posting while the opening round is sealed, attach/fetch paths).
- mcp__lab__* are lab.py's ToolSpecs, unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codex_sandbox import bwrap_argv, scoped
from .lab import ToolSpec

OUTPUT_MAX = 30_000  # characters of tool output returned to the model (the rest is saved, never dropped)
READ_LINES = 2_000
READ_LINE_CHARS = 2_000
GLOB_MAX = 500


def text_result(s: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": s}], "is_error": is_error}


def result_text(res: dict[str, Any]) -> str:
    parts = []
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text", "")))
        elif isinstance(c, dict):
            parts.append(json.dumps(c)[:2000])
    return "\n".join(parts)


def clip_middle(s: str, limit: int = OUTPUT_MAX, note: str = "") -> str:
    if len(s) <= limit:
        return s
    head = limit // 4
    tail = limit - head
    return f"{s[:head]}\n[... {len(s) - limit} characters omitted{note} ...]\n{s[-tail:]}"


@dataclass
class SandboxSpec:
    """Everything needed to put one command in this agent's sandbox."""

    workspace: Path
    deny_paths: list[str]
    env: dict[str, str]
    board_sock: Path | None
    board_port: int | None
    slice_name: str | None
    cpus: str | None

    def argv(self, command: str) -> list[str]:
        return scoped(bwrap_argv(self.workspace, self.deny_paths, self.env, command,
                                 self.board_sock, self.board_port), self.slice_name, self.cpus)


@dataclass
class BgTask:
    id: str
    command: str
    proc: asyncio.subprocess.Process
    log: Path
    started: float
    reported: bool = False
    exit_code: int | None = None


@dataclass
class ShellHost:
    """Runs sandboxed commands for one agent; owns its background tasks."""

    sandbox: SandboxSpec
    default_timeout_s: int = 120
    max_timeout_s: int = 600
    tasks: dict[str, BgTask] = field(default_factory=dict)
    foreground: set[asyncio.subprocess.Process] = field(default_factory=set)
    _n: int = 0

    @property
    def bg_dir(self) -> Path:
        # .tmp/ is excluded from the lab's sync: background logs never reach the node
        return self.sandbox.workspace / ".tmp" / "bg"

    async def _spawn(self, command: str, stdout: Any) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *self.sandbox.argv(command), stdin=asyncio.subprocess.DEVNULL, stdout=stdout,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        for sig in (15, 9):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                await asyncio.wait_for(proc.wait(), 3)
                return
            except asyncio.TimeoutError:
                continue

    async def run(self, command: str, timeout_s: float | None) -> tuple[int, str]:
        t = min(float(timeout_s or self.default_timeout_s), float(self.max_timeout_s))
        proc = await self._spawn(command, asyncio.subprocess.PIPE)
        self.foreground.add(proc)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), t)
            rc = proc.returncode if proc.returncode is not None else -1
            text = (out or b"").decode(errors="replace")
        except asyncio.TimeoutError:
            await self._kill(proc)
            out = await proc.stdout.read() if proc.stdout else b""
            rc = 124
            text = (out or b"").decode(errors="replace") + f"\n[harness: killed after {t:.0f}s; " \
                   "use run_in_background for long commands]"
        finally:
            self.foreground.discard(proc)
        return rc, text

    async def start_background(self, command: str) -> BgTask:
        self._n += 1
        tid = f"bg{self._n}"
        self.bg_dir.mkdir(parents=True, exist_ok=True)
        log = self.bg_dir / f"{tid}.log"
        fh = log.open("wb")
        try:
            proc = await self._spawn(command, fh)
        finally:
            fh.close()  # the child holds its own descriptor
        task = BgTask(tid, command, proc, log, time.time())
        self.tasks[tid] = task
        return task

    def poll(self) -> list[BgTask]:
        """Background tasks that finished since the last poll."""
        done = []
        for t in self.tasks.values():
            if t.exit_code is None and t.proc.returncode is not None:
                t.exit_code = t.proc.returncode
            if t.exit_code is not None and not t.reported:
                t.reported = True
                done.append(t)
        return done

    async def stop(self, tid: str) -> str:
        t = self.tasks.get(tid)
        if t is None:
            return f"no background task {tid!r}; known: {', '.join(self.tasks) or 'none'}"
        await self._kill(t.proc)
        t.exit_code = t.proc.returncode
        return f"stopped {tid} (exit {t.exit_code})"

    async def close_foreground(self) -> None:
        for p in list(self.foreground):
            await self._kill(p)

    async def close(self) -> None:
        await self.close_foreground()
        for t in self.tasks.values():
            await self._kill(t.proc)


def _resolve(root: Path, candidate: str) -> Path:
    p = Path(os.path.expanduser(candidate))
    if not p.is_absolute():
        p = root / p
    return Path(os.path.realpath(p))


def _inside(root: Path, p: Path) -> bool:
    return p == root or root in p.parents


def local_tool_specs(shell: ShellHost, workspace: Path, with_shell: bool = True) -> list[ToolSpec]:
    """Bash/Read/Write/Edit/Glob/Grep/TaskOutput/TaskStop, named and shaped
    like Claude Code's own tools. The guard has already judged every call."""
    root = Path(os.path.realpath(workspace))

    def finished_note() -> str:
        done = shell.poll()
        if not done:
            return ""
        return "\n" + "\n".join(f"[background task {t.id} finished, exit {t.exit_code}: {t.log.relative_to(root)}]"
                                for t in done)

    async def bash(args: dict[str, Any]) -> dict[str, Any]:
        cmd = str(args.get("command", ""))
        if not cmd.strip():
            return text_result("command is empty", True)
        if args.get("run_in_background"):
            t = await shell.start_background(cmd)
            return text_result(f"started background task {t.id}; its output goes to {t.log.relative_to(root)}. "
                               f"TaskOutput(task_id='{t.id}') shows it, TaskStop stops it.")
        timeout_ms = args.get("timeout")
        rc, out = await shell.run(cmd, (float(timeout_ms) / 1000) if isinstance(timeout_ms, (int, float)) else None)
        if len(out) > OUTPUT_MAX:
            shell.bg_dir.mkdir(parents=True, exist_ok=True)
            saved = shell.bg_dir / f"fg-{time.strftime('%H%M%S')}-{os.getpid()}-{int(time.time()*1000) % 100000}.log"
            saved.write_text(out)
            out = clip_middle(out, note=f"; full output: {saved.relative_to(root)}")
        body = out if out.strip() else "(no output)"
        return text_result(f"{body}\n[exit {rc}]{finished_note()}", rc != 0)

    async def task_output(args: dict[str, Any]) -> dict[str, Any]:
        tid = str(args.get("task_id", ""))
        t = shell.tasks.get(tid)
        if t is None:
            return text_result(f"no background task {tid!r}; known: {', '.join(shell.tasks) or 'none'}", True)
        try:
            data = t.log.read_text(errors="replace")
        except OSError as e:
            data = f"(log unreadable: {e})"
        state = "running" if t.proc.returncode is None else f"finished, exit {t.proc.returncode}"
        return text_result(f"[{tid}: {state}; {time.time() - t.started:.0f}s since start; "
                           f"log {t.log.relative_to(root)}]\n{clip_middle(data)}")

    async def task_stop(args: dict[str, Any]) -> dict[str, Any]:
        return text_result(await shell.stop(str(args.get("task_id", ""))))

    async def read(args: dict[str, Any]) -> dict[str, Any]:
        p = _resolve(root, str(args.get("file_path", "")))
        if p.is_dir():
            return text_result(f"{p} is a directory; use Bash `ls` or Glob", True)
        try:
            raw = p.read_bytes()
        except OSError as e:
            return text_result(f"cannot read {p}: {e}", True)
        if b"\0" in raw[:8192]:
            return text_result(f"{p} looks binary ({len(raw)} bytes); not shown", True)
        lines = raw.decode(errors="replace").splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, int(args.get("limit") or READ_LINES))
        chunk = lines[offset - 1: offset - 1 + limit]
        body = "\n".join(f"{i:6d}\t{ln[:READ_LINE_CHARS]}" for i, ln in enumerate(chunk, start=offset))
        more = len(lines) - (offset - 1 + len(chunk))
        tail = f"\n[{more} more lines; read on with offset={offset + len(chunk)}]" if more > 0 else ""
        return text_result((body or "(empty file)") + tail)

    async def write(args: dict[str, Any]) -> dict[str, Any]:
        p = _resolve(root, str(args.get("file_path", "")))
        if not _inside(root, p):  # the guard judged it; re-check after resolving (defence in depth)
            return text_result(f"{p}: you can only write inside your workspace {root}", True)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(args.get("content", "")))
        except OSError as e:
            return text_result(f"cannot write {p}: {e}", True)
        return text_result(f"wrote {p.relative_to(root)} ({len(str(args.get('content', '')))} chars)")

    async def edit(args: dict[str, Any]) -> dict[str, Any]:
        p = _resolve(root, str(args.get("file_path", "")))
        if not _inside(root, p):
            return text_result(f"{p}: you can only write inside your workspace {root}", True)
        old, new = str(args.get("old_string", "")), str(args.get("new_string", ""))
        try:
            s = p.read_text()
        except OSError as e:
            return text_result(f"cannot read {p}: {e}", True)
        if not old:
            return text_result("old_string is empty", True)
        n = s.count(old)
        if n == 0:
            return text_result(f"old_string not found in {p.relative_to(root)}", True)
        if n > 1 and not args.get("replace_all"):
            return text_result(f"old_string occurs {n} times in {p.relative_to(root)}; give more context "
                               "or set replace_all", True)
        s = s.replace(old, new) if args.get("replace_all") else s.replace(old, new, 1)
        p.write_text(s)
        return text_result(f"edited {p.relative_to(root)} ({n if args.get('replace_all') else 1} replacement(s))")

    async def glob(args: dict[str, Any]) -> dict[str, Any]:
        base = _resolve(root, str(args.get("path") or "."))
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return text_result("pattern is empty", True)
        if pattern.startswith("/"):
            base, pattern = Path("/"), pattern.lstrip("/")
        try:
            hits = []
            for h in base.glob(pattern):
                hits.append(h)
                if len(hits) > 5 * GLOB_MAX:
                    break
        except (OSError, ValueError) as e:
            return text_result(f"glob failed: {e}", True)
        hits.sort(key=lambda h: h.stat().st_mtime if h.exists() else 0, reverse=True)
        shown = [str(h) for h in hits[:GLOB_MAX]]
        more = f"\n[{len(hits) - GLOB_MAX}+ more not shown]" if len(hits) > GLOB_MAX else ""
        return text_result(("\n".join(shown) or "(no matches)") + more)

    async def grep(args: dict[str, Any]) -> dict[str, Any]:
        import shlex
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return text_result("pattern is empty", True)
        cmd = ["rg", "--no-heading", "--line-number", "--color", "never", "--max-columns", "400"]
        if args.get("-i") or args.get("case_insensitive"):
            cmd.append("-i")
        mode = args.get("output_mode") or "files_with_matches"
        if mode == "files_with_matches":
            cmd.append("-l")
        elif mode == "count":
            cmd.append("-c")
        if args.get("glob"):
            cmd += ["--glob", str(args["glob"])]
        cmd += ["-e", pattern, "--", str(args.get("path") or ".")]
        rc, out = await shell.run(shlex.join(cmd), 60)
        if rc == 1 and not out.strip():
            return text_result("(no matches)")
        return text_result(clip_middle(out) or "(no output)", rc not in (0, 1))

    str_ = {"type": "string"}
    specs = [
        ToolSpec("Read", "Read a file (anywhere you are allowed to read). Returns numbered lines; use offset/limit "
                 "for long files.",
                 {"type": "object", "properties": {"file_path": str_, "offset": {"type": "integer"},
                                                   "limit": {"type": "integer"}}, "required": ["file_path"]}, read),
        ToolSpec("Write", "Write a file inside your workspace (creates parent directories; overwrites).",
                 {"type": "object", "properties": {"file_path": str_, "content": str_},
                  "required": ["file_path", "content"]}, write),
        ToolSpec("Edit", "Replace old_string with new_string in a file inside your workspace. old_string must "
                 "occur exactly once unless replace_all is true.",
                 {"type": "object", "properties": {"file_path": str_, "old_string": str_, "new_string": str_,
                                                   "replace_all": {"type": "boolean"}},
                  "required": ["file_path", "old_string", "new_string"]}, edit),
        ToolSpec("Glob", "Find files by glob pattern (e.g. **/*.py), newest first.",
                 {"type": "object", "properties": {"pattern": str_, "path": str_}, "required": ["pattern"]}, glob),
        ToolSpec("Grep", "Search file contents with ripgrep. output_mode: files_with_matches (default), content, "
                 "or count.",
                 {"type": "object", "properties": {"pattern": str_, "path": str_, "glob": str_,
                                                   "output_mode": {"type": "string",
                                                                   "enum": ["files_with_matches", "content", "count"]},
                                                   "-i": {"type": "boolean"}},
                  "required": ["pattern"]}, grep),
    ]
    if with_shell:
        specs += [
            ToolSpec("Bash", "Run a bash command in your sandboxed shell. It starts in your workspace each time "
                     "(state such as cd or exported variables does not carry over between calls). It can write "
                     "only inside your workspace; its network reaches only this board. timeout is in "
                     "milliseconds (default 120000, max 600000). For long jobs set run_in_background: the "
                     "command keeps running, its output goes to a log file, and TaskOutput/TaskStop manage it. "
                     "Processes started with `&` in a foreground call end when that call ends.",
                     {"type": "object", "properties": {
                         "command": str_, "timeout": {"type": "integer", "description": "milliseconds"},
                         "run_in_background": {"type": "boolean"},
                         "description": {"type": "string", "description": "optional: what this command does"}},
                      "required": ["command"]}, bash),
            ToolSpec("TaskOutput", "Show a background task's status and output so far.",
                     {"type": "object", "properties": {"task_id": str_}, "required": ["task_id"]}, task_output),
            ToolSpec("TaskStop", "Stop a background task.",
                     {"type": "object", "properties": {"task_id": str_}, "required": ["task_id"]}, task_stop),
        ]
    return specs


class KoraxBridge:
    """The agent's korax MCP server, with the HARNESS as its client, so every
    board call is visible to (and judged by) the guard before it runs."""

    def __init__(self, command: list[str], env: dict[str, str], cwd: Path, deny: list[str],
                 errlog: Path | None = None):
        self.command, self.env, self.cwd, self.deny = command, env, cwd, set(deny)
        self.errlog = errlog  # korax-mcp's stderr (its HTTP log is chatty): a file, not the run's console
        self.session: Any = None
        self.tools: list[Any] = []

    async def open(self, stack: Any) -> None:
        """Enter the MCP client into `stack` (an AsyncExitStack owned by the
        agent's task: the MCP SDK's task groups must exit in the task that
        entered them)."""
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=self.command[0], args=self.command[1:], env=self.env, cwd=self.cwd)
        if self.errlog is not None:
            err = stack.enter_context(self.errlog.open("a"))
            read, write = await stack.enter_async_context(stdio_client(params, errlog=err))
        else:
            read, write = await stack.enter_async_context(stdio_client(params))
        self.session = await stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = [t for t in listed.tools if t.name not in self.deny]

    def specs(self) -> list[ToolSpec]:
        out = []
        for t in self.tools:
            async def call(args: dict[str, Any], _name: str = t.name) -> dict[str, Any]:
                try:
                    r = await self.session.call_tool(_name, args, read_timeout_seconds=900)
                except Exception as e:  # a board or client hiccup is a tool error, never a crash
                    return text_result(f"korax MCP call failed: {type(e).__name__}: {e}", True)
                content = []
                for c in getattr(r, "content", None) or []:
                    if getattr(c, "type", None) == "text":
                        content.append({"type": "text", "text": c.text})
                    else:
                        content.append({"type": "text", "text": f"[{getattr(c, 'type', '?')} content omitted]"})
                sc = getattr(r, "structured_content", None)
                if not content and sc is not None:
                    content.append({"type": "text", "text": json.dumps(sc)})
                return {"content": content, "is_error": bool(getattr(r, "is_error", False))}
            out.append(ToolSpec(f"mcp__korax__{t.name}", t.description or "", t.input_schema, call))
        return out

