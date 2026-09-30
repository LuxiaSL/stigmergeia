"""The Codex backend's shell sandbox, built by the harness (not by Codex).

Every shell command an agent runs goes through `bwrap`:

- **Filesystem:** the whole host read-only (`--ro-bind / /`), the agent's
  workspace read-write, a private `/tmp`, and every credential location
  masked (a directory becomes an empty tmpfs, a file becomes /dev/null).
  `/run` is masked too: it holds the user's D-Bus and systemd sockets,
  which would otherwise let a command start processes outside the sandbox.
- **Network:** a fresh network namespace with only loopback. Inside it a
  `socat` listens on 127.0.0.1:<board port> and forwards to a Unix socket
  bound in from the harness, which forwards to the board and nowhere else.
  So `KORAX_URL=http://127.0.0.1:<port>` works unchanged, and nothing else
  is reachable: no DNS, no internet, no other local port.
- **Processes:** own PID/IPC/UTS/user namespaces, `--die-with-parent`,
  `--new-session`; when the command exits the namespace (and anything it
  left running) is torn down.
- **CPU/memory:** each sandbox runs in a transient systemd scope inside the
  agent's own slice (`swarm-<run>-<agent>.slice`, CPUQuota/MemoryMax set on
  the slice), pinned with `taskset` to the agent's core, the same slice its
  app-server runs in: one agent's local work cannot starve the others.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
from pathlib import Path

INNER_SOCK = "/run/swarm/board.sock"


def mask_args(deny_paths: list[str], keep: list[Path]) -> list[str]:
    """bwrap arguments hiding every existing deny path. A path under an
    already-masked directory is skipped (the sandbox already hides it),
    and so is any path that CONTAINS a kept path (the workspace): masking a
    parent of the workspace would hide the workspace itself."""
    out: list[str] = []
    masked_dirs: list[str] = []
    keep_s = [os.path.realpath(k) for k in keep]
    seen: set[str] = set()
    for raw in sorted(deny_paths):
        rp = os.path.realpath(raw)
        if rp in seen or not os.path.lexists(rp):
            continue
        seen.add(rp)
        if any(rp == d or rp.startswith(d + "/") for d in masked_dirs):
            continue
        if any(k == rp or k.startswith(rp + "/") for k in keep_s):
            continue
        if os.path.isdir(rp):
            out += ["--tmpfs", rp]
            masked_dirs.append(rp)
        else:
            out += ["--ro-bind", "/dev/null", rp]
    return out


def bwrap_argv(workspace: Path, deny_paths: list[str], env: dict[str, str], command: str,
               board_sock: Path | None = None, board_port: int | None = None,
               bwrap: str = "bwrap") -> list[str]:
    """The full bwrap command line for one shell command."""
    ws = os.path.realpath(workspace)
    argv = [bwrap, "--die-with-parent", "--new-session", "--unshare-all",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/var/tmp",
            "--bind", ws, ws]
    argv += mask_args(deny_paths, [Path(ws)])
    if board_sock is not None:
        argv += ["--dir", os.path.dirname(INNER_SOCK), "--bind", str(board_sock), INNER_SOCK]
    argv += ["--chdir", ws, "--clearenv"]
    for k, v in sorted(env.items()):
        argv += ["--setenv", k, v]
    if board_sock is not None and board_port is not None:
        hexport = f"{board_port:04X}"
        # the forwarder, then wait (bounded) until it listens, then the command
        script = (f"socat TCP-LISTEN:{board_port},bind=127.0.0.1,fork,reuseaddr UNIX-CONNECT:{INNER_SOCK} "
                  f"</dev/null >/dev/null 2>&1 & "
                  f"i=0; while ! grep -q ':{hexport} 00000000:0000 0A' /proc/net/tcp 2>/dev/null; do "
                  f"i=$((i+1)); [ $i -gt 300 ] && break; sleep 0.01; done; "
                  f'exec bash -c "$0"')
        argv += ["--", "sh", "-c", script, command]
    else:
        argv += ["--", "bash", "-c", command]
    return argv


def scoped(argv: list[str], slice_name: str | None, cpus: str | None) -> list[str]:
    """Run argv in a transient scope in the agent's slice, pinned to its core(s)."""
    out: list[str] = []
    if slice_name:
        out += ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--slice={slice_name}", "--"]
    if cpus:
        out += ["taskset", "-c", cpus]
    return out + argv


def configure_slice(slice_name: str, cpu_quota: str | None, mem_max: str | None) -> None:
    """Set the agent slice's limits (runtime only: gone at reboot or stop)."""
    props = []
    if cpu_quota:
        props.append(f"CPUQuota={cpu_quota}")
    if mem_max:
        props.append(f"MemoryMax={mem_max}")
    if not props:
        return
    r = subprocess.run(["systemctl", "--user", "set-property", "--runtime", slice_name, *props],
                       capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"could not configure {slice_name}: {r.stderr.strip()[-300:]}")


def stop_slice(slice_name: str) -> None:
    """Stop the agent's slice: kills anything still running in it (by unit
    name, never by process text)."""
    try:
        subprocess.run(["systemctl", "--user", "stop", slice_name], capture_output=True, timeout=30)
    except Exception:
        pass


def board_sock_path(run_name: str, agent: str) -> Path:
    """A short path for the agent's board socket (AF_UNIX paths max out at
    108 bytes): the user's runtime dir, which every sandbox masks anyway."""
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/swarm-{os.getuid()}") / "swarm"
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    return base / f"{run_name}-{agent}.sock"


def slice_name_for(run_name: str, agent: str) -> str:
    """`swarm-<run>-<agent>.slice`. systemd nests slices on '-', so every
    agent of a run also sits under `swarm-<run>.slice`."""
    return f"swarm-{run_name}-{agent}.slice"


class BoardForwarder:
    """A Unix socket (bound into each sandbox) forwarding to the board's
    TCP host:port, and only there."""

    def __init__(self, sock_path: Path, host: str, port: int):
        self.sock_path, self.host, self.port = sock_path, host, port
        self.server: asyncio.AbstractServer | None = None
        self._conns: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sock_path.exists() or self.sock_path.is_symlink():
            self.sock_path.unlink()
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.sock_path))
        os.chmod(self.sock_path, 0o600)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._conns.add(task)
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10)
        except Exception:
            writer.close()
            return

        async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                while data := await r.read(65536):
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                try:
                    w.close()
                except Exception:
                    pass

        try:
            await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))
        finally:
            if task:
                self._conns.discard(task)

    async def close(self) -> None:
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 5)
            except Exception:
                pass
        for t in list(self._conns):
            t.cancel()
        try:
            self.sock_path.unlink()
        except OSError:
            pass


def preflight_tools() -> list[str]:
    """What the Codex backend's sandbox needs on this machine."""
    return [t for t in ("bwrap", "socat", "systemd-run", "taskset") if shutil.which(t) is None]


def quote(argv: list[str]) -> str:
    return shlex.join(argv)
