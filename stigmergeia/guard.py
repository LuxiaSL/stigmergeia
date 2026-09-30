"""PreToolUse guard: what an agent's local tools may touch, and a record of it.

Policy (the operator's call: reading is fine, and watched):
- **Writes** (Write/Edit/MultiEdit/NotebookEdit) stay inside the workspace.
- **Reads** (Read/Glob/Grep) may go anywhere EXCEPT credential locations
  (`deny` globs: ssh keys, board tokens, the API key's homes, other agents'
  private dirs, secrets). Reads outside the workspace are allowed and
  recorded with `outside: true`.
- **Shell** (Bash/Monitor) runs inside Claude Code's bubblewrap sandbox,
  which is configured separately (writes: workspace only; network: the
  board only; the same credential globs as Read deny rules). Every command
  is recorded, so a monitor can watch what agents look at.
- MCP tools pass (their servers scope them). Any other tool is denied, so a
  tool added later is closed until someone decides otherwise.

Every path is judged the way the tool will open it: `~` expanded, relative
paths against the workspace, symlinks resolved.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import time
from pathlib import Path
from typing import Any

WRITE_ARGS: dict[str, tuple[str, ...]] = {
    "Write": ("file_path",), "Edit": ("file_path",), "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
}
READ_ARGS: dict[str, tuple[str, ...]] = {"Read": ("file_path",), "Glob": ("path",), "Grep": ("path",)}
PATTERN_ARGS: dict[str, tuple[str, ...]] = {"Glob": ("pattern",), "Grep": ("glob",)}
SHELL_TOOLS = {"Bash": "command", "Monitor": "command"}
PASSIVE_TOOLS = {"TaskStop", "TaskOutput"}  # manage the agent's own background tasks; nothing to judge
BOARD_NS_PREFIXES = ("/swarm", "/korax/", "/commons/", "/dm/", "/scratch/")
# where the CLI keeps an agent's own background-task output: its own scratch, not a read elsewhere
CLI_SCRATCH = f"/tmp/claude-{os.getuid()}/"


def _resolve(root: Path, candidate: str) -> Path:
    p = Path(os.path.expanduser(candidate))
    if not p.is_absolute():
        p = root / p
    return Path(os.path.realpath(p))


def _inside(root: Path, p: Path) -> bool:
    return p == root or root in p.parents


def _denied(p: Path, deny: list[str]) -> str | None:
    s = str(p)
    for pat in deny:
        # a "dir/**" pattern also covers the directory itself
        if fnmatch.fnmatch(s, pat) or (pat.endswith("/**") and s == pat[:-3]):
            return pat
    return None


WARN_TEMPLATE = ("A WARN that retires an idea must be a scoped claim, not a verdict. Include:\n"
                 "  Tested on: <the base policy / setup, with its code path>\n"
                 "  Result: <the numbers>\n"
                 "  Revive if: <conditions under which it is worth retrying>\n"
                 "(see 'Working together' in the swarm-environment canon). Nothing was posted.")
_TESTED = re.compile(r"tested[\s_-]*on", re.I)
_REVIVE = re.compile(r"revive[\s_-]*if", re.I)


def warn_is_scoped(text: str) -> bool:
    return bool(_TESTED.search(text) and _REVIVE.search(text))


def _cli_warn_payload(cmd: str, root: Path) -> str | None:
    """The payload of a `korax post --type WARN` shell command, or None if
    the command is not one. A --payload-file is read from the workspace."""
    if not re.search(r"\bkorax\s+post\b", cmd) or not re.search(r"--type[=\s]+['\"]?WARN\b", cmd):
        return None
    m = re.search(r"--payload-file[=\s]+(\S+)", cmd)
    if m:
        try:
            return (root / m.group(1).strip("'\"")).read_text()
        except OSError:
            return ""
    return cmd  # inline payload: judge the whole command text


LONG_SLEEP_S = 60  # a foreground shell sleep this long (in total) is refused
LOOP_SLEEP_S = 20  # a sleep this long inside a shell loop is a polling wait: refused too
LONG_SLEEP = ("Not run: a long `sleep` holds your whole turn, and nothing (a job of yours finishing, someone "
              "messaging you) can reach you until it ends. The lab's `wait` tool returns the moment one of your "
              "background jobs finishes or someone addresses you on the board. And while a job runs is a good "
              "moment to read what the others are doing, reply, or review someone's code. (Short sleeps, and "
              "commands with run_in_background, are fine.)")
# `sleep` in command position (start, after ; & | ( { or a newline, or after do/then/else; `time`, `command`,
# `exec`, `env`, `nice` prefixes allowed, since `time sleep 120` is as long a wait as `sleep 120`), its literal
# arguments (each a whole token), and not put in the background by a single trailing `&`
_SLEEP = re.compile(r"(?:^|[;&|(){}\n]|\b(?:do|then|else)\b)\s*(?:(?:time|command|exec|builtin|env|nice)\s+(?:-\S+\s+)*)*sleep"
                    r"((?:[ \t]+(?:\d+(?:\.\d+)?[smhd]?|inf(?:inity)?)(?![\w.]))+)(?![ \t]*&(?!&))", re.I)
_LOOP = re.compile(r"(?<![\w-])(?:while|until|for)\b.*?(?<![\w-])do\b(.*?)(?<![\w-])done\b", re.S)
_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _sleep_seconds(args: str) -> float:
    """The seconds a `sleep` with these arguments sleeps: coreutils sums them, so `sleep 1m 5s` is 65 s."""
    total = 0.0
    for a in args.split():
        if a.lower().startswith("inf"):
            return float("inf")
        unit = a[-1].lower() if a[-1].isalpha() else ""
        total += float(a[:-1] if unit else a) * _UNIT_S[unit]
    return total


def long_sleep(cmd: str) -> float | None:
    """The foreground sleep a shell command would sit in, in seconds, when it is a long wait: the sum of
    its literal `sleep`s (>= LONG_SLEEP_S), or any `sleep` of LOOP_SLEEP_S or more inside a
    for/while/until loop (a polling wait of unknown length: reported as inf). None otherwise. A sleep put
    in the background with a single `&` does not count; `sleep $N` cannot be judged and is let through."""
    for body in _LOOP.findall(cmd):
        if any(_sleep_seconds(m.group(1)) >= LOOP_SLEEP_S for m in _SLEEP.finditer(body)):
            return float("inf")
    total = sum(_sleep_seconds(m.group(1)) for m in _SLEEP.finditer(cmd))
    return total if total >= LONG_SLEEP_S else None


NO_HANDOVER = ("A swarm run has no handovers: it is one continuous piece of work, ended from outside, and the "
               "board is its record (see *One continuous run* in the swarm-environment canon). Post what the board "
               "does not know yet as a FINDING or WARN, then pick up another line. Nothing was posted.")
_CLI_HANDOVER = re.compile(r"\bkorax\s+post\b.*--type[=\s]+['\"]?HANDOVER\b", re.S)
_KORAX_WRITE_CLI = re.compile(r"\bkorax\s+(post|dm|reply)\b")
KORAX_WRITE_MCP = {"mcp__korax__korax_post", "mcp__korax__korax_dm"}


def make_guard(workspace: Path, mcp_prefixes: tuple[str, ...], deny: list[str] | None = None,
               audit: Path | None = None, shell: bool = False, posting_blocked=None):
    """`posting_blocked()` returns a reason while the agent may not post or
    DM (the opening round's sealed phase), else None."""
    root = Path(os.path.realpath(workspace))
    deny = deny or []

    def record(tool: str, outside: bool, **fields: Any) -> None:
        if audit is None:
            return
        with audit.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "tool": tool, "outside": outside, **fields}) + "\n")

    def deny_out(reason: str) -> dict[str, Any]:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    async def guard(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        name = input_data.get("tool_name", "")
        args = input_data.get("tool_input") or {}
        if name == "mcp__korax__korax_post" and str(args.get("type", "")).upper() == "HANDOVER":
            record(name, False, decision="deny", rule="no handovers")
            return deny_out(NO_HANDOVER)
        if name in SHELL_TOOLS and _CLI_HANDOVER.search(str(args.get(SHELL_TOOLS[name], ""))):
            record(name, False, command=str(args.get(SHELL_TOOLS[name], ""))[:500], decision="deny", rule="no handovers")
            return deny_out(NO_HANDOVER)
        if posting_blocked is not None:
            cmd = str(args.get(SHELL_TOOLS.get(name, ""), "")) if name in SHELL_TOOLS else ""
            if name in KORAX_WRITE_MCP or (cmd and _KORAX_WRITE_CLI.search(cmd)):
                why = posting_blocked()
                if why:
                    record(name, False, command=cmd[:500] or None, decision="deny", rule="sealed round")
                    return deny_out(why)
        if name == "mcp__korax__korax_post" and str(args.get("type", "")).upper() == "WARN":
            payload = args.get("payload")
            text = payload if isinstance(payload, str) else json.dumps(payload or {})
            if not warn_is_scoped(text):
                record(name, False, decision="deny", rule="unscoped WARN")
                return deny_out(WARN_TEMPLATE)
        # The korax MCP server runs OUTSIDE any sandbox, with the harness's
        # filesystem: its two file-touching tools are judged like Read/Write.
        if name == "mcp__korax__korax_attach":
            val = args.get("path")
            if not isinstance(val, str):
                return deny_out("path must be a file path")
            p = _resolve(root, val)
            hit = _denied(p, deny)
            if hit:
                record(name, True, path=str(p), decision="deny", rule=hit)
                return deny_out(f"{p} is a credential location ({hit}); it cannot be attached")
            if not _inside(root, p):
                record(name, True, path=str(p), decision="allow")
        if name == "mcp__korax__korax_fetch" and args.get("out_path") is not None:
            val = args.get("out_path")
            if not isinstance(val, str) or not _inside(root, _resolve(root, val)):
                record(name, True, path=val, decision="deny")
                return deny_out(f"out_path={val!r}: you can only write inside your workspace {root}")
        if name.startswith(mcp_prefixes) or name in PASSIVE_TOOLS:
            return {}
        if name in SHELL_TOOLS:
            if not shell:
                return deny_out(f"{name} is not available in this environment")
            cmd = str(args.get(SHELL_TOOLS[name], ""))
            if name == "Bash" and not args.get("run_in_background"):
                slept = long_sleep(cmd)
                if slept is not None:
                    record(name, False, command=cmd[:500], decision="deny", rule="long sleep",
                           sleep_s=None if slept == float("inf") else slept)
                    return deny_out(LONG_SLEEP)
            warn = _cli_warn_payload(cmd, root)
            if warn is not None and not warn_is_scoped(warn):
                record(name, False, command=cmd[:500], decision="deny", rule="unscoped WARN")
                return deny_out(WARN_TEMPLATE)
            # Heuristic flag only: the sandbox is the enforcement. Absolute or
            # home-relative tokens outside the workspace mark the command —
            # except board namespaces (BOARD_NS_PREFIXES name board paths, not
            # files), `//` comments, and korax CLI calls, which read the board.
            outside: list[str] = []
            if not cmd.lstrip().startswith("korax"):
                toks = [t.strip("'\"(),;") for t in cmd.replace("=", " ").split()]
                outside = [t for t in toks if t.startswith(("/", "~")) and t != "/" and not t.startswith("//")
                           and not t.startswith(BOARD_NS_PREFIXES) and not t.startswith(CLI_SCRATCH)
                           and t != "/dev/null" and not _inside(root, _resolve(root, t))]
            record(name, bool(outside), command=cmd[:2000], paths_outside=outside[:20],
                   background=bool(args.get("run_in_background")))
            return {}
        if name in WRITE_ARGS:
            for key in WRITE_ARGS[name]:
                val = args.get(key)
                if not isinstance(val, str) or not _inside(root, _resolve(root, val)):
                    record(name, True, path=val, decision="deny")
                    return deny_out(f"{key}={val!r}: you can only write inside your workspace {root}")
            return {}
        if name in READ_ARGS:
            for key in READ_ARGS[name]:
                val = args.get(key)
                if val is None and name in ("Glob", "Grep"):
                    continue  # defaults to cwd, the workspace
                if not isinstance(val, str):
                    return deny_out(f"{key} must be a path")
                p = _resolve(root, val)
                hit = _denied(p, deny)
                if hit:
                    record(name, True, path=str(p), decision="deny", rule=hit)
                    return deny_out(f"{p} is a credential location ({hit}); it is not readable here")
                if not _inside(root, p):
                    record(name, True, path=str(p), decision="allow")
            for key in PATTERN_ARGS.get(name, ()):
                val = args.get(key)
                if isinstance(val, str) and (val.startswith(("/", "~")) or ".." in Path(val).parts):
                    # judge the pattern by its literal prefix: the directory it will walk
                    parts = []
                    for part in Path(val).parts:
                        if any(ch in part for ch in "*?[{"):
                            break
                        parts.append(part)
                    base = _resolve(root, str(Path(*parts)) if parts else ".")
                    hit = _denied(base, deny) or _denied(base / "x", deny)
                    if hit:
                        record(name, True, pattern=val, decision="deny", rule=hit)
                        return deny_out(f"{val!r} walks a credential location ({hit})")
                    record(name, True, pattern=val, decision="allow")
            return {}
        return deny_out(f"{name} is not available in this environment")

    return guard
