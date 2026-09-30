"""The Codex backend: one agent's life on `codex app-server`.

Same run semantics as the Claude backend (agent.Agent, which this extends):
empty/neutral base instructions, the orientation as the first message, the
continue message plus the board digest after each turn, the harness's own
budget from token usage, the wall clock enforced mid-turn, and transcript
rows in the Claude backend's shapes so `swarm status`, the panel and
every other transcript reader read them unchanged.

What Codex itself contributes is only the model loop. Its built-in tools
(shell, apply_patch, web search, sub-agents, memories, apps, browser,
image tools, clock, request_user_input, update_plan, code mode) are
switched off by config and by a stripped local model catalog, and the
injected context blocks (environment_context with date/timezone,
permissions, collaboration mode, skills, apps, the AGENTS project doc) are switched off
too; every tool the model sees is a harness-hosted dynamic tool (see
codex_tools.py). As a tripwire, any built-in tool item that still appears
stops the agent (fail closed), and every approval-type server request is
declined.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from . import board
from .agent import Agent, AgentState, _BudgetExhausted, _jsonable
from .codex_rpc import AppServer, Refused, RpcError, ServerGone
from .codex_sandbox import BoardForwarder, board_sock_path, configure_slice, scoped, slice_name_for, stop_slice
from .codex_tools import KoraxBridge, ShellHost, SandboxSpec, local_tool_specs, result_text
from .config import RunConfig
from .guard import make_guard
from .lab import ToolSpec, lab_tool_specs

log = logging.getLogger("swarm")

KORAX_DENY = ["korax_enlist", "korax_animate", "korax_credentials", "korax_rotate"]
ACTIVE_TOOLS = {"Write", "Edit", "Bash", "mcp__lab__run", "mcp__lab__score", "mcp__lab__submit", "mcp__lab__propose",
                "mcp__lab__answer", "mcp__korax__korax_post"}
# Built-in Codex tool items: if one ever appears, a tool slipped past the config (fail closed).
FORBIDDEN_ITEMS = {"commandExecution", "fileChange", "mcpToolCall", "webSearch", "collabAgentToolCall",
                   "imageGeneration", "imageView", "subAgentActivity", "sleep"}
RESULT_CHARS = 20_000  # tool output kept per transcript row

# Codex features that are ON by default in 0.159 and must be OFF here: every
# tool family Codex would add, and every source of injected context
# (current_time_reminder / rollout_budget / token_budget would tell the model
# the time or a budget). Unknown names are ignored by older/newer versions.
FEATURES_OFF = [
    "shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot", "apps", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "computer_use", "image_generation", "view_image", "multi_agent", "multi_agent_v2",
    "memories", "goals", "plugins", "remote_plugin", "skill_search", "tool_suggest", "hooks", "sleep_tool",
    "code_mode_host", "code_mode", "code_mode_only", "in_app_browser", "workspace_dependencies", "worktrees",
    "current_time_reminder", "rollout_budget", "token_budget", "realtime_conversation", "fast_mode",
    "guardian_approval", "skill_mcp_dependency_install", "standalone_web_search", "in_app_chat",
    "in_app_dictation", "in_app_local_automation", "in_app_updates", "mentions_v2", "plugin_sharing",
    "recommended_plugins", "artifact", "chronicle", "agent_message_board", "send_message_to_user_async",
    "context_management", "deferred_tool_world_state", "request_permissions_tool", "default_mode_request_user_input",
    "terminal_visualization_instructions", "auth_elicitation", "tool_call_mcp_elicitation", "prevent_idle_sleep",
]
BASE_OVERRIDES = [
    "include_environment_context=false",        # <environment_context>: cwd, shell, DATE, TIMEZONE, filesystem
    "include_permissions_instructions=false",   # sandbox/approval/network text
    "include_apps_instructions=false",
    "include_collaboration_mode_instructions=false",
    "project_doc_max_bytes=0",                  # no AGENTS project doc
    'web_search="disabled"',
    "skills.include_instructions=false",
    "tools.experimental_request_user_input={enabled=false}",
    "tools.update_plan={enabled=false}",
    'approval_policy="never"',
    'sandbox_mode="read-only"',                 # belt and braces: no built-in tool should exist to use it
    "check_for_update_on_startup=false",
    "analytics.enabled=false",
    'history.persistence="none"',
    'cli_auth_credentials_store="file"',        # auth.json, which is a symlink to the one shared login
]


def codex_overrides(catalog: Path, extra: list[str]) -> list[str]:
    ov = list(BASE_OVERRIDES) + [f"features.{f}=false" for f in FEATURES_OFF]
    ov.append(f"model_catalog_json={json.dumps(str(catalog))}")
    return ov + list(extra)


def strip_catalog_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A model catalog entry with Codex's per-model tool and prompt add-ons
    removed: `tool_mode: code_mode_only` wraps every tool in a JavaScript
    `exec` tool (and adds apply_patch and a clock tool that tells the time),
    `multi_agent_version` adds the sub-agent tools and a role prompt,
    `experimental_supported_tools` adds e.g. `clock`, `model_messages`
    carries the instruction templates. tools/codex_leak_probe.py captures
    the actual request and checks that none of these reach it."""
    e = dict(entry)
    for k in ("tool_mode", "multi_agent_version", "multi_agent_reasoning_effort"):
        e.pop(k, None)
    e.update(experimental_supported_tools=[], apply_patch_tool_type=None, supports_search_tool=False,
             include_skills_usage_instructions=False, include_plugin_usage_instructions=False,
             include_apps_usage_instructions=False, model_messages=None, base_instructions="")
    return e


def build_catalog(cfg: RunConfig, out: Path, home: Path) -> Path:
    """Write the stripped one-model catalog for this run, from the installed
    CLI's live catalog (its shape is version-specific, so it is never
    hand-written)."""
    home.mkdir(parents=True, exist_ok=True)
    link_auth(home, cfg.codex.auth_file)
    r = subprocess.run([cfg.codex.binary, "debug", "models"], capture_output=True, text=True, timeout=120,
                       env=codex_env(home), cwd=str(home))
    if r.returncode:
        raise RuntimeError(f"`codex debug models` failed ({r.returncode}): {r.stderr.strip()[-500:]}")
    models = json.loads(r.stdout).get("models") or []
    hit = [m for m in models if m.get("slug") == cfg.model]
    if not hit:
        raise RuntimeError(f"model {cfg.model!r} is not in the Codex catalog: {[m.get('slug') for m in models]}")
    out.write_text(json.dumps({"models": [strip_catalog_entry(hit[0])]}, indent=1))
    return out


def link_auth(home: Path, auth_file: Path) -> None:
    """auth.json in `home` is a SYMLINK to the one real login. Codex writes a
    refreshed token through the symlink (checked with a dummy file: the link
    survives, the target changes), and re-reads it before refreshing, so
    every agent and the operator's own Codex keep one live refresh token. A
    per-agent COPY would rotate the refresh token in one place and leave the
    original stale. The harness never reads this file."""
    link = home / "auth.json"
    if link.is_symlink() and os.readlink(link) == str(auth_file):
        return
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(auth_file)


def codex_env(home: Path) -> dict[str, str]:
    """A minimal environment: no API keys, no proxies, no user config."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["CODEX_HOME"] = str(home)
    return env


def _usage_keys(b: dict[str, Any]) -> dict[str, int]:
    """OpenAI usage in the Claude backend's keys (see config.Prices)."""
    inp, cached, out = int(b.get("inputTokens") or 0), int(b.get("cachedInputTokens") or 0), int(b.get("outputTokens") or 0)
    return {"input_tokens": max(0, inp - cached), "cache_read_input_tokens": cached, "output_tokens": out,
            "reasoning_output_tokens": int(b.get("reasoningOutputTokens") or 0)}


def _is_work(name: str, args: dict[str, Any]) -> bool:
    if name not in ACTIVE_TOOLS:
        return False
    if name == "Bash":
        cmd = str(args.get("command", "")).lstrip()
        return not (cmd.startswith("korax ") or cmd == "korax")
    return True


def _leaf(eg: BaseException) -> BaseException:
    """The first non-group exception inside an exception group: the real cause."""
    while isinstance(eg, BaseExceptionGroup) and eg.exceptions:
        eg = eg.exceptions[0]
    return eg


class CodexFailClosed(Exception):
    """A built-in Codex tool appeared: the configuration did not hold."""


class CodexAgent(Agent):
    """An Agent (shared digest/idle/budget logic) whose turns run on Codex."""

    def __init__(self, *a: Any, catalog: Path, **kw: Any):
        super().__init__(*a, **kw)
        self.catalog = catalog
        p = self.slot.private
        self.home = p / "codex-home"
        self.slice = slice_name_for(self.cfg.run_name, self.slot.name) if self.cfg.local_cpu_quota else None
        self.cpus = (str(self.cfg.local_cpu_for(self.slot.index))
                     if self.cfg.local_cpu_quota and self.cfg.local_pin_cpus else None)
        # the harness-side private files the sandbox must never show: this agent's own private dir
        self.deny_paths = sorted({*(self.deny_paths or self.cfg.deny_paths()), str(p), str(self.home)})
        self.thread_id: str | None = None
        self.tools: dict[str, ToolSpec] = {}
        self.shell: ShellHost | None = None
        self._active = False
        self._conn_cost = 0.0  # this app-server connection's running estimate (ResultMessage.total_cost_usd)
        self._prev_total: dict[str, int] | None = None
        self._fail_closed: str | None = None
        self.extra_env: dict[str, str] = {}  # added to the app-server's environment (tests)
        self.guard = make_guard(self.slot.workspace, ("mcp__korax__", "mcp__lab__"), self.cfg.deny_patterns(),
                                audit=p / "audit.jsonl", shell=self.cfg.shell,
                                posting_blocked=None)

    # -- environment
    def korax_env(self) -> dict[str, str]:
        return {"KORAX_URL": self.cred.url, "KORAX_TOKEN": self.cred.token, "KORAX_IDENTITY": self.cred.identity,
                "KORAX_CONFIG_DIR": str(self.slot.private / "korax-config")}

    def sandbox_env(self) -> dict[str, str]:
        shellbin = Path(__file__).resolve().parent / "shellbin"
        env = {**self.korax_env(), "KORAX_REAL_BIN": str(self.cfg.korax_cli_bin), "SWARM_NS": self.cfg.board.ns,
               "PATH": f"{shellbin}:/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
               "LANG": "C.UTF-8", "TERM": "dumb", "PYTHONDONTWRITEBYTECODE": "1",
               "TMPDIR": "/tmp"}
        if self.cpus is not None:
            env["PYTHON_CPU_COUNT"] = "1"  # os.cpu_count() agrees with the one pinned core
        return env

    def app_server_argv(self) -> list[str]:
        argv = [self.cfg.codex.binary, "app-server", "--listen", "stdio://"]
        for kv in codex_overrides(self.catalog, self.cfg.codex.extra_config):
            argv += ["-c", kv]
        return scoped(argv, self.slice, self.cpus)

    def dynamic_tools(self) -> list[dict[str, Any]]:
        """Local tools as plain functions; MCP-style tools (Codex reserves the
        `mcp__` prefix for its own MCP client) as the namespaces `korax` and
        `lab`, called by the model as e.g. `korax.korax_post` and answered
        here under the Claude backend's names (`mcp__korax__korax_post`), so
        the guard and every transcript reader see one naming."""
        plain, spaces = [], {}
        for s in self.tools.values():
            if s.name.startswith("mcp__"):
                _, server, tool = s.name.split("__", 2)
                spaces.setdefault(server, []).append(
                    {"type": "function", "name": tool, "description": s.description, "inputSchema": s.schema})
            else:
                plain.append({"type": "function", "name": s.name, "description": s.description,
                              "inputSchema": s.schema})
        blurb = {"korax": "The Korax board, as your band (the same operations as the korax CLI in your shell).",
                 "lab": "The lab: runs your code on your reserved compute-node cores."}
        return plain + [{"type": "namespace", "name": ns, "description": blurb.get(ns, ns), "tools": tools}
                        for ns, tools in spaces.items()]

    # -- server requests
    async def on_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "item/tool/call":
            return await self._tool_call(params)
        blob = json.dumps(params, default=str)
        self._record({"_type": "codex_server_request", "method": method,
                      "params": params if len(blob) <= 4000 else blob[:4000]})
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return {"decision": "decline"}
        if method in ("applyPatchApproval", "execCommandApproval"):
            return {"decision": "denied"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            return {"action": "decline"}
        # currentTime/read (a clock), auth refresh, attestation, permissions: refused
        raise Refused(f"{method} is not served by this harness")

    async def _tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("tool", ""))
        if params.get("namespace"):
            name = f"mcp__{params['namespace']}__{name}"
        call_id = str(params.get("callId", ""))
        args = params.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {"_value": args}
        self._record({"_type": "AssistantMessage", "content": [{"id": call_id, "name": name, "input": args}],
                      "model": self.cfg.model, "message_id": None, "usage": None})
        res = await self._run_tool(name, call_id, args)
        text = result_text(res)
        is_error = bool(res.get("is_error"))
        self._record({"_type": "UserMessage", "content": [{"tool_use_id": call_id, "content": text[:RESULT_CHARS],
                                                           "is_error": is_error}]})
        if _is_work(name, args):
            self._active = True
        items = [{"type": "inputText", "text": text or "(no output)"}]
        # the in-turn board pulse (pulse.py), the Codex twin of the Claude PostToolUse hook:
        # appended as a separate item of this tool result, so the tool's own output is untouched
        pulse = await self._pulse_text(call_id)
        if pulse:
            items.append({"type": "inputText", "text": pulse})
        return {"contentItems": items, "success": not is_error}

    async def _pulse_text(self, call_id: str) -> str | None:
        if self.pulse is None:
            return None
        try:
            text = await self.pulse.poll()
        except Exception as e:
            self._record({"_type": "harness_error", "error": f"pulse: {type(e).__name__}: {e}"})
            return None
        if text:
            self._record({"_type": "harness_pulse", "tool_use_id": call_id, "text": text})
        return text

    async def _run_tool(self, name: str, call_id: str, args: dict[str, Any]) -> dict[str, Any]:
        spec = self.tools.get(name)
        if spec is None:
            return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}], "is_error": True}
        try:
            verdict = await self.guard({"tool_name": name, "tool_input": args}, call_id, None)
        except Exception as e:  # a guard bug denies, never allows
            verdict = {"hookSpecificOutput": {"permissionDecision": "deny",
                                              "permissionDecisionReason": f"guard error: {type(e).__name__}: {e}"}}
        hso = verdict.get("hookSpecificOutput") or {}
        if hso.get("permissionDecision") == "deny":
            return {"content": [{"type": "text", "text": str(hso.get("permissionDecisionReason", "denied"))}],
                    "is_error": True}
        try:
            return await spec.handler(args)
        except Exception as e:
            log.exception("%s: tool %s failed", self.slot.name, name)
            return {"content": [{"type": "text", "text": f"tool error: {type(e).__name__}: {e}"}], "is_error": True}

    # -- one turn
    async def _turn(self, srv: AppServer, message: str) -> bool:
        self._active = False
        params: dict[str, Any] = {"threadId": self.thread_id,
                                  "input": [{"type": "text", "text": message, "text_elements": []}]}
        if self.cfg.effort:
            params["effort"] = self.cfg.effort
        res = await srv.request("turn/start", params, self.cfg.codex.request_timeout_s)
        turn_id = (res or {}).get("turn", {}).get("id")
        while True:
            left = self.deadline - time.time()
            try:
                n = await srv.next_notification(timeout=max(1.0, min(left, 60.0)))
            except asyncio.TimeoutError:
                if time.time() >= self.deadline:
                    self.state.stop_reason = "wall clock (interrupted mid-turn)"
                    self._record({"_type": "harness_deadline", "note": "interrupted a turn at the wall-clock limit"})
                    await self._interrupt(srv, turn_id)
                    raise _BudgetExhausted()
                continue
            method, p = n.get("method", ""), n.get("params") or {}
            if method == "_server_gone":
                raise ServerGone("app-server exited mid-turn")
            if method == "item/completed":
                self._on_item(p.get("item") or {})
                if self._fail_closed:
                    await self._interrupt(srv, turn_id)
                    raise CodexFailClosed(self._fail_closed)
            elif method == "thread/tokenUsage/updated":
                await self._on_usage(p)
                if self.state.spent >= self.cfg.per_agent_budget_usd:
                    self.state.stop_reason = "agent budget (harness cap)"
                    await self._interrupt(srv, turn_id)
                    raise _BudgetExhausted()
            elif method == "turn/completed":
                turn = p.get("turn") or {}
                if turn_id and turn.get("id") not in (None, turn_id):
                    continue
                self.state.turns += 1
                self.state.session_id = self.thread_id
                self._record({"_type": "ResultMessage", "subtype": turn.get("status"), "session_id": self.thread_id,
                              "total_cost_usd": round(self._conn_cost, 6), "error": turn.get("error"),
                              "duration_ms": turn.get("durationMs")})
                await self.ledger.set(self.slot.name, self.state.spent)
                if turn.get("status") == "failed":
                    err = turn.get("error") or {}
                    info = err.get("codexErrorInfo")
                    if info in ("usageLimitExceeded", "unauthorized", "cyberPolicy"):
                        self.state.stop_reason = f"codex: {info}: {str(err.get('message'))[:200]}"
                        raise _BudgetExhausted()
                    raise ServerGone(f"turn failed: {info}: {str(err.get('message'))[:300]}")
                break
            elif method in ("item/agentMessage/delta", "item/reasoning/summaryTextDelta", "item/reasoning/textDelta",
                            "item/started", "item/plan/delta", "thread/status/changed", "turn/started",
                            "serverRequest/resolved", "item/reasoning/summaryPartAdded", "turn/diff/updated"):
                continue
            else:
                self._record({"_type": "codex_event", "method": method,
                              "params": p if len(json.dumps(p, default=str)) < 4000 else "(large)"})
        if not self._active:
            self.state.idle_turns += 1
        return self._active

    def _on_item(self, item: dict[str, Any]) -> None:
        kind = item.get("type")
        if kind == "agentMessage":
            if (item.get("text") or "").strip():
                self._record({"_type": "AssistantMessage", "content": [{"text": item["text"]}],
                              "model": self.cfg.model, "message_id": item.get("id"), "usage": None,
                              "phase": item.get("phase")})
        elif kind in FORBIDDEN_ITEMS:
            self._fail_closed = f"a built-in Codex tool ran ({kind}); the configuration did not hold"
            self._record({"_type": "harness_error", "error": self._fail_closed, "item": _jsonable(item)})
        elif kind == "contextCompaction":
            self._record({"_type": "codex_event", "method": "contextCompaction"})

    async def _on_usage(self, p: dict[str, Any]) -> None:
        total = (p.get("tokenUsage") or {}).get("total") or {}
        cur = _usage_keys(total)
        prev = self._prev_total
        if prev is None or cur["input_tokens"] + cur["cache_read_input_tokens"] < prev["input_tokens"] + prev["cache_read_input_tokens"]:
            delta = cur  # first update of this connection, or a reset count
        else:
            delta = {k: max(0, cur[k] - prev.get(k, 0)) for k in cur}
        self._prev_total = cur
        cost = self.cfg.prices.cost(delta)
        self._conn_cost += cost
        self.state.estimate += cost
        self._record({"_type": "AssistantMessage", "content": [], "model": self.cfg.model,
                      "message_id": f"usage:{self.thread_id}:{sum(cur.values())}", "usage": delta,
                      "context_window": (p.get("tokenUsage") or {}).get("modelContextWindow")})
        await self.ledger.set(self.slot.name, self.state.spent)

    async def _interrupt(self, srv: AppServer, turn_id: str | None) -> None:
        if self.shell:
            await self.shell.close_foreground()
        if not turn_id:
            return
        try:
            await srv.request("turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id}, 30)
        except Exception:
            pass

    # -- a connection
    async def _setup_tools(self, stack: contextlib.AsyncExitStack) -> None:
        self.tools = {}
        if self.cfg.shell:
            fwd = None
            sock = None
            from urllib.parse import urlsplit
            u = urlsplit(self.cred.url)
            port = u.port or (443 if u.scheme == "https" else 80)
            sock = board_sock_path(self.cfg.run_name, self.slot.name)
            fwd = BoardForwarder(sock, u.hostname or "127.0.0.1", port)
            await fwd.start()
            stack.push_async_callback(fwd.close)
            self.shell = ShellHost(SandboxSpec(self.slot.workspace, self.deny_paths, self.sandbox_env(), sock, port,
                                               self.slice, self.cpus),
                                   self.cfg.codex.bash_timeout_s, self.cfg.codex.bash_max_timeout_s)
            stack.push_async_callback(self.shell.close)
        else:
            self.shell = None
        for s in local_tool_specs(self.shell, self.slot.workspace, with_shell=self.cfg.shell):  # type: ignore[arg-type]
            if s.name in self.cfg.builtin_tools or s.name in ("Bash", "TaskOutput", "TaskStop"):
                self.tools[s.name] = s
        bridge = KoraxBridge(self.korax_mcp, {**codex_env(self.home), **self.korax_env()}, self.slot.workspace,
                             KORAX_DENY, errlog=self.slot.private / "korax-mcp.log")
        await bridge.open(stack)
        for s in bridge.specs():
            self.tools[s.name] = s
        if self.lab is not None:
            for s in lab_tool_specs(self.lab, self.slot, self.cred, self.opening_round, self.pulse):
                self.tools[f"mcp__lab__{s.name}"] = ToolSpec(f"mcp__lab__{s.name}", s.description, s.schema, s.handler)

    async def _connect(self, stack: contextlib.AsyncExitStack) -> AppServer:
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        link_auth(self.home, self.cfg.codex.auth_file)
        if self.slice:
            configure_slice(self.slice, self.cfg.local_cpu_quota, self.cfg.local_mem_max)
        srv = AppServer(self.app_server_argv(), {**codex_env(self.home), **self.extra_env}, str(self.slot.workspace),
                        self.on_request,
                        on_stderr=self._on_stderr)
        await srv.start()
        stack.push_async_callback(srv.close)
        self._record({"_type": "codex_start", "pid": srv.pid, "slice": self.slice, "cpus": self.cpus})
        init = await srv.request("initialize", {"clientInfo": {"name": "swarm", "version": "0.1"},
                                                "capabilities": {"experimentalApi": True}},
                                 self.cfg.codex.request_timeout_s)
        await srv.notify("initialized")
        self._record({"_type": "codex_init", "userAgent": (init or {}).get("userAgent")})
        self._prev_total = None
        self._conn_cost = 0.0
        common = {"model": self.cfg.model, "cwd": str(self.slot.workspace), "baseInstructions": self.cfg.system_prompt,
                  "approvalPolicy": "never", "sandbox": "read-only"}
        if self.thread_id:
            try:
                r = await srv.request("thread/resume", {"threadId": self.thread_id, **common},
                                      self.cfg.codex.request_timeout_s)
                self._record({"_type": "codex_thread", "resumed": True, "thread": self.thread_id})
                return srv
            except RpcError as e:
                self._record({"_type": "harness_error", "error": f"thread/resume failed: {e}; starting a new thread"})
        r = await srv.request("thread/start", {**common, "dynamicTools": self.dynamic_tools(), "ephemeral": False},
                              self.cfg.codex.request_timeout_s)
        self.thread_id = r["thread"]["id"]
        self._record({"_type": "codex_thread", "resumed": False, "thread": self.thread_id,
                      "tools": sorted(self.tools)})
        return srv

    def _on_stderr(self, line: str) -> None:
        if line.strip():
            self._record({"_type": "cli_stderr", "line": line[:2000]})

    async def live(self) -> AgentState:
        await self._new_posts()  # start the cursor at the current head
        await self._prime_pulse()
        if self.opening_round is not None:
            self.guard = make_guard(self.slot.workspace, ("mcp__korax__", "mcp__lab__"), self.cfg.deny_patterns(),
                                    audit=self.slot.private / "audit.jsonl", shell=self.cfg.shell,
                                    posting_blocked=lambda: self.opening_round.posting_blocked(self.slot.name))
        message = self.orientation
        fresh_thread_note = ("[The harness restarted your session and could not restore it; your earlier "
                             "conversation is gone, but your workspace and the board are intact.]\n\n")
        while True:
            reason = self._stop_reason()
            if reason:
                self.state.stop_reason = reason
                break
            try:
                try:
                    async with contextlib.AsyncExitStack() as stack:
                        await self._setup_tools(stack)
                        had_thread = self.thread_id
                        srv = await self._connect(stack)
                        if had_thread and self.thread_id != had_thread:
                            message = fresh_thread_note + self.orientation
                        while not (reason := self._stop_reason()):
                            active = await self._turn(srv, message)
                            message = await self._next_message(idle=not active)
                        self.state.stop_reason = reason
                        break
                except BaseExceptionGroup as eg:  # the MCP client's task group wraps whatever ended the session
                    raise _leaf(eg) from eg
            except _BudgetExhausted:
                break
            except CodexFailClosed as e:
                self.state.stop_reason = f"codex tool leak, stopped (fail closed): {e}"[:300]
                log.error("%s: %s", self.slot.name, self.state.stop_reason)
                break
            except (ServerGone, RpcError, OSError, asyncio.TimeoutError, RuntimeError, Exception) as e:
                self._record({"_type": "harness_error", "error": f"{type(e).__name__}: {e}",
                              "traceback": traceback.format_exc()[-4000:]})
                self.state.restarts += 1
                if self.state.restarts > self.cfg.max_turn_restarts:
                    self.state.stop_reason = f"gave up after {self.state.restarts} restarts: {e}"[:300]
                    break
                log.warning("%s: %s; restarting (%d)", self.slot.name, e, self.state.restarts)
                await asyncio.sleep(min(60, 5 * self.state.restarts))
                message = await self._next_message(idle=False)
        if self.slice:
            await asyncio.to_thread(stop_slice, self.slice)
        end = {k: v for k, v in self.state.__dict__.items() if k != "seen_messages"}
        self._record({"_type": "agent_end", **end, "spent": self.state.spent})
        return self.state


async def rate_limits(cfg: RunConfig, home: Path) -> dict[str, Any] | None:
    """One app-server, before any agent starts: reads the account's rate
    limits (and so refreshes the shared login once, if it is due, in ONE
    process rather than in every agent at the same moment)."""
    home.mkdir(parents=True, exist_ok=True)
    link_auth(home, cfg.codex.auth_file)

    async def deny(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("not served")

    srv = AppServer([cfg.codex.binary, "app-server", "--listen", "stdio://", "-c", "analytics.enabled=false"],
                    codex_env(home), str(home), deny)
    await srv.start()
    try:
        await srv.request("initialize", {"clientInfo": {"name": "swarm", "version": "0.1"}}, 60)
        await srv.notify("initialized")
        acct = await srv.request("account/read", {}, 60)
        rl = await srv.request("account/rateLimits/read", None, 60)
        return {"t": time.time(), "account_type": ((acct or {}).get("account") or {}).get("type"),
                "plan": ((acct or {}).get("account") or {}).get("planType"), "rateLimits": rl}
    except Exception as e:
        return {"t": time.time(), "error": f"{type(e).__name__}: {e}"}
    finally:
        await srv.close()
