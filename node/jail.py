#!/usr/bin/env python3
"""jail.py — run one command inside an unprivileged jail on a shared node.

The target node grants no root, no user namespaces (AppArmor restricts
them), and no container runtime, and every agent runs as the SAME Unix user
that owns the operator's other work. So isolation cannot come from uids. It comes from
four layers that an unprivileged process can impose on itself, each covering
what the one before cannot:

1. **systemd user service** (outer stage): MemoryMax, TasksMax, CPUQuota,
   RuntimeMaxSec, and KillMode=control-group — when the command ends or
   times out, every process it spawned dies with it. `nohup … &` does not
   outlive the run.
2. **Landlock** (inner stage): filesystem allowlist — the agent's workspace
   read-write, sibling workspaces and the shared venv read-only, system
   libraries read-only, and nothing else. `/proc` is not listed, so `ps`,
   `pkill` and `/proc/*/environ` see nothing. ABI 4 also denies TCP
   bind/connect outright.
3. **seccomp** (inner stage): what Landlock cannot express on this kernel.
   `socket()` is refused, which closes the escape Landlock leaves open —
   connecting to the user's D-Bus socket and asking systemd for an
   unjailed process — as well as ssh-agent and UDP. io_uring is refused
   because it can create sockets without calling `socket()`. keyctl (the
   user's session keyring), ptrace, bpf and perf are refused.
   **Signals**: Landlock ABI 4 cannot scope them (that is ABI 6, kernel
   6.12), and pids are not secret — getppid() hands one over, and
   kill(pid, 0) probes the whole pid space. So a negative pid (`kill -9 -1`:
   every process this user owns) is refused in-kernel, and every other
   signal-family call is routed by seccomp user-notification to a
   supervisor (the unconfined parent) that allows it only when the target
   is in this unit's own cgroup. Killing the supervisor fails closed.
4. **Scrubbed environment + rlimits**: no inherited secrets or agent
   sockets (only STIGMERGEIA_DATA_ROOT, a path, passes through); per-file size cap; no core dumps; pinned CPU cores; numeric
   thread pools sized to fit under TasksMax.

RESIDUAL (stated, not hidden): the target pid is checked, then the call
proceeds. A pid recycled into this cgroup in that window is a theoretical
race, bounded to processes the job itself owns.

Optional CPU DataLoader compatibility: --torch-ipc copy installs a lazy
Python startup adapter that serializes tensors over existing multiprocessing
pipes. It requires the adjacent python_compat directory; it adds copying and
does not preserve shared-update semantics. Socket denial and /dev/shm rules
are unchanged. See python_compat/swarm_torch_ipc.py for supported tensor types.

Stdlib only: it runs under the node's system python3.

Usage (outer, from the harness over ssh):
  python3 jail.py --ws DIR [--ro DIR ...] [--cores 0-3] [--mem 4G]
                  [--tasks 256] [--timeout 600] [--fsize 4G]
                  [--torch-ipc copy] -- CMD ARGS...
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import errno
import fcntl
import os
import resource
import select
import shlex
import socket
import struct
import subprocess
import sys

# ---------------------------------------------------------------- constants

# x86_64 syscall numbers
NR_LANDLOCK_CREATE_RULESET = 444
NR_LANDLOCK_ADD_RULE = 445
NR_LANDLOCK_RESTRICT_SELF = 446

PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2

# Landlock filesystem rights (ABI 1-4). IOCTL_DEV (1<<15) is ABI 5: excluded.
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13  # ABI 2
FS_TRUNCATE = 1 << 14  # ABI 3

NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1

FS_ALL_ABI4 = (1 << 15) - 1
FS_FILE_ONLY = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE
FS_RO = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
# Read-write inside the workspace — but never device nodes.
FS_RW = FS_ALL_ABI4 & ~(FS_MAKE_CHAR | FS_MAKE_BLOCK)

LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_CREATE_RULESET_VERSION = 1

# System trees every workload needs, read + execute only.
SYSTEM_RO = ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"]
# Single files/dirs under /proc and /sys that numeric libraries probe for
# topology. Listed individually: /proc as a whole would re-open `ps`.
PROBE_RO = [
    "/proc/cpuinfo", "/proc/meminfo", "/proc/stat", "/proc/loadavg",
    "/sys/devices/system/cpu", "/sys/fs/cgroup",
]
# Character devices a process legitimately writes to.
DEV_RW = ["/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom", "/dev/tty"]
# /dev/shm is NEVER granted: on a shared node it holds other jobs' live
# state, and a Landlock rule on it covers every segment. This preload
# serves shm_open/sem_open from <ws>/.shm instead (see shmredir.c), which is
# what multiprocessing locks and DataLoader workers need.
PYTHON_COMPAT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "python_compat")
SHM_SHIM = os.path.join(os.path.dirname(os.path.realpath(__file__)), "libshmredir.so")
# The one variable carried into the scrubbed environment: where task data lives
# on this node (a task's gate resolves its corpus under it). It names a path,
# not a secret; granting read access to that path is still the caller's --ro.
DATA_ROOT_ENV = "STIGMERGEIA_DATA_ROOT"

# seccomp
AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000
RET_ALLOW = 0x7FFF0000
RET_USER_NOTIF = 0x7FC00000
RET_KILL_PROCESS = 0x80000000


def RET_ERRNO(e: int) -> int:
    return 0x00050000 | (e & 0xFFFF)


EPERM, ESRCH, EACCES = 1, 3, 13

NR_SECCOMP = 317
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_FILTER_FLAG_NEW_LISTENER = 1 << 3
SECCOMP_USER_NOTIF_FLAG_CONTINUE = 1
# _IOWR('!', 0, struct seccomp_notif[80]), _IOWR('!', 1, seccomp_notif_resp[24]),
# _IOW('!', 2, __u64)
IOCTL_NOTIF_RECV = 0xC0502100
IOCTL_NOTIF_SEND = 0xC0182101
IOCTL_NOTIF_ID_VALID = 0x40082102
NOTIF_FMT = "<QIIiIQ6Q"  # id, pid, flags, nr, arch, ip, args[6]
RESP_FMT = "<QqiI"  # id, val, error, flags

NR = {
    "kill": 62, "rt_sigqueueinfo": 129,
    "tkill": 200, "tgkill": 234, "rt_tgsigqueueinfo": 297, "pidfd_open": 434,
    "socket": 41,
    "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311,
    "add_key": 248, "request_key": 249, "keyctl": 250,
    "perf_event_open": 298, "bpf": 321,
    "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
    "unshare": 272, "setns": 308, "mount": 165, "pivot_root": 155,
}
# Refused unconditionally: (syscall, errno)
DENY = [
    ("socket", EACCES),
    ("ptrace", EPERM), ("process_vm_readv", EPERM), ("process_vm_writev", EPERM),
    ("add_key", EPERM), ("request_key", EPERM), ("keyctl", EPERM),
    ("perf_event_open", EPERM), ("bpf", EPERM),
    ("io_uring_setup", EPERM), ("io_uring_enter", EPERM), ("io_uring_register", EPERM),
    ("unshare", EPERM), ("setns", EPERM), ("mount", EPERM), ("pivot_root", EPERM),
]
# Signal-family calls: a negative pid (a whole process group, or -1 = every
# process this user owns) is refused in-kernel; pid 0 (own group) is allowed
# in-kernel; any other target is handed to the supervisor, which allows it
# only inside the jail's own cgroup. getppid() and kill(pid, 0) probing make
# pids trivially discoverable, so "they would have to guess" is no defence.
SIGNAL_NOTIFY = ["kill", "rt_sigqueueinfo", "tkill", "tgkill", "rt_tgsigqueueinfo", "pidfd_open"]

# ------------------------------------------------------------ libc plumbing

_libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.prctl.restype = ctypes.c_int


class JailError(RuntimeError):
    pass


def _check(ret: int, what: str) -> int:
    if ret < 0:
        err = ctypes.get_errno()
        raise JailError(f"{what}: {os.strerror(err)} (errno {err})")
    return ret


# ----------------------------------------------------------------- landlock


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def landlock_abi() -> int:
    return _libc.syscall(NR_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                         ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))


def apply_landlock(rw: list[str], ro: list[str], dev_rw: list[str]) -> None:
    abi = landlock_abi()
    if abi < 4:
        raise JailError(f"Landlock ABI {abi} < 4: refusing to run unjailed")
    attr = _RulesetAttr(FS_ALL_ABI4, NET_BIND_TCP | NET_CONNECT_TCP)
    rfd = _check(_libc.syscall(NR_LANDLOCK_CREATE_RULESET, ctypes.byref(attr),
                               ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0)),
                 "landlock_create_ruleset")

    def add(path: str, access: int) -> None:
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except FileNotFoundError:
            return  # absent on this host (e.g. /lib64): nothing to allow
        try:
            if not os.path.isdir(path):
                access &= FS_FILE_ONLY
            rule = _PathBeneath(access, fd)
            _check(_libc.syscall(NR_LANDLOCK_ADD_RULE, ctypes.c_int(rfd),
                                 ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
                                 ctypes.byref(rule), ctypes.c_uint32(0)),
                   f"landlock_add_rule {path}")
        finally:
            os.close(fd)

    for p in ro:
        add(p, FS_RO)
    for p in dev_rw:
        add(p, FS_READ_FILE | FS_WRITE_FILE)
    for p in rw:
        add(p, FS_RW)
    _check(_libc.syscall(NR_LANDLOCK_RESTRICT_SELF, ctypes.c_int(rfd), ctypes.c_uint32(0)),
           "landlock_restrict_self")
    os.close(rfd)


# ------------------------------------------------------------------ seccomp

BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_JGE_K = 0x35
BPF_RET_K = 0x06

OFF_NR, OFF_ARCH, OFF_ARG0 = 0, 4, 16


def _build_filter() -> list[tuple[int, int, int, int]]:
    """A flat BPF program. Jumps are relative, so it is built back to front
    in blocks: each block is `jeq NR -> body, else -> next block`."""
    prog: list[tuple[int, int, int, int]] = []
    ins = lambda code, jt, jf, k: prog.append((code, jt, jf, k))  # noqa: E731

    ins(BPF_LD_W_ABS, 0, 0, OFF_ARCH)
    ins(BPF_JEQ_K, 1, 0, AUDIT_ARCH_X86_64)
    ins(BPF_RET_K, 0, 0, RET_KILL_PROCESS)  # foreign arch: no exceptions
    ins(BPF_LD_W_ABS, 0, 0, OFF_NR)
    ins(BPF_JGE_K, 0, 1, X32_SYSCALL_BIT)
    ins(BPF_RET_K, 0, 0, RET_ERRNO(EPERM))  # x32 aliases bypass nr matches

    for name, err in DENY:
        ins(BPF_JEQ_K, 0, 1, NR[name])
        ins(BPF_RET_K, 0, 0, RET_ERRNO(err))

    for name in SIGNAL_NOTIFY:
        # nr == name ? (arg0 negative ? deny : arg0 == 0 ? allow : notify) : next
        ins(BPF_JEQ_K, 0, 6, NR[name])
        ins(BPF_LD_W_ABS, 0, 0, OFF_ARG0)
        ins(BPF_JGE_K, 0, 1, 0x80000000)
        ins(BPF_RET_K, 0, 0, RET_ERRNO(EPERM))
        ins(BPF_JEQ_K, 0, 1, 0)
        ins(BPF_RET_K, 0, 0, RET_ALLOW)
        ins(BPF_RET_K, 0, 0, RET_USER_NOTIF)
        # the block clobbered A; reload nr for the next block
        ins(BPF_LD_W_ABS, 0, 0, OFF_NR)

    ins(BPF_RET_K, 0, 0, RET_ALLOW)
    return prog


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def apply_seccomp() -> int:
    """Install the filter; return the user-notification listener fd."""
    prog = _build_filter()
    raw = b"".join(struct.pack("HBBI", *i) for i in prog)
    buf = ctypes.create_string_buffer(raw, len(raw))
    fprog = _SockFprog(len(prog), ctypes.cast(buf, ctypes.c_void_p))
    return _check(_libc.syscall(NR_SECCOMP, ctypes.c_uint(SECCOMP_SET_MODE_FILTER),
                                ctypes.c_uint(SECCOMP_FILTER_FLAG_NEW_LISTENER),
                                ctypes.byref(fprog)),
                  "seccomp(SET_MODE_FILTER, NEW_LISTENER)")


# --------------------------------------------------------------- supervisor


def _cgroup_of(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            return f.read().strip()
    except OSError:
        return None


def supervise(listener: int, child: int) -> int:
    """Answer the jail's signal syscalls until its main process exits.

    Allowed iff the target lives in this unit's own cgroup (the job's own
    process tree) and is not the supervisor itself. Everything else gets
    EPERM (or ESRCH for a pid that does not exist — the same answer an
    unconfined kill would give, so the probe learns nothing new about
    processes outside). If the supervisor dies, pending and future calls
    fail in-kernel (ENOSYS): fail-closed, and systemd then stops the unit
    because its main process is gone.
    """
    own_cgroup = _cgroup_of(os.getpid())
    me = os.getpid()
    child_fd = os.pidfd_open(child)
    poller = select.poll()
    poller.register(listener, select.POLLIN)
    poller.register(child_fd, select.POLLIN)
    while True:
        for fd, ev in poller.poll():
            if fd == child_fd:
                _, status = os.waitpid(child, 0)
                os.close(listener)
                return os.waitstatus_to_exitcode(status) % 256
            if ev & (select.POLLHUP | select.POLLERR):
                poller.unregister(listener)
                continue
            buf = bytearray(struct.calcsize(NOTIF_FMT))
            try:
                fcntl.ioctl(listener, IOCTL_NOTIF_RECV, buf, True)
            except OSError as e:
                if e.errno in (errno.ENOENT, errno.EINTR):
                    continue  # caller died before we read it, or a signal
                raise
            nid, _caller, _fl, _nr, _arch, _ip, *args = struct.unpack(NOTIF_FMT, buf)
            target = ctypes.c_int32(args[0] & 0xFFFFFFFF).value
            cg = _cgroup_of(target)
            if cg is None:
                error, flags = -ESRCH, 0
            elif cg == own_cgroup and target != me:
                error, flags = 0, SECCOMP_USER_NOTIF_FLAG_CONTINUE
            else:
                error, flags = -EPERM, 0
            try:
                fcntl.ioctl(listener, IOCTL_NOTIF_ID_VALID, struct.pack("<Q", nid))
                fcntl.ioctl(listener, IOCTL_NOTIF_SEND,
                            bytearray(struct.pack(RESP_FMT, nid, 0, error, flags)), True)
            except OSError as e:
                if e.errno != errno.ENOENT:  # caller gone meanwhile: nothing to answer
                    raise


# ----------------------------------------------------------------- helpers


def parse_size(s: str) -> int:
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    s = s.strip().upper()
    return int(float(s[:-1]) * units[s[-1]]) if s[-1] in units else int(s)


def parse_cores(s: str) -> set[int]:
    out: set[int] = set()
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


def thread_budget(ncores: int, tasks: str) -> int:
    """Threads per numeric library. It must fit under TasksMax: OpenBLAS
    sizes its pool from the thread env var, and a pool that exceeds the
    task cap fails pthread_create and hangs `import numpy`."""
    try:
        cap = max(1, int(tasks) // 4)
    except ValueError:  # TasksMax=infinity or a percentage
        cap = ncores
    return max(1, min(ncores, cap))


def jail_env(ws: str, venv: str | None, ncores: int) -> dict[str, str]:
    path = [f"{venv}/bin"] if venv else []
    path += ["/usr/local/bin", "/usr/bin", "/bin"]
    env = {
        "PATH": ":".join(path),
        "HOME": ws,
        # /tmp is not writable in the jail (Landlock grants only the workspace).
        # Every temp-dir convention points inside it, at .tmp/ (not synced).
        "TMPDIR": f"{ws}/.tmp",
        "TMP": f"{ws}/.tmp",
        "TEMP": f"{ws}/.tmp",
        "XDG_CACHE_HOME": f"{ws}/.tmp/cache",
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",  # a system Python with ENABLE_USER_SITE=True would read ~/.local
        "PYTHONDONTWRITEBYTECODE": "1",  # the venv is read-only
        "MPLCONFIGDIR": f"{ws}/.tmp/mpl",
        "OMP_NUM_THREADS": str(ncores),
        "MKL_NUM_THREADS": str(ncores),
        "OPENBLAS_NUM_THREADS": str(ncores),
        "SWARM_JAILED": "1",
        "SWARM_SHM_DIR": f"{ws}/.shm",
    }
    if os.path.isfile(SHM_SHIM):
        env["LD_PRELOAD"] = SHM_SHIM
    if venv:
        env["VIRTUAL_ENV"] = venv
    if os.environ.get(DATA_ROOT_ENV):
        env[DATA_ROOT_ENV] = os.environ[DATA_ROOT_ENV]
    return env


# ------------------------------------------------------------------- stages


def inner(a: argparse.Namespace) -> int:
    """Runs as the systemd service's main process.

    Forks: the CHILD locks itself down (Landlock + seccomp) and execs the
    command; the PARENT stays unconfined as the signal supervisor and exits
    with the child's status. The listener fd crosses over a socketpair
    made before the filter exists (the filter refuses socket())."""
    ws = os.path.realpath(a.ws)
    os.makedirs(f"{ws}/.tmp", exist_ok=True)
    os.makedirs(f"{ws}/.shm", mode=0o700, exist_ok=True)
    if not os.path.isfile(SHM_SHIM):
        print(f"jail: {SHM_SHIM} not built; multiprocessing locks will fail "
              "(gcc -O2 -shared -fPIC -o libshmredir.so shmredir.c -pthread)",
              file=sys.stderr)
    cores = parse_cores(a.cores) if a.cores else os.sched_getaffinity(0)
    os.sched_setaffinity(0, cores)
    env = jail_env(ws, a.venv, a.threads or thread_budget(len(cores), "infinity"))
    if a.torch_ipc == "copy":
        if not os.path.isfile(os.path.join(PYTHON_COMPAT, "sitecustomize.py")):
            raise JailError("--torch-ipc copy requires the adjacent python_compat directory")
        env["PYTHONPATH"] = PYTHON_COMPAT
        env["SWARM_TORCH_IPC"] = "copy"
    ro = SYSTEM_RO + PROBE_RO + [os.path.realpath(p) for p in a.ro]
    if a.torch_ipc == "copy":
        ro.append(PYTHON_COMPAT)
    if a.venv:
        ro.append(os.path.realpath(a.venv))
    if os.path.isfile(SHM_SHIM):
        ro.append(SHM_SHIM)  # a file rule: read + execute on exactly this .so
    fsize = parse_size(a.fsize)

    parent_end, child_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    child = os.fork()
    if child == 0:
        code = 126
        try:
            parent_end.close()
            resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            _check(_libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(NO_NEW_PRIVS)")
            apply_landlock(rw=[ws], ro=ro, dev_rw=DEV_RW)
            listener = apply_seccomp()
            socket.send_fds(child_end, [b"L"], [listener])
            os.close(listener)
            child_end.close()
            os.chdir(ws)
            code = 127
            os.execvpe(a.cmd[0], a.cmd, env)
        except BaseException as e:  # never fall through into the parent's code
            print(f"jail: child setup failed: {e}", file=sys.stderr, flush=True)
        os._exit(code)

    child_end.close()
    _msg, fds, _flags, _addr = socket.recv_fds(parent_end, 1, 1)
    parent_end.close()
    if not fds:  # the child died before installing the filter
        _, status = os.waitpid(child, 0)
        return os.waitstatus_to_exitcode(status) % 256 or 126
    return supervise(fds[0], child)


def outer(a: argparse.Namespace) -> int:
    """Wrap the inner stage in a transient systemd user service."""
    ws = os.path.realpath(a.ws)
    if not os.path.isdir(ws):
        print(f"jail: workspace {ws} does not exist", file=sys.stderr)
        return 2
    unit_props = [
        f"MemoryMax={a.mem}", "MemorySwapMax=0",
        f"TasksMax={a.tasks}",
        f"RuntimeMaxSec={a.timeout}",
        "KillMode=control-group",
    ]
    if a.cores:
        unit_props.append(f"CPUQuota={len(parse_cores(a.cores)) * 100}%")
    ncores = len(parse_cores(a.cores)) if a.cores else len(os.sched_getaffinity(0))
    inner_cmd = [sys.executable, os.path.realpath(__file__), "--inner", "--ws", ws,
                 "--fsize", a.fsize, "--torch-ipc", a.torch_ipc, "--threads", str(thread_budget(ncores, a.tasks))]
    if a.cores:
        inner_cmd += ["--cores", a.cores]
    if a.venv:
        inner_cmd += ["--venv", a.venv]
    for p in a.ro:
        inner_cmd += ["--ro", p]
    inner_cmd += ["--", *a.cmd]
    systemd = ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
               f"--working-directory={ws}"]
    for p in unit_props:
        systemd += ["-p", p]
    if a.unit:
        systemd += [f"--unit={a.unit}"]
    if os.environ.get(DATA_ROOT_ENV):  # a transient unit does not inherit the caller's environment
        systemd += [f"--setenv={DATA_ROOT_ENV}={os.environ[DATA_ROOT_ENV]}"]
    if a.dry_run:
        print(shlex.join(systemd + inner_cmd))
        return 0
    return subprocess.run(systemd + inner_cmd, stdin=subprocess.DEVNULL).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ws", required=True, help="workspace: the only writable tree")
    ap.add_argument("--ro", action="append", default=[], help="extra read-only tree (repeatable)")
    ap.add_argument("--venv", default=None, help="venv to expose read-only and put on PATH")
    ap.add_argument("--torch-ipc", choices=("default", "copy"), default="default",
                    help="copy: CPU tensors use pipe serialization; extra copies, no shared-update semantics")
    ap.add_argument("--cores", default=None, help="pin to cores, e.g. 0-3 or 4,5")
    ap.add_argument("--mem", default="4G")
    ap.add_argument("--tasks", default="256")
    ap.add_argument("--timeout", default="600", help="seconds before the unit is killed")
    ap.add_argument("--fsize", default="4G", help="largest single file the job may write")
    ap.add_argument("--unit", default=None, help="systemd unit name (for status/stop)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--threads", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if a.cmd and a.cmd[0] == "--":
        a.cmd = a.cmd[1:]
    if not a.cmd:
        ap.error("no command given (put it after --)")
    try:
        return inner(a) if a.inner else outer(a)
    except JailError as e:
        print(f"jail: {e}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    sys.exit(main())
