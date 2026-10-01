"""The lab: in-process MCP tools that run agent code on the node.

The agent never holds an ssh key, the held-out secret, or a shell on this
machine. It edits files in its local workspace; these tools sync that
workspace to its node workspace and run commands inside the jail there.

- `run(command)`   : sync, then run a shell command in the jail. Output
                     comes back head+tail; the FULL log is saved in the
                     workspace and its path is named — nothing is dropped.
                     A run can go to the BACKGROUND (`background: true`, a
                     timeout_s above `run_background_after_s`, or a foreground
                     run still going after that long): the call returns a job
                     id and the output is delivered like a background submit's.
                     A blocking `run` (a search or a grid, up to 30 min) is
                     otherwise the lab's main cost to an agent.
- `score(policy)`  : the task gate on the public training seeds, in the jail.
- `submit(policy)` : the gate on HELD-OUT seeds. The gate process holds the
                     secret and runs unjailed on the node; the policy it
                     evaluates runs jailed, from a frozen snapshot. Rate
                     limited per agent. The verified result is posted to the
                     board by the gate identity — the only author whose
                     numbers in the gate namespace mean anything.
                     Before the held-out batch, the policy is TIMED on a few
                     training episodes in the jail; if one batch is estimated
                     to take longer than `background_after_s`, the submission
                     runs in the background and `submit` returns at once with
                     a job id (one synchronous submit of a slow policy can
                     block an agent for a large part of a run). The result
                     reaches the agent on its next lab call, in `jobs`, and
                     in its next continue message.
- `jobs()`         : the agent's background jobs (runs, held-out submissions,
                     confirmation batches) and their results.
- `wait()`         : block until one of the agent's background jobs finishes
                     (its result, as `jobs` gives it), or board news for the
                     agent arrives (what the pulse shows: DMs, mentions,
                     replies, a new gate record), or `wait_cap_s` passes with
                     neither. It replaces a shell `sleep` on the agent's own
                     job: a fixed sleep overshoots the job, and no pulse
                     reaches an agent inside one shell call.

Cores: a held-out batch (a submission or a confirmation) has the agent's
cores to itself, because the policy's time budget is wall-clock: run, score
and submit are refused while one runs. Up to `max_background_runs` background
RUNs at a time (with one, an agent whose run is going has nothing else to
launch, and sleeps on it); they SHARE the cores (a split fixed at start
would halve a lone run, the common case, for nothing, and a running job's cores
cannot be changed). Beside them a light foreground run or score (<=
`shared_run_timeout_s`) may share too. A submit made while runs are going is
QUEUED: its snapshot is frozen at once, and its batch starts, with the cores to
itself, when the runs (and any foreground call) have finished; new background
runs wait while it is queued, so it cannot starve. A queued submission is not
timed first (it is already in the background, and the probe would only delay
it), so its status carries no estimate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shlex
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import board
from .config import NodeConfig, RunConfig

if TYPE_CHECKING:
    from .pulse import Pulse

HEAD, TAIL = 2_000, 10_000
PROPOSAL_PART_MIN = 150
SYNC_EXCLUDES = [".runs/", ".tmp/", ".shm/", "__pycache__/"]
PULL_MAX = "5M"
PROBE_CAP_S = 60  # the timing probe's episodes run at most this long: a slower policy is 'slow' already
WAIT_BOARD_POLL_S = 5.0  # inside `wait`, how often the board is asked for news addressed to the agent (never shown)


@dataclass
class AgentSlot:
    name: str
    index: int
    workspace: Path  # local
    private: Path  # local, never visible to the agent
    remote_ws: str
    cores: str
    last_submit: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # guards syncs and held-out batches
    fg_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one FOREGROUND lab call at a time
    jobs: dict[str, Job] = field(default_factory=dict)
    auto_confirms: list[float] = field(default_factory=list)  # when auto-confirmation batches started


JOB_PREFIX = {"submit": "s", "confirm": "c", "run": "r"}


@dataclass
class Job:
    """Lab work running in the background: a held-out submission, an
    auto-confirmation batch, or a run (or score)."""

    id: str
    policy: str  # the policy file; for a run, its label (the command, shortened, or "score <file>")
    sha: str = ""
    snap: str = ""
    started: float = 0.0
    estimate_s: float | None = None
    kind: str = "submit"  # submit | confirm | run
    limit_s: int | None = None  # a run's wall cap
    ends: dict | None = None  # confirm: the first batch's episode ends, pooled with the second's
    queued: bool = False  # submit: waiting for the agent's background runs to free the cores
    task: asyncio.Task | None = None
    result: str | None = None  # the text the synchronous call would have returned
    is_error: bool = False
    finished: float | None = None
    delivered: bool = False

    @property
    def held_out(self) -> bool:
        return self.kind in ("submit", "confirm")

    def label(self) -> str:
        if self.kind == "run":
            return f"background run {self.id} ({self.policy})"
        if self.kind == "confirm":
            return f"confirmation batch {self.id} ({self.policy})"
        return f"held-out job {self.id} ({self.policy})"

    def status(self, now: float) -> str:
        est = f"; estimated ~{_mins(self.estimate_s)} per batch" if self.estimate_s else ""
        if self.kind == "run":
            est = f"; stopped at {self.limit_s} s if still running" if self.limit_s else ""
        what = {"run": "run", "confirm": "confirmation of"}.get(self.kind, "submit")
        if self.queued and self.result is None:
            return (f"{self.id} {what} {self.policy}: queued; it starts when your background runs finish, and then "
                    "has your cores to itself")
        if self.result is None:
            return f"{self.id} {what} {self.policy}: running for {_mins(now - self.started)}{est}"
        return f"{self.id} {what} {self.policy}: finished after {_mins((self.finished or now) - self.started)}"


SubmitJob = Job  # the name used where a job is known to be a held-out submission


def _short(command: str, n: int = 80) -> str:
    one = " ".join(command.split())
    return f"`{one}`" if len(one) <= n else f"`{one[:n - 3]}...`"


def _mins(s: float | None) -> str:
    if s is None:
        return "?"
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.1f} min"


def cores_for(node: NodeConfig, index: int) -> str:
    start = node.first_core + index * node.cores_per_agent
    return f"{start}-{start + node.cores_per_agent - 1}"


def _text(s: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": s}], "is_error": is_error}


def _alltext(res: dict[str, Any]) -> str:
    return "".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict))


def _prepend(res: dict[str, Any], note: str) -> dict[str, Any]:
    content = list(res.get("content") or [])
    if content and isinstance(content[0], dict) and "text" in content[0]:
        content[0] = {**content[0], "text": note + content[0]["text"]}
    else:
        content.insert(0, {"type": "text", "text": note})
    return {**res, "content": content}


async def _exec(argv: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out, _ = await proc.communicate()
        return 124, (out or b"").decode(errors="replace") + f"\n[harness: killed after {timeout:.0f}s]"
    return proc.returncode or 0, (out or b"").decode(errors="replace")


def _clip(log: str, saved: Path, root: Path) -> str:
    rel = saved.relative_to(root)
    if len(log) <= HEAD + TAIL:
        return log + f"\n[full log: {rel}]"
    cut = len(log) - HEAD - TAIL
    return f"{log[:HEAD]}\n[... {cut} chars omitted here; full log: {rel} ...]\n{log[-TAIL:]}"


class Lab:
    def __init__(self, cfg: RunConfig, gate_cred: board.Credential):
        if cfg.node is None:
            raise ValueError("the lab needs a node section in the config")
        self.cfg = cfg
        self.node: NodeConfig = cfg.node
        self.gate_cred = gate_cred
        # The best CONFIRMED held-out score, shared by every agent's submit:
        # a batch that beats it earns a confirmation batch before it counts.
        self.record: float | None = None
        self.record_lock = asyncio.Lock()
        # Every batch ever scored for a policy digest: resubmitting the same
        # bytes adds evidence to one pooled score, never a fresh lottery ticket.
        self.batches_by_sha: dict[str, list[dict]] = {}
        # Each agent's best CONFIRMED score: a single batch that clearly beats
        # it earns a background confirmation batch even when it is no record
        # candidate: otherwise an agent's real improvements below the record
        # are never confirmed.
        self.agent_best: dict[str, float] = {}
        self._board_loaded = False  # distinct from "a record exists": an empty board is loaded too

    # -- node plumbing
    def _jail_prefix(self, slot: AgentSlot, ws: str, timeout: int) -> str:
        n = self.node
        if not n.jail_template.strip():
            # No jail (config allows it only for scripted agents): the jail's own
            # working directory and wall cap are all that is kept.
            return f"cd {shlex.quote(ws)} && timeout {int(timeout)} "
        return n.jail_template.format(
            python=n.remote_python, harness_dir=n.harness_dir, ws=shlex.quote(ws),
            siblings=shlex.quote(f"{n.run_root}/agents"),
            task_dir=shlex.quote(f"{n.harness_dir}/tasks/{self.cfg.task_dir.name}"),
            venv=shlex.quote(n.venv), cores=slot.cores, mem=n.mem, tasks=n.tasks, timeout=timeout,
            data_root=shlex.quote(n.data_root or ""))

    async def _push(self, slot: AgentSlot) -> tuple[int, str]:
        ex = [a for e in SYNC_EXCLUDES for a in ("--exclude", e)]
        return await _exec(["rsync", "-a", *ex, f"{slot.workspace}/", self.node.remote(f"{slot.remote_ws}/")], 120)

    async def _pull(self, slot: AgentSlot, update: bool = False) -> tuple[int, str]:
        """update: skip files that are newer locally. A BACKGROUND run pulls
        back long after its push, and the agent may have edited files since:
        without it, the pull would overwrite those edits with the old copy."""
        ex = [a for e in SYNC_EXCLUDES for a in ("--exclude", e)]
        return await _exec(["rsync", "-a", *(["--update"] if update else []), f"--max-size={PULL_MAX}", *ex,
                            self.node.remote(f"{slot.remote_ws}/"), f"{slot.workspace}/"], 120)

    def _save(self, slot: AgentSlot, kind: str, text: str) -> Path:
        d = slot.workspace / ".runs"
        d.mkdir(exist_ok=True)
        p = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}.log"
        p.write_text(text)
        return p

    def busy_job(self, slot: AgentSlot) -> Job | None:
        """The agent's held-out batch (submission or confirmation) RUNNING in the background, if any: it has
        the cores to itself. A queued submission is not running yet."""
        return next((j for j in slot.jobs.values() if j.result is None and j.held_out and not j.queued), None)

    def queued_job(self, slot: AgentSlot) -> Job | None:
        """The agent's held-out submission waiting for its background runs to finish, if any."""
        return next((j for j in slot.jobs.values() if j.result is None and j.queued), None)

    def bg_runs(self, slot: AgentSlot) -> list[Job]:
        """The agent's background runs still going, oldest first."""
        return [j for j in slot.jobs.values() if j.result is None and j.kind == "run"]

    def bg_run(self, slot: AgentSlot) -> Job | None:
        """The agent's oldest background run still going, if any."""
        runs = self.bg_runs(slot)
        return runs[0] if runs else None

    def _new_job(self, slot: AgentSlot, kind: str, **kw: Any) -> Job:
        k = sum(1 for j in slot.jobs.values() if j.kind == kind) + 1
        job = Job(id=f"{JOB_PREFIX[kind]}{k}", kind=kind, started=kw.pop("started", time.time()), **kw)
        slot.jobs[job.id] = job
        return job

    def _busy_text(self, job: Job) -> dict[str, Any]:
        if job.queued:
            return _text(
                f"Nothing was submitted: your held-out submission {job.id} ({job.policy}) is queued for your cores "
                "and starts as soon as your background runs finish; the lab scores one held-out submission per "
                "agent at a time. Its result comes to you on your next lab call after it finishes, in `jobs`, and "
                "from the lab's `wait`; submit again then.", True)
        what = ("a confirmation batch for your submission" if job.kind == "confirm"
                else "your held-out submission")
        return _text(
            f"Your node cores are running {what} in the background ({job.status(time.time())}). "
            "run, score and submit use the same cores, and sharing them would slow the policy being scored (its "
            "time budget is wall-clock), so they wait for it: nothing was run. Meanwhile you can read the board, "
            "read and build on others' code, write and test in your local shell, and post. The result is posted "
            "by the gate and shown to you on your next lab call; `jobs` shows its status.", True)

    def _joblog(self, slot: AgentSlot, **row: Any) -> None:
        """Every held-out submission's timing, in the agent's PRIVATE dir (for analysis, never shown)."""
        try:
            with open(slot.private / "lab-jobs.jsonl", "a") as f:
                f.write(json.dumps({"t": round(time.time(), 3), **row}) + "\n")
        except OSError:
            pass

    def take_notes(self, slot: AgentSlot, via: str) -> str:
        """Results of background jobs that finished and were not yet shown to the agent (marked shown)."""
        out = []
        for j in slot.jobs.values():
            if j.result is not None and not j.delivered:
                j.delivered = True
                self._joblog(slot, event="delivered", job=j.id, via=via, after_finish_s=round(time.time() - (j.finished or 0), 1))
                out.append(f"[your {j.label()} finished after "
                           f"{_mins((j.finished or time.time()) - j.started)}]\n{j.result}")
        return "\n\n".join(out)

    def jobs_text(self, slot: AgentSlot) -> str:
        now = time.time()
        notes = self.take_notes(slot, "jobs")
        running = [j.status(now) for j in slot.jobs.values() if j.result is None]
        done = [j.status(now) for j in slot.jobs.values() if j.result is not None]
        parts = [f"running: {'; '.join(running) if running else 'none'}",
                 f"finished: {'; '.join(done) if done else 'none'}"]
        if notes:
            parts.append(notes)
        elif done:
            parts.append("(finished results were shown to you earlier; each is also on the board, posted by the gate)")
        return "\n".join(parts)

    async def wait(self, slot: AgentSlot, pulse: Pulse | None = None, cap_s: float | None = None,
                   poll_s: float = WAIT_BOARD_POLL_S) -> dict[str, Any]:
        """Block until the first of: one of this agent's background jobs finishes (event-driven, on the
        job's task), board news addressed to it (polled every `poll_s` through the pulse's own cursors, so
        nothing is shown twice), or `cap_s` with neither. Returns what happened, plus the pulse's line about
        the other new posts. No clock and no limit in the text: only single-job durations."""
        cap = self.node.wait_cap_s if cap_s is None else cap_s
        t0 = time.monotonic()
        notes = self.take_notes(slot, "wait")
        running = [j for j in slot.jobs.values() if j.result is None]
        if not notes and not running and pulse is None:
            return _text("Nothing to wait for: you have no background jobs running (`jobs` lists finished ones). "
                         "Start one with `run` (background: true) or `submit`, and `wait` returns the moment it "
                         "finishes.")
        self._joblog(slot, event="wait", running=[j.id for j in running])
        news = None
        why = "job" if notes else None
        try:
            while why is None:
                left = cap - (time.monotonic() - t0)
                if left <= 0:
                    why = "cap"
                    break
                step = min(poll_s, left) if pulse is not None else left
                tasks = {j.task for j in slot.jobs.values() if j.result is None and j.task is not None}
                if tasks:
                    await asyncio.wait(tasks, timeout=step)
                else:
                    await asyncio.sleep(step)
                notes = self.take_notes(slot, "wait")
                if notes:
                    why = "job"
                    break
                if pulse is not None:
                    news = await pulse.wait_check(news)
                    if news is not None and news.addressed():
                        why = "board"
        finally:
            self._joblog(slot, event="waited", why=why or "stopped", took_s=round(time.monotonic() - t0, 1))
        board_text = await pulse.wait_finish(news) if pulse is not None else None
        now = time.time()
        still = [j.status(now) for j in slot.jobs.values() if j.result is None]
        still_line = f"still running: {'; '.join(still)}" if still else ""
        if why == "job":
            parts = [notes] + ([still_line] if still_line else [])
        elif why == "board":
            parts = ["Something on the board is for you (below)." + (f" Your jobs keep going; {still_line}."
                                                                     if still_line else "")]
        else:
            parts = [("None of your jobs has finished yet and nothing new is addressed to you. " + still_line + ". "
                      if still_line else "Nothing new is addressed to you. ")
                     + "Call `wait` again to keep waiting; meanwhile the board, others' code and your local shell "
                     "are all yours."]
        if board_text:
            parts.append(board_text)
        return _text("\n\n".join(parts))

    async def run(self, slot: AgentSlot, command: str, timeout: int | None, background: bool = False,
                  label: str | None = None, fail_head: str = "") -> dict[str, Any]:
        """A shell command in the jail. Foreground unless asked (or it outlasts
        `run_background_after_s`); see the module docstring for the core rules."""
        n = self.node
        held = self.busy_job(slot)
        if held is not None:
            return self._busy_text(held)
        after = n.run_background_after_s
        asked_bg = bool(background) or (after is not None and timeout is not None and timeout > after)
        label = label or _short(command)
        runs = self.bg_runs(slot)
        queued = self.queued_job(slot)
        running = runs[0] if runs else None
        if asked_bg and queued is not None:
            return _text(
                f"Nothing was started: your held-out submission {queued.id} ({queued.policy}) is queued for your "
                "cores, and it starts as soon as your background runs finish; a new background run would keep it "
                f"waiting. Meanwhile a light foreground run or score (up to {n.shared_run_timeout_s} s) can share "
                "the cores, and the lab's `wait` returns the moment a job of yours finishes.", True)
        if asked_bg and len(runs) >= n.max_background_runs:
            ids = ", ".join(f"{r.id} ({r.policy})" for r in runs)
            return _text(
                f"Nothing was started: your background runs {ids} are going, and the lab runs up to "
                f"{n.max_background_runs} at a time per agent. Their output comes to you on your next lab call after "
                "one finishes, in `jobs`, and from the lab's `wait` (it returns the moment one finishes). Meanwhile "
                f"a light foreground run or score (up to {n.shared_run_timeout_s} s) can share the cores, and the "
                "board, your local shell and others' code are all yours.", True)
        if (running is not None or queued is not None) and not asked_bg:
            t = min(timeout or n.shared_run_timeout_s, n.shared_run_timeout_s)
            async with slot.fg_lock:
                held = self.busy_job(slot)  # a queued submission may have taken the cores while this call waited
                if held is not None:
                    return self._busy_text(held)
                sharing = self.bg_run(slot)
                res = await self._run_once(slot, command, t, timeout, "shared", fail_head, sharing)
            what = f"background run {sharing.id}" if sharing else "your background work"
            return _prepend(res, f"[shared your cores with {what}, so both ran slower; foreground calls are "
                                 f"capped at {n.shared_run_timeout_s} s while background runs are going]\n")
        if asked_bg and after is not None:
            cap = n.background_run_timeout_s
            t = min(timeout or cap, cap)
            job = self._new_job(slot, "run", policy=label, limit_s=t)
            inner = asyncio.create_task(self._run_once(slot, command, t, timeout, "background", fail_head))
            job.task = asyncio.create_task(self._finish_run(slot, job, inner))
            self._joblog(slot, event="run", job=job.id, label=label, limit_s=t, background=True, how="asked")
            why = ("you asked for the background" if background else
                   f"you asked for up to {timeout} s, more than the {after:.0f} s a foreground call waits")
            capped = f" (capped at {cap} s)" if timeout and timeout > cap else ""
            beside = (f" It shares your cores with background run {running.id} ({running.policy}), so both run "
                      "slower than either would alone." if running is not None else "")
            return _text(
                f"Started IN THE BACKGROUND as job {job.id} ({why}); it may run up to {t} s{capped}.{beside} You are "
                "not blocked: its output is saved under .runs/ as usual and comes to you on your next lab call after "
                "it finishes, in `jobs`, from the lab's `wait` (which returns the moment it finishes), and in your "
                "next continue message. Files it writes are pulled back when it ends (files you edit locally "
                f"meanwhile are kept). Up to {n.max_background_runs} background runs can go at once; beside them a "
                f"light foreground run or score (up to {n.shared_run_timeout_s} s) can share your cores, and a "
                "submit queues until they finish.")
        t = min(timeout or n.run_timeout_s, n.run_timeout_s)
        async with slot.fg_lock:
            held = self.busy_job(slot)  # a queued submission may have taken the cores while this call waited
            if held is not None:
                return self._busy_text(held)
            t0 = time.time()
            task = asyncio.create_task(self._run_once(slot, command, t, timeout, "foreground", fail_head))
            if after is None or after >= t:
                return await task
            try:
                done, _ = await asyncio.wait({task}, timeout=after)
            except asyncio.CancelledError:
                task.cancel()
                raise
            if task in done:
                return task.result()
            job = self._new_job(slot, "run", policy=label, limit_s=t, started=t0)
            job.task = asyncio.create_task(self._finish_run(slot, job, task))
            self._joblog(slot, event="run", job=job.id, label=label, limit_s=t, background=True, how="detached")
        return _text(
            f"Still running after {after:.0f} s, so it moved to the BACKGROUND as job {job.id} (it keeps running, up "
            f"to {t} s in all). You are not blocked: its output is saved under .runs/ and comes to you on your next "
            "lab call after it finishes, in `jobs`, from the lab's `wait` (which returns the moment it finishes), and "
            "in your next continue message. For long work, "
            f"`background: true` starts it there at once and allows up to {n.background_run_timeout_s} s. While it "
            f"runs, a light foreground run or score (up to {n.shared_run_timeout_s} s) can share your cores, another "
            "background run can start beside it, and a submit queues until it finishes.")

    async def _finish_run(self, slot: AgentSlot, job: Job, inner: asyncio.Task) -> None:
        try:
            res = await inner
            job.result, job.is_error = _alltext(res), bool(res.get("is_error"))
        except asyncio.CancelledError:
            inner.cancel()
            job.result, job.is_error = "NOT FINISHED: the harness stopped it.", True
            raise
        except Exception as e:  # a background job must always end with a result the agent sees
            job.result, job.is_error = f"NOT FINISHED (harness error {type(e).__name__}: {e})", True
        finally:
            job.finished = time.time()
            self._joblog(slot, event="finished", job=job.id, kind="run", label=job.policy, background=True,
                         took_s=round(job.finished - job.started, 1), is_error=job.is_error)

    async def _run_once(self, slot: AgentSlot, command: str, t: int, asked: int | None, mode: str,
                        fail_head: str = "", sharing: Job | None = None) -> dict[str, Any]:
        """Push, run in the jail with a wall cap of t seconds, pull, save, report.
        mode: foreground | background | shared (a foreground call beside a background run)."""
        t0 = time.time()
        async with slot.lock:
            rc, out = await self._push(slot)
        if rc:
            return _text(f"sync to node failed ({rc}):\n{out[-2000:]}", True)
        inner = self._jail_prefix(slot, slot.remote_ws, t) + shlex.join(["bash", "-c", command])
        rc, out = await _exec(self.node.shell(inner), t + 60)
        took = time.time() - t0
        async with slot.lock:
            prc, pout = await self._pull(slot, update=(mode == "background" or sharing is not None))
        saved = self._save(slot, "run", out)
        note = "" if prc == 0 else f"\n[harness: pulling results back failed: {pout[-500:]}]"
        if "/tmp/" in out and ("Permission denied" in out or "Read-only file system" in out):
            note += ("\n[harness: /tmp is read-only in the lab. Use $TMPDIR instead (it is .tmp/ in your node "
                     "workspace: writable, never synced back), or any path inside your workspace.]")
        if rc:
            note += self._failure_note(rc, out, took, t, asked, mode, sharing)
        head = fail_head if rc and fail_head else ""
        return _text(f"{head}exit {rc}\n{_clip(out, saved, slot.workspace)}{note}", rc != 0)

    def _failure_note(self, rc: int, out: str, took: float, t: int, asked: int | None, mode: str,
                      sharing: Job | None) -> str:
        """Why a lab call failed, when its own output cannot say. An 'exit 1 with an empty log' is almost
        always the jail's wall cap: systemd stops the unit and prints nothing."""
        n = self.node
        empty = not out.strip()
        if rc == 124 or took >= t - 2:
            why = f"\n[harness: STOPPED at this call's {t} s limit{' before it printed anything' if empty else ''}."
            if mode == "foreground":
                cap = (f"a foreground call is capped at {n.run_timeout_s} s. Pass background: true to run up to "
                       f"{n.background_run_timeout_s} s without waiting for it.")
            elif mode == "shared":
                cap = (f"while background run {sharing.id if sharing else ''} has your cores, a foreground call is "
                       f"capped at {n.shared_run_timeout_s} s.")
            else:
                cap = f"a background run is capped at {n.background_run_timeout_s} s."
            why += f" You asked for {asked} s; {cap}" if asked and asked > t else f" {cap[0].upper()}{cap[1:]}"
            return why + (" Output the command wrote to files in your workspace was still pulled back: long jobs "
                          "that write progress to a file keep it.]")
        if rc == 137 or rc == -9:
            return (f"\n[harness: exit {rc} means the process was KILLED, most often by the lab's memory limit "
                    f"({n.mem}).]")
        if empty:
            return (f"\n[harness: exit {rc} with NO output at all: nothing on stdout or stderr, after {took:.0f} s. "
                    "The command runs from your node workspace root (your local shell's current directory does not "
                    "carry over), so check its relative paths; `set -x;` at the start of the command shows each "
                    "step.]")
        return ""

    async def score(self, slot: AgentSlot, policy: str) -> dict[str, Any]:
        policy = own_relative(slot.name, policy)
        rel = Path(policy)
        if not rel.is_absolute() and ".." not in rel.parts and not (slot.workspace / rel).is_file():
            return _text(f"NOT SCORED: {policy!r} is not a file in your workspace. score takes a path relative to "
                         "your workspace root (your shell's current directory does not carry over), e.g. "
                         "`policy.py` or `sub/policy.py`.", True)
        gate = f"{self.node.harness_dir}/tasks/{self.cfg.task_dir.name}/gate.py"
        cmd = shlex.join(["python", gate, policy, "--episodes", str(self.cfg.gate.train_episodes),
                          "--json", "scores/latest.json"])
        return await self.run(slot, cmd, None, label=f"score {policy}",
                              fail_head=f"score of {policy} FAILED: the gate did not finish. Its exit status and "
                                        "full output follow.\n")

    # -- held-out evaluation on fresh seeds
    async def _allocate(self, n: int) -> int:
        """Take the next n held-out seed indices. A counter beside the secret,
        under flock, so no two batches — across agents, submissions and
        harness restarts — ever share an index: every held-out score is on
        seeds nothing has been scored on before."""
        c = shlex.quote(f"{self.node.harness_dir}/secrets/{self.cfg.run_name}.counter")
        script = (f"exec 9>>{c}.lock && flock 9 && n=$(cat {c} 2>/dev/null || echo 0) && "
                  f"echo $((n + {n})) > {c}.tmp && mv {c}.tmp {c} && echo $n")
        rc, out = await _exec(self.node.shell(script), 60)
        if rc or not out.strip().splitlines()[-1].isdigit():
            raise RuntimeError(f"seed allocation failed ({rc}): {out[-300:]}")
        return int(out.strip().splitlines()[-1])

    async def _batch(self, slot: AgentSlot, snap: str, policy: str, offset: int, tag: str) -> tuple[int, str, dict]:
        n = self.node
        task = f"{n.harness_dir}/tasks/{self.cfg.task_dir.name}"
        # The jailed policy host lives for the WHOLE batch, so its runtime cap
        # must cover every episode at the full per-episode policy budget. A
        # shorter cap (run_timeout_s, say) kills a slow policy partway through
        # the batch, and the remaining episodes read as 'error': a harness
        # limit that an agent reads as a fault in its policy.
        batch_s = self._batch_timeout()
        # the gate execs this prefix + the policy host; with no jail, only the wall cap remains
        policy_cmd = (self._jail_prefix(slot, snap, batch_s).strip() if n.jail_template.strip()
                      else f"timeout {batch_s}")
        cpu = ([] if self.cfg.gate.episode_cpu_s is None else ["--episode-cpu", str(self.cfg.gate.episode_cpu_s)])
        cmd = shlex.join([n.remote_python, f"{task}/gate.py", f"{snap}/{policy}", "--split", "heldout",
                          "--secret", f"{n.harness_dir}/secrets/{self.cfg.run_name}.secret",
                          "--episodes", str(self.cfg.gate.heldout_episodes), "--seed-offset", str(offset),
                          *cpu, "--workers", str(n.cores_per_agent),
                          "--policy-cmd", policy_cmd, "--fifo-dir", snap, "--json", f"{snap}/.heldout-{tag}.json"])
        rc, out = await _exec(n.shell(cmd), batch_s + 120)
        lines = out.strip().splitlines()
        try:
            summary = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError:
            summary = {}
        return rc, out, summary

    def _batch_timeout(self) -> int:
        g, n = self.cfg.gate, self.node
        if g.heldout_timeout_s is not None:
            return g.heldout_timeout_s
        return int(g.heldout_episodes * (g.episode_cpu_s or 0.0) / n.cores_per_agent) + 120

    def _better(self, a: float, b: float | None) -> bool:
        """a is a better score than b (None: no score yet), in the gate's direction."""
        return b is None or (a > b if self.cfg.gate.higher_is_better else a < b)

    def _digest_cmd(self, snap: str, policy: str) -> str:
        """Prints the submission's digest as the first token of its last line."""
        if self.cfg.gate.digest == "file":
            return f"sha256sum {shlex.quote(snap + '/' + policy)}"
        # every *.py under the policy's directory, paths included (dot-dirs and files excluded)
        d = shlex.quote(str(Path(snap) / Path(policy).parent))
        return (f"cd {d} && find . -type f -name '*.py' -not -path '*/.*' -print0 | LC_ALL=C sort -z "
                f"| xargs -0 -r sha256sum | sha256sum")

    async def _current_record(self) -> float | None:
        """Seed the shared record from the gate's own confirmed posts (so a
        harness restart does not forget it), then keep it in memory."""
        if not self._board_loaded:
            try:
                envs = await asyncio.to_thread(board.read_since, self.gate_cred, self.cfg.board.gate_ns, -1)
                for e in envs:
                    full = await asyncio.to_thread(board._request, f"{self.gate_cred.url}/envelope/{e['id']}",
                                                   self.gate_cred.token)
                    p = (full.get("envelope") or full).get("payload") or {}
                    if e["author"] != self.gate_cred.identity or p.get("kind") != "gate-result":
                        continue
                    if p.get("sha256") and isinstance(p.get("batches"), list):
                        # each post carries its file's cumulative batches: the latest wins
                        self.batches_by_sha[p["sha256"]] = list(p["batches"])
                    if p.get("record") and self._better(float(p["score"]), self.record):
                        self.record = float(p["score"])
                    if p.get("confirmed") and p.get("agent") and p.get("score") is not None:
                        self._note_agent_best(str(p["agent"]), float(p["score"]))
                self._board_loaded = True
            except Exception:
                pass  # retried next submit; meanwhile no record is the safe default (it forces confirmation)
        return self.record

    def _note_agent_best(self, agent: str, score: float) -> None:
        if self._better(score, self.agent_best.get(agent)):
            self.agent_best[agent] = score

    async def submit(self, slot: AgentSlot, policy: str) -> dict[str, Any]:
        if self.bg_runs(slot):
            return await self._queue_submit(slot, policy)
        async with slot.fg_lock:
            if self.bg_runs(slot):  # a foreground run moved to the background while this call waited
                return await self._queue_submit(slot, policy)
            return await self._submit(slot, policy)

    def _submit_refusal(self, slot: AgentSlot, policy: str) -> dict[str, Any] | None:
        """Why this submission cannot be taken now, or None."""
        rel = Path(policy)
        if rel.is_absolute() or ".." in rel.parts or not (slot.workspace / rel).is_file():
            return _text(f"{policy!r}: give a path to a file inside your workspace", True)
        wait = self.cfg.gate.submit_cooldown_s - (time.time() - slot.last_submit)
        if wait > 0:
            return _text(f"held-out submissions are rate limited: next one allowed in {wait/60:.0f} min", True)
        busy = self.busy_job(slot) or self.queued_job(slot)
        if busy is not None:
            return self._busy_text(busy)
        return None

    async def _freeze(self, slot: AgentSlot, policy: str) -> tuple[str, str] | dict[str, Any]:
        """Push, copy the node workspace to a submission snapshot, digest it: (snap, sha), or an error result.
        The caller holds the slot's lock."""
        n = self.node
        rc, out = await self._push(slot)
        if rc:
            return _text(f"sync to node failed ({rc}):\n{out[-2000:]}", True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        snap = f"{n.run_root}/submissions/{slot.name}/{stamp}"
        rc, out = await _exec(n.shell(" && ".join([
            f"mkdir -p {shlex.quote(snap)}",
            f"cp -a {shlex.quote(slot.remote_ws)}/. {shlex.quote(snap)}/",
            f"rm -rf {shlex.quote(snap)}/.runs {shlex.quote(snap)}/.tmp {shlex.quote(snap)}/.shm",
            self._digest_cmd(snap, policy)])), 120)
        if rc:
            return _text(f"NOT SCORED (could not freeze a snapshot, exit {rc}; this did not use your submission):\n{out[-1500:]}", True)
        return snap, out.strip().splitlines()[-1].split()[0]

    async def _queue_submit(self, slot: AgentSlot, policy: str) -> dict[str, Any]:
        """A submit while background runs hold the cores: freeze the snapshot now, score it when they finish."""
        policy = own_relative(slot.name, policy)
        refused = self._submit_refusal(slot, policy)
        if refused is not None:
            return refused
        async with slot.lock:
            frozen = await self._freeze(slot, policy)
        if isinstance(frozen, dict):
            return frozen
        snap, sha = frozen
        behind = self.bg_runs(slot)
        job = self._new_job(slot, "submit", policy=policy, sha=sha, snap=snap, queued=True)
        self._joblog(slot, event="submit", policy=policy, sha=sha, snap=snap, background=True, queued=True,
                     job=job.id, behind=[r.id for r in behind])
        job.task = asyncio.create_task(self._queued_job(slot, job))
        ids = ", ".join(f"{r.id} ({r.policy})" for r in behind) or "your background run"
        return _text(
            f"Held-out submission of {policy} is QUEUED as job {job.id}. Your background runs {ids} are using your "
            "cores, and a held-out batch is scored with the cores to itself (the policy's time budget is "
            "wall-clock), so it starts the moment they finish. Its snapshot was frozen just now: edits you make from "
            "here on are not part of it. You are not blocked: the gate posts the result to the board as usual, and "
            "it comes to you on your next lab call after it finishes, in `jobs`, from the lab's `wait`, and in your "
            "next continue message. Until it has run, new background runs wait for it; light foreground runs and "
            "scores still share your cores.")

    async def _queued_job(self, slot: AgentSlot, job: Job) -> None:
        """Wait until no background run and no foreground call is using the cores, then score like any
        background submission. New background runs are refused while this waits, so it cannot starve."""
        t_queued = job.started
        try:
            while True:
                tasks = {r.task for r in self.bg_runs(slot) if r.task is not None}
                if self.bg_runs(slot):
                    if tasks:
                        await asyncio.wait(tasks)
                    else:
                        await asyncio.sleep(1.0)
                    continue
                async with slot.fg_lock:  # no foreground call in flight
                    if self.bg_runs(slot):  # a foreground run moved to the background meanwhile
                        continue
                    job.queued = False
                    job.started = time.time()
                    break
        except asyncio.CancelledError:
            job.result, job.is_error = "NOT SCORED: the run ended before it finished.", True
            job.finished = time.time()
            raise
        except Exception as e:
            job.result, job.is_error = (f"NOT SCORED (harness error {type(e).__name__}: {e}; this did not use your "
                                        "submission)"), True
            job.finished = time.time()
            return
        self._joblog(slot, event="dequeued", job=job.id, queued_s=round(job.started - t_queued, 1))
        await self._run_job(slot, job)

    async def _submit(self, slot: AgentSlot, policy: str) -> dict[str, Any]:
        policy = own_relative(slot.name, policy)
        refused = self._submit_refusal(slot, policy)
        if refused is not None:
            return refused
        t0 = time.time()
        async with slot.lock:
            frozen = await self._freeze(slot, policy)
            if isinstance(frozen, dict):
                return frozen
            snap, sha = frozen
            est, how = await self._estimate(slot, policy)
        g = self.cfg.gate
        background = g.background_after_s is not None and est is not None and est > g.background_after_s
        job = self._new_job(slot, "submit", policy=policy, sha=sha, snap=snap, estimate_s=est) if background else None
        self._joblog(slot, event="submit", policy=policy, sha=sha, snap=snap, estimate_s=est, estimate_how=how,
                     background=background, prep_s=round(time.time() - t0, 1), **({"job": job.id} if job else {}))
        if job is not None:
            job.task = asyncio.create_task(self._run_job(slot, job))
            return _text(
                f"Held-out submission of {policy} started IN THE BACKGROUND as job {job.id}: {how}, one held-out "
                f"batch is estimated at ~{_mins(est)} (over the {_mins(g.background_after_s)} this lab waits for; a "
                "score that would beat the record earns a second, confirmation batch, so up to twice that). "
                "You are not blocked: the gate posts the result to the board as usual, and it is shown to you on "
                "your next lab call, in `jobs`, and in your next continue message. Until it finishes, your node "
                "cores are its own, so run/score/submit wait for it; the board, your local shell and others' code "
                "are all yours meanwhile.")
        res = await self._scored(slot, snap, policy, sha)
        note = (f"[{how}: one batch estimated at ~{_mins(est)}; this submission took {_mins(time.time() - t0)}]\n"
                if est is not None else "")
        self._joblog(slot, event="finished", policy=policy, sha=sha, background=False,
                     took_s=round(time.time() - t0, 1), is_error=bool(res.get("is_error")))
        res["content"][0]["text"] = note + res["content"][0]["text"]
        return res

    async def _run_job(self, slot: AgentSlot, job: SubmitJob) -> None:
        try:
            res = await self._scored(slot, job.snap, job.policy, job.sha)
            job.result, job.is_error = res["content"][0]["text"], bool(res.get("is_error"))
        except asyncio.CancelledError:
            job.result, job.is_error = "NOT SCORED: the run ended before it finished.", True
            raise
        except Exception as e:  # a background job must always end with a result the agent sees
            job.result, job.is_error = (f"NOT SCORED (harness error {type(e).__name__}: {e}; this did not use your "
                                        "submission)"), True
        finally:
            job.finished = time.time()
            self._joblog(slot, event="finished", job=job.id, kind=job.kind, policy=job.policy, sha=job.sha, background=True,
                         took_s=round(job.finished - job.started, 1), estimate_s=job.estimate_s, is_error=job.is_error)

    async def _estimate(self, slot: AgentSlot, policy: str) -> tuple[float | None, str]:
        """How long ONE held-out batch of this policy will take, in seconds, and how that was found.

        A fixed `estimate_s` from the config wins (gates whose cost does not
        scale with a per-episode policy, e.g. lmspeed's fixed training budget).
        Otherwise the policy is timed in the jail, on the agent's own cores,
        from its (just synced) workspace: first the gate with 0 episodes (its
        start-up and one policy process's), then `probe_episodes` training
        episodes on `cores_per_agent` workers, as the held-out batch runs them;
        the episode time scales by heldout_episodes / probe_episodes, since
        episode time in the jail is linear in the episode count. None: unknown
        (probing is off or failed), and the submission runs in the foreground."""
        g, n = self.cfg.gate, self.node
        if g.estimate_s is not None:
            return float(g.estimate_s), "this task's fixed estimate"
        if g.probe_episodes <= 0 or g.background_after_s is None:
            return None, "not timed"
        gate = f"{n.harness_dir}/tasks/{self.cfg.task_dir.name}/gate.py"
        cpu = [] if g.episode_cpu_s is None else ["--episode-cpu", str(g.episode_cpu_s)]
        base = ["python", gate, policy, *cpu]
        w, k = n.cores_per_agent, g.probe_episodes
        script = (f"s=$(date +%s.%N); {shlex.join(base + ['--episodes', '0'])} >/dev/null 2>&1; m=$(date +%s.%N); "
                  f"timeout {PROBE_CAP_S} {shlex.join(base + ['--episodes', str(k), '--workers', str(w)])} 2>/dev/null; "
                  f"rc=$?; e=$(date +%s.%N); echo PROBE $rc $s $m $e")
        cap = PROBE_CAP_S * 2 + 60
        inner = self._jail_prefix(slot, slot.remote_ws, cap) + shlex.join(["bash", "-c", script])
        rc, out = await _exec(n.shell(inner), cap + 60)
        line = next((ln for ln in reversed(out.strip().splitlines()) if ln.startswith("PROBE ")), None)
        if line is None:
            return None, f"timing probe failed (exit {rc})"
        try:
            prc, s, m, e = line.split()[1:5]
            prc_i, startup, eps = int(prc), float(m) - float(s), float(e) - float(m)
        except ValueError:
            return None, "timing probe unreadable"
        if prc_i == 124:  # the probe's own cap: a lower bound is enough to know it is slow
            return round(eps * g.heldout_episodes / k), f"timed on {k} training episodes (cut off at {PROBE_CAP_S} s)"
        if prc_i != 0:
            return None, f"timing probe failed (gate exit {prc_i})"
        # start-up (the gate's own Python start plus one policy process) is paid once per batch, so it is
        # subtracted once; subtracting it once per worker under-estimates the batch several-fold
        est = startup + max(0.0, eps - startup) * g.heldout_episodes / k
        return round(est), f"timed on {k} training episodes in your jail"

    async def _scored(self, slot: AgentSlot, snap: str, policy: str, sha: str) -> dict[str, Any]:
        """The held-out batch (and a confirmation batch for a candidate record),
        pooled, judged against the record, and posted by the gate: everything
        a submit does from seed allocation on, whether it runs in the
        foreground or as a background job."""
        try:
            off1 = await self._allocate(self.cfg.gate.heldout_episodes)
        except RuntimeError as e:
            return _text(f"NOT SCORED (infrastructure: {e}; this did not use your submission)", True)
        async with slot.lock:
            rc, out, s1 = await self._batch(slot, snap, policy, off1, "a")
        saved = self._save(slot, "submit", out)
        if "infra_error" in s1:
            return _text("NOT SCORED (infrastructure: the policy process never started; this did not use "
                         f"your submission): {s1['infra_error']}\n[full log: {saved.relative_to(slot.workspace)}]", True)
        if rc or "mean" not in s1:
            return _text(f"NOT SCORED (the held-out run failed, exit {rc}; this did not use your submission):\n"
                         f"{_clip(out, saved, slot.workspace)}", True)
        if s1["ends"] == {"error": s1["episodes"]} and s1.get("mean_steps", 0) == 0:
            return _text("NOT SCORED: every episode failed before the first move, so nothing was "
                         "posted and this did not use your submission. First error:\n"
                         f"{s1.get('first_error', '(none recorded)')}\nScore it on training seeds first.", True)
        slot.last_submit = time.time()
        async with self.record_lock:
            await self._current_record()  # the board's record and per-file evidence, before using either
        prior = self.batches_by_sha.setdefault(sha, [])
        prior.append({"seed_range": s1.get("seed_range"), "mean": s1["mean"], "std": s1["std"], "episodes": s1["episodes"]})
        batches = list(prior)
        ends = dict(s1["ends"])
        # these bytes already have an independent batch; an exact score needs no second one
        confirmed = len(batches) >= 2 or self.cfg.gate.exact
        # The lock guards READING and UPDATING the record only. It is never
        # held across a batch: a confirmation can take most of an hour, and
        # every other agent's finished submission would queue behind it.
        async with self.record_lock:
            best = self.record
        auto = False
        if not confirmed and self._better(s1["mean"], best):
            # A candidate record: the best of many noisy scores is inflated
            # by selection, so it earns a second, independent fresh batch,
            # and the verified score is the pooled mean of both.
            try:
                off2 = await self._allocate(self.cfg.gate.heldout_episodes)
                async with slot.lock:
                    rc2, out2, s2 = await self._batch(slot, snap, policy, off2, "b")
                self._save(slot, "confirm", out2)
                if rc2 == 0 and "mean" in s2:
                    b2 = {"seed_range": s2.get("seed_range"), "mean": s2["mean"],
                          "std": s2["std"], "episodes": s2["episodes"]}
                    prior.append(b2)
                    batches.append(b2)
                    for k, v in s2["ends"].items():
                        ends[k] = ends.get(k, 0) + v
                    confirmed = True
            except RuntimeError:
                pass  # unconfirmed: reported as such below, never as a record
        elif not confirmed:
            auto = self._auto_confirm_due(slot, s1)
        res = await self._judge_and_post(slot, snap, policy, sha, batches, ends, confirmed, auto)
        if auto:
            slot.auto_confirms.append(time.time())
            job = self._new_job(slot, "confirm", policy=policy, sha=sha, snap=snap, ends=dict(s1["ends"]))
            self._joblog(slot, event="job", job=job.id, kind="confirm", policy=policy, sha=sha)
            job.task = asyncio.create_task(self._run_confirm(slot, job))
            res = _prepend(res, "")
            res["content"][0]["text"] += (
                f"\n\nThis batch clearly beats your own best confirmed score, so the lab is running ONE confirmation "
                f"batch of this exact file on fresh seeds in the background, as job {job.id}. Its pooled, confirmed "
                "result is posted by the gate and comes to you on your next lab call after it finishes, in `jobs`, "
                "and in your next continue message. Until then your node cores are the batch's, so run, score and "
                "submit wait for it; the board, your local shell and others' code are all yours meanwhile.")
        return res

    def _auto_confirm_due(self, slot: AgentSlot, s1: dict) -> bool:
        """A single batch that is no record candidate but clearly beats this
        agent's own confirmed best (or the agent has none yet) earns one
        confirmation batch. Bounded per agent per rolling hour; never shown."""
        g = self.cfg.gate
        if g.auto_confirm_margin_se is None or g.auto_confirm_per_hour <= 0:
            return False
        now = time.time()
        slot.auto_confirms[:] = [t for t in slot.auto_confirms if now - t < 3600]
        if len(slot.auto_confirms) >= g.auto_confirm_per_hour:
            return False
        own = self.agent_best.get(slot.name)
        if own is None:
            return True
        se = float(s1.get("std") or 0.0) / math.sqrt(max(1, int(s1.get("episodes") or 1)))
        if g.run_sd is not None:
            se = math.sqrt(g.run_sd ** 2 + se ** 2)
        m = g.auto_confirm_margin_se * se
        cautious = s1["mean"] - m if g.higher_is_better else s1["mean"] + m
        return self._better(cautious, own)

    async def _run_confirm(self, slot: AgentSlot, job: Job) -> None:
        try:
            off = await self._allocate(self.cfg.gate.heldout_episodes)
            async with slot.lock:
                rc, out, s2 = await self._batch(slot, job.snap, job.policy, off, "c")
            saved = self._save(slot, "confirm", out)
            if rc or "mean" not in s2:
                job.result, job.is_error = (f"The confirmation batch for {job.policy} failed (exit {rc}), so it stays "
                                            "UNCONFIRMED; resubmitting this exact file adds a batch to its pooled "
                                            f"score.\n{_clip(out, saved, slot.workspace)}"), True
                return
            prior = self.batches_by_sha.setdefault(job.sha, [])
            prior.append({"seed_range": s2.get("seed_range"), "mean": s2["mean"], "std": s2["std"],
                          "episodes": s2["episodes"]})
            ends = dict(job.ends or {})
            for k, v in s2["ends"].items():
                ends[k] = ends.get(k, 0) + v
            res = await self._judge_and_post(slot, job.snap, job.policy, job.sha, list(prior), ends, True, False)
            job.result, job.is_error = _alltext(res), bool(res.get("is_error"))
        except asyncio.CancelledError:
            job.result, job.is_error = "NOT CONFIRMED: the harness stopped the confirmation batch before it finished.", True
            raise
        except Exception as e:
            job.result, job.is_error = (f"NOT CONFIRMED (harness error {type(e).__name__}: {e}); resubmitting this "
                                        "exact file adds a batch to its pooled score."), True
        finally:
            job.finished = time.time()
            self._joblog(slot, event="finished", job=job.id, kind="confirm", policy=job.policy, sha=job.sha,
                         background=True, took_s=round(job.finished - job.started, 1), is_error=job.is_error)

    async def _judge_and_post(self, slot: AgentSlot, snap: str, policy: str, sha: str, batches: list[dict],
                              ends: dict, confirmed: bool, auto: bool) -> dict[str, Any]:
        """Pool, judge against the record, post as the gate, and word the result."""
        record = False
        score, ci = _pooled(batches, self.cfg.gate.run_sd, self.cfg.gate.exact)
        hib = self.cfg.gate.higher_is_better
        # the end of the interval nearest to "worse": it must clear the record
        cautious, hopeful = (ci[0], ci[1]) if hib else (ci[1], ci[0])
        async with self.record_lock:
            # A record must be SIGNIFICANTLY better: the cautious end of its 95%
            # interval clears the current record. Inside the noise is a tie.
            if confirmed and self._better(cautious, self.record):
                self.record, record = score, True
            tie = (confirmed and not record and self.record is not None and not self.cfg.gate.exact
                   and not self._better(self.record, hopeful))
            if confirmed:
                self._note_agent_best(slot.name, score)
        result = {"agent": slot.name, "policy": policy, "sha256": sha, "split": "heldout",
                  # the path every agent can read in the lab (the snapshot on the node is not reachable)
                  "code": f"../{slot.name}/{policy}", "snapshot": snap,
                  "score": score, "ci95": ci, "episodes": sum(b["episodes"] for b in batches),
                  "batches": batches, "ends": ends, "confirmed": confirmed, "record": record, "tie": tie,
                  "mean": score}
        try:
            env = await asyncio.to_thread(board.post, self.gate_cred, self.cfg.board.gate_ns, "FINDING",
                                          {"kind": "gate-result", **result})
            posted = f"\nposted to the board as #{env['id']} in {self.cfg.board.gate_ns}"
        except Exception as e:  # never lose a finished held-out result to a board hiccup
            posted = f"\n[harness: result NOT posted to the board ({type(e).__name__}: {e}); it is saved at {snap}]"
        worse = "below" if hib else "above (lower is better)"
        exact = self.cfg.gate.exact
        head = ("NEW RECORD: better than the previous best (the score is exact)" if record and exact else
                "NEW RECORD: significantly better than the previous best, on fresh seeds" if record else
                f"ties the record {self.record} within noise" if tie else
                f"{worse} the record {self.record}" if confirmed else
                # agents misread an unconfirmed batch as a missing or a counted result, so the head names it
                "UNCONFIRMED: scored on one fresh batch only, so it does not count as a verified score yet"
                + (" (a confirmation batch is running for it: see below)" if auto else
                   f" (the record is {self.record}; the lab confirms a batch that could beat it)"
                   if self.record is not None else "")
                + ". Resubmitting this exact file adds a batch to its pooled score.")
        if not exact:
            head += f" [{len(batches)} batch{'es' if len(batches) != 1 else ''} pooled for this exact file]"
        return _text(f"{head}\n{json.dumps(result, indent=2)}{posted}")


def own_relative(name: str, policy: str) -> str:
    """`../<own name>/x.py` (the form gate results print as `code`) means
    that file in the agent's own workspace: agents copy that path back into
    submit, and refusing it costs a round trip."""
    parts = Path(policy).parts
    if len(parts) > 2 and parts[0] == ".." and parts[1] == name:
        return str(Path(*parts[2:]))
    return policy


def _pooled(batches: list[dict], run_sd: float | None = None, exact: bool = False) -> tuple[float, list[float]]:
    """Pooled mean and 95% CI over batches, from their means and stds.

    exact: the gate's score is exact (gate.exact), so every batch of one file
    is the same number. It is returned at the gate's own precision with a
    zero-width interval: rounding it here could erase a one-step record.

    run_sd None: every episode is one independent draw (snake), so batches
    pool as one big sample. run_sd set: each batch is ONE training run whose
    mean carries its own run-to-run noise, which more episodes cannot shrink.
    The mean's variance is then sum_b w_b^2 (s^2 + std_b^2 / n_b), with
    w_b = n_b / N and s = max(run_sd, the observed sd of the batch means):
    two runs say little about their own spread, so a calibrated floor is
    used unless the runs disagree by more."""
    if exact:
        M = batches[-1]["mean"]
        return M, [M, M]
    N = sum(b["episodes"] for b in batches)
    M = sum(b["mean"] * b["episodes"] for b in batches) / N
    if run_sd is not None:
        means = [b["mean"] for b in batches]
        s = max(run_sd, statistics.stdev(means) if len(means) > 1 else 0.0)
        var = sum((b["episodes"] / N) ** 2 * (s ** 2 + b["std"] ** 2 / b["episodes"]) for b in batches)
        half = 1.96 * math.sqrt(var)
        return round(M, 4), [round(M - half, 4), round(M + half, 4)]
    ss = sum((b["episodes"] - 1) * b["std"] ** 2 + b["episodes"] * (b["mean"] - M) ** 2 for b in batches)
    sd = math.sqrt(ss / (N - 1)) if N > 1 else 0.0
    half = 1.96 * sd / math.sqrt(N) if N > 1 else float("nan")
    return round(M, 3), [round(M - half, 3), round(M + half, 3)]


async def unread_canon(cred: board.Credential) -> list[int] | None:
    """The canon documents this band has not acked, from the board's own
    onboard view (the same computation Korax's required reading uses).
    None if the board cannot be asked: the gate then fails OPEN for this
    call, because a board hiccup must not lock an onboarded agent out."""
    try:
        v = await asyncio.to_thread(board._request, f"{cred.url}/view/onboard", cred.token)
    except Exception:
        return None
    out = v.get("output") or v
    return [int(d["id"]) for d in out.get("canon") or [] if not d.get("read")]


def onboard_first(unread: list[int]) -> dict[str, Any]:
    ids = " ".join(str(i) for i in unread)
    return _text(
        "Onboard first: the lab runs nothing until you have read and acked the board's canon, which is how "
        f"this board and the agents on it work. Unread: {', '.join('#' + str(i) for i in unread)}.\n"
        f"Read each (`korax envelope <id>`, or korax_envelope), then ack it after reading: `korax ack {ids}` "
        "(or korax_ack). `korax onboard` shows what is still unread.", True)


@dataclass(frozen=True)
class ToolSpec:
    """A harness-hosted tool, independent of the agent backend: the Claude
    backend wraps these as an in-process MCP server, the Codex backend as
    `dynamicTools` answered over JSON-RPC. `handler(args)` returns the MCP
    result shape: {"content": [{"type": "text", "text": ...}], "is_error": bool}."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def lab_tool_specs(lab: Lab, slot: AgentSlot, cred: board.Credential | None = None, opening_round=None,
                   pulse: Pulse | None = None) -> list[ToolSpec]:
    """The lab's tools for one agent. Bound to one slot: an agent can only
    ever run, score or submit as itself. With `cred`, every lab call first
    requires the agent's canon to be acked: an agent that skips onboarding
    misses exactly the sections that keep agents working, and idles."""

    def with_notes(res: dict[str, Any], via: str) -> dict[str, Any]:
        """Any background job result not yet shown rides on the next lab call's
        output: agents that never end their turn never see a continue message."""
        notes = lab.take_notes(slot, via)
        if notes:
            res = {**res, "content": [*res.get("content", []), {"type": "text", "text": "\n\n" + notes}]}
        return res

    async def gated(call, via: str = "lab"):
        if cred is not None:
            unread = await unread_canon(cred)
            if unread:
                return with_notes(onboard_first(unread), via)
        if opening_round is not None and cred is not None:
            why = await opening_round.lab_blocked(slot.name, cred)
            if why:
                return with_notes(_text(why, True), via)
        return with_notes(await call(), via)

    async def run(args: dict[str, Any]) -> dict[str, Any]:
        try:
            timeout = int(args["timeout_s"]) if args.get("timeout_s") not in (None, "") else None
        except (TypeError, ValueError):
            return _text(f"timeout_s must be a whole number of seconds, got {args.get('timeout_s')!r}. Nothing was run.",
                         True)
        bg = args.get("background")
        bg = bg if isinstance(bg, bool) else str(bg).strip().lower() in ("true", "1", "yes")
        return await gated(lambda: lab.run(slot, str(args["command"]), timeout, bg), "run")

    async def score(args: dict[str, Any]) -> dict[str, Any]:
        return await gated(lambda: lab.score(slot, str(args["policy"])), "score")

    async def submit(args: dict[str, Any]) -> dict[str, Any]:
        return await gated(lambda: lab.submit(slot, str(args["policy"])), "submit")

    async def jobs(args: dict[str, Any]) -> dict[str, Any]:
        return _text(lab.jobs_text(slot))

    async def wait(args: dict[str, Any]) -> dict[str, Any]:
        return await lab.wait(slot, pulse)

    n = lab.node
    after = n.run_background_after_s
    bg_line = ("" if after is None else
               f" Long work can run in the BACKGROUND: pass background: true (up to {n.background_run_timeout_s} s), "
               f"or a timeout_s over {after:.0f}; a call still running after {after:.0f} s moves there by itself. "
               "The call then returns a job id at once, and the output comes to you on your next lab call after it "
               "finishes, in `jobs`, from `wait`, and in your next continue message. Up to "
               f"{n.max_background_runs} background runs at a time, sharing your cores; beside them, light foreground "
               f"runs (up to {n.shared_run_timeout_s} s) share them too, and a submit queues until they finish.")
    specs = [
        ToolSpec("run", "Sync your workspace to your node workspace and run a shell command there, "
                 "inside your sandbox on your own CPU cores, from your node workspace root. Returns exit code and "
                 "output; the full log is saved under .runs/ in your workspace. /tmp is read-only there: scratch "
                 "files go in $TMPDIR (.tmp/ in your node workspace, never synced back)." + bg_line,
                 {"type": "object", "properties": {
                     "command": {"type": "string"},
                     "timeout_s": {"type": "integer", "description": f"optional; a foreground call is capped at "
                                   f"{n.run_timeout_s} s" + ("" if after is None else
                                   f", a background run at {n.background_run_timeout_s} s")},
                     **({} if after is None else {"background": {
                         "type": "boolean", "description": "start it in the background and get a job id at once"}})},
                  "required": ["command"]}, run),
        *((ToolSpec("score", "Check a submission file with the task gate, exactly as `submit` will: the same "
                    "computation, posted nowhere. `policy` is the file's path in your workspace.",
                    {"type": "object", "properties": {"policy": {"type": "string"}}, "required": ["policy"]}, score),
           ToolSpec("submit", "Have the gate check a submission file and post its score to the board: the only "
                    "score that counts. The score is exact (no held-out set, no noise): the same file always "
                    "scores the same, and anything strictly above the record is a record. `policy` is the "
                    "file's path in your workspace.",
                    {"type": "object", "properties": {"policy": {"type": "string"}}, "required": ["policy"]}, submit))
          if lab.cfg.gate.exact else
          (ToolSpec("score", "Score a policy file on the public TRAINING seeds with the task gate.",
                    {"type": "object", "properties": {"policy": {"type": "string"}}, "required": ["policy"]}, score),
           ToolSpec("submit", "Score a policy on the HELD-OUT seeds. The result is posted to the board by the "
                    "gate and is the only score that counts. The policy is first timed on a few training episodes; "
                    "a slow one runs in the background (this call then returns at once with a job id, and the "
                    "result comes to you on a later lab call and in `jobs`).",
                    {"type": "object", "properties": {"policy": {"type": "string"}}, "required": ["policy"]}, submit))),
        ToolSpec("jobs", "Your background jobs (runs, held-out submissions, confirmation batches) and the results of "
                 "finished ones.",
                 {"type": "object", "properties": {}}, jobs),
        ToolSpec("wait", "Wait on your lab: returns the moment one of your background jobs finishes (with its "
                 "result)" + ("" if pulse is None else ", or something on the board is addressed to you (a DM, a "
                 "mention, a reply to your post) or the gate posts a new record") + ", whichever comes first; if "
                 "neither happens for a while it returns anyway with your jobs' status. Use it instead of a shell "
                 "`sleep`: it never overshoots the job" + ("" if pulse is None else ", and board news reaches you "
                 "while you wait") + ".",
                 {"type": "object", "properties": {}}, wait),
    ]
    if opening_round is not None:
        async def propose(args: dict[str, Any]) -> dict[str, Any]:
            if cred is not None:
                unread = await unread_canon(cred)
                if unread:
                    return onboard_first(unread)
            parts = {k: str(args.get(k, "")).strip() for k in ("problem", "plan", "risks")}
            thin = [k for k, v in parts.items() if len(v) < PROPOSAL_PART_MIN]
            if thin:
                return _text(f"Each part needs substance (at least {PROPOSAL_PART_MIN} characters); too short: "
                             f"{', '.join(thin)}. Nothing was sealed; call propose again.", True)
            text = (f"Read of the problem:\n{parts['problem']}\n\nPlan:\n{parts['plan']}\n\n"
                    f"Risks, and what would tell me early:\n{parts['risks']}")
            return _text(await opening_round.propose(slot.name, text))

        specs.append(ToolSpec(
            "propose", "The opening round: submit your SEALED proposal. Nobody sees it until every agent has "
            "proposed; then all are posted together, and this call returns them all. Take the time to think "
            "it through: it is the one moment your view is formed independently of everyone else's.",
            {"type": "object", "properties": {
                "problem": {"type": "string", "description": "your read of the problem: what makes it hard, "
                            "where the score is won or lost"},
                "plan": {"type": "string", "description": "what you would try, and why you expect it to work"},
                "risks": {"type": "string", "description": "what you expect to be hardest, or most likely wrong "
                          "about your plan, and what would tell you early"}},
             "required": ["problem", "plan", "risks"]}, propose))

        async def answer(args: dict[str, Any]) -> dict[str, Any]:
            raw = args.get("replies") or []
            if isinstance(raw, (int, str)):
                raw = [raw]
            try:
                replies = [int(str(x).lstrip("#")) for x in raw]
            except ValueError:
                return _text(f"replies must be envelope ids (numbers), got {raw!r}. Nothing was posted.", True)
            text, err = await opening_round.answer(slot.name, str(args.get("text", "")), replies)
            return with_notes(_text(text, err), "answer")

        specs.append(ToolSpec(
            "answer", "The opening round, after the reveal: post your ANSWER to the round, in turn. It waits until "
            "every agent before you in the answer order has answered (or let its turn pass) and shows you any "
            "answers you have not seen yet, WITHOUT posting yours; call it again to post. Once your answer is "
            "posted, the lab, posting and messaging open for you.",
            {"type": "object", "properties": {
                "text": {"type": "string", "description": "which direction you take (yours, someone else's, a "
                         "split of one, or a combination), and why that adds most to the swarm's chance of beating "
                         "the best, given the answers before yours"},
                "replies": {"type": "array", "items": {"type": "integer"},
                            "description": "envelope ids of the proposals and answers this one takes up or argues with"}},
             "required": ["text", "replies"]}, answer))
    return specs


def lab_server(lab: Lab, slot: AgentSlot, cred: board.Credential | None = None, opening_round=None,
               pulse: Pulse | None = None):
    """The per-agent in-process MCP server for the Claude backend."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = []
    for spec in lab_tool_specs(lab, slot, cred, opening_round, pulse):
        tools.append(tool(spec.name, spec.description, spec.schema)(spec.handler))
    return create_sdk_mcp_server(name="lab", version="0.1.0", tools=tools)


def policy_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
