"""swarm panel CONFIG — a live local page showing every agent in a run.

Reads what the run already writes (per-agent transcripts and audit logs)
plus the board, and serves it at http://127.0.0.1:<port>/ with a JSON
endpoint the page polls. Works for a live run or a finished one; it never
writes anything. Stdlib only.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import board
from ..agent import slot_paths
from ..config import RunConfig

HTML = Path(__file__).with_name("index.html")
BLOCKED_AFTER_S = 180  # a lab call this long is the agent BLOCKED on it (the default of stigmergeia.analysis.idle --blocked-min 3)


@dataclass
class AgentView:
    """Incrementally folded from one agent's transcript and audit log."""

    name: str
    offset: int = 0
    audit_offset: int = 0
    started: float | None = None
    last_t: float | None = None
    last_kind: str = "not started"
    banked: float = 0.0
    running: float = 0.0
    estimate: float = 0.0
    seen: set[str] = field(default_factory=set)
    turns: int = 0
    idle_waits: int = 0
    idle_until: float | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    open_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_text: str = ""
    last_text_t: float | None = None
    ended: str | None = None
    errors: list[str] = field(default_factory=list)
    outside: list[dict[str, Any]] = field(default_factory=list)
    blocked_s: float = 0.0  # finished lab calls that kept the agent waiting >= BLOCKED_AFTER_S
    jobs_offset: int = 0
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)  # background lab jobs still running
    bg_s: float = 0.0  # finished background lab jobs: compute while the agent was free, NOT idle
    bg_done: int = 0


def _long_sleep(call: dict[str, Any]) -> bool:
    """A foreground shell call that sleeps >= 30 s. An agent can sleep inside a turn on its own
    background job, and the between-turn idle count never sees that time, so it is counted here."""
    if call.get("tool") != "Bash" or call.get("background"):
        return False
    return any(int(n) >= 30 for n in re.findall(r"\bsleep\s+(\d+)", call.get("what", "")))


def _is_lab(call: dict[str, Any]) -> bool:
    return str(call.get("tool", "")).startswith("mcp__lab__")


def _summarise_call(name: str, inp: dict[str, Any]) -> str:
    for key in ("command", "policy", "file_path", "pattern", "ns", "payload", "query"):
        if key in inp:
            val = inp[key]
            return (val if isinstance(val, str) else json.dumps(val))[:300]
    return json.dumps(inp)[:300]


class Panel:
    def __init__(self, cfg: RunConfig, prices_cost=None):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.views = {cfg.agent_name(i): AgentView(cfg.agent_name(i)) for i in range(cfg.n_agents)}
        self.creds: dict[str, board.Credential] = {}
        self.gate: board.Credential | None = None
        try:
            self.gate = board.Credential.model_validate_json((cfg.run_dir / "gate.json").read_text())
            for i in range(cfg.n_agents):
                _, private = slot_paths(cfg, i)
                self.creds[cfg.agent_name(i)] = board.Credential.model_validate_json(
                    (private / "korax.json").read_text())
        except (OSError, ValueError):
            pass  # not provisioned yet: the page says so
        self.board_cache: dict[str, Any] = {"posts": [], "gate": [], "error": None, "t": 0.0}

    # -- the lab's own job log (private/<agent>/lab-jobs.jsonl)
    @staticmethod
    def _fold_job(v: AgentView, row: dict[str, Any]) -> None:
        ev, job = row.get("event"), row.get("job")
        if not job:
            return
        if ev in ("run", "job") or (ev == "submit" and row.get("background")):
            kind = row.get("kind") or ("run" if ev == "run" else "submit")
            v.jobs[job] = {"id": job, "kind": kind, "what": str(row.get("label") or row.get("policy") or "")[:160],
                           "t": row.get("t")}
        elif ev == "finished" and row.get("background"):
            v.jobs.pop(job, None)
            v.bg_s += float(row.get("took_s") or 0.0)
            v.bg_done += 1

    # -- transcripts
    def _fold(self, v: AgentView, row: dict[str, Any]) -> None:
        t = row.get("t")
        kind = row.get("_type", "")
        v.last_t, v.last_kind = t, kind
        v.started = v.started or t
        if kind == "AssistantMessage":
            v.estimate += self.cfg.prices.generated_cost(row.get("content"))
            u = row.get("usage") or {}
            mid = row.get("message_id")
            if u and mid not in v.seen:
                if mid:
                    v.seen.add(mid)
                v.estimate += self.cfg.prices.cost(u)
            for b in row.get("content") or []:
                if "name" in b and "input" in b:
                    call = {"t": t, "tool": b["name"], "what": _summarise_call(b["name"], b["input"]),
                            "background": bool(b["input"].get("run_in_background")), "done": False}
                    v.calls.append(call)
                    v.open_calls[b.get("id", "")] = call
                elif "text" in b and b["text"].strip():
                    v.last_text, v.last_text_t = b["text"], t
        elif kind == "UserMessage":
            for b in row.get("content") or []:
                if isinstance(b, dict) and "tool_use_id" in b:
                    call = v.open_calls.pop(b["tool_use_id"], None)
                    if call:
                        call["done"] = True
                        call["error"] = bool(b.get("is_error"))
                        took = (t or 0) - (call.get("t") or t or 0)
                        if _is_lab(call) and took >= BLOCKED_AFTER_S:
                            v.blocked_s += took
        elif kind == "ResultMessage":
            v.turns += 1
            v.running = row.get("total_cost_usd") or v.running
            v.idle_until = None
        elif kind in ("ConversationResetMessage", "harness_error"):
            v.banked, v.running = v.banked + v.running, 0.0
            if kind == "harness_error":
                v.errors.append(str(row.get("error", ""))[:300])
        elif kind == "harness_idle_wait":
            v.idle_waits += 1
            v.idle_until = (t or time.time()) + float(row.get("seconds", 0))
        elif kind == "agent_end":
            v.ended = row.get("stop_reason") or "ended"

    def refresh(self) -> None:
        for i in range(self.cfg.n_agents):
            name = self.cfg.agent_name(i)
            v = self.views[name]
            _, private = slot_paths(self.cfg, i)
            for fname, attr in (("transcript.jsonl", "offset"), ("audit.jsonl", "audit_offset"),
                                ("lab-jobs.jsonl", "jobs_offset")):
                f = private / fname
                if not f.exists():
                    continue
                with f.open("rb") as fh:  # byte offsets: text-mode tell() lies about multibyte lines
                    pos = getattr(v, attr)
                    fh.seek(pos)
                    while True:
                        line = fh.readline()
                        if not line.endswith(b"\n"):
                            break  # EOF, or a line still being written: take it next time
                        pos += len(line)
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if attr == "offset":
                            self._fold(v, row)
                        elif attr == "jobs_offset":
                            self._fold_job(v, row)
                        elif row.get("outside"):
                            v.outside.append(row)
                    setattr(v, attr, pos)
        now = time.time()
        if now - self.board_cache["t"] > 5 and self.creds:
            self._refresh_board()

    def _refresh_board(self) -> None:
        cred = next(iter(self.creds.values()))
        names = {c.identity: n for n, c in self.creds.items()}
        if self.gate:
            names[self.gate.identity] = "GATE"
        try:
            envs = board._request(f"{cred.url}/read?ns={self.cfg.board.ns}&since=-1&limit=1000", cred.token)["envelopes"]
            posts, gate = [], []
            for e in envs:
                p = e.get("payload")
                who = names.get(e["author"], e["author"][-6:])
                if isinstance(p, dict) and p.get("kind") == "gate-result":
                    gate.append({"id": e["id"], "ts": e["ts"], "agent": p.get("agent"), "policy": p.get("policy"),
                                 "score": p.get("score", p.get("mean")), "ci95": p.get("ci95"),
                                 "confirmed": p.get("confirmed"), "ends": p.get("ends")})
                    continue
                text = p if isinstance(p, str) else (p.get("text") if isinstance(p, dict) and "text" in p else json.dumps(p))
                posts.append({"id": e["id"], "ts": e["ts"], "who": who, "type": e["type"],
                              "refs": [f"{r['edge']} #{r['id']}" for r in e["refs"]],
                              "text": (text or "")[:1200], "evidence": e.get("evidence")})
            self.board_cache = {"posts": posts, "gate": gate, "error": None, "t": time.time()}
        except Exception as ex:  # the page shows the error; it never crashes the panel
            self.board_cache.update(error=f"{type(ex).__name__}: {ex}", t=time.time())

    def state(self) -> dict[str, Any]:
        with self.lock:
            self.refresh()
            now = time.time()
            agents = []
            for name, v in self.views.items():
                spent = max(v.banked + v.running, v.estimate)
                pending = [c for c in v.open_calls.values()]
                stop = (v.last_t or now) if v.ended else now  # an ended agent's unanswered call stopped with it
                if v.ended:
                    status, detail = "ended", v.ended
                elif v.last_t is None:
                    status, detail = "not started", ""
                elif v.idle_until and now < v.idle_until:
                    status, detail = "idle", f"waiting for board activity, up to {int(v.idle_until - now)}s more"
                elif pending and any(_long_sleep(c) for c in pending):
                    # an in-turn sleep is idle time the harness's idle-wait count never sees
                    c = next(c for c in reversed(pending) if _long_sleep(c))
                    status = "sleeping"
                    detail = f"in-turn sleep for {int(now - c['t'])}s: {c['what'][:100]}"
                elif pending and any(_is_lab(c) and now - (c["t"] or now) >= BLOCKED_AFTER_S for c in pending):
                    # blocked in a lab call: a synchronous submit or run holds the agent's whole turn until it returns
                    c = next(c for c in reversed(pending) if _is_lab(c) and now - (c["t"] or now) >= BLOCKED_AFTER_S)
                    status = "blocked"
                    detail = f"in {c['tool'].removeprefix('mcp__lab__')} for {int((now - c['t']) / 60)} min: {c['what'][:100]}"
                elif pending:
                    c = pending[-1]
                    status = "waiting on tool"
                    detail = f"{c['tool']}: {c['what'][:120]}"
                elif now - (v.last_t or now) > 600:
                    status, detail = "quiet", "no activity for 10+ minutes"
                else:
                    status, detail = "working", ""
                agents.append({
                    "name": name, "identity": self.creds[name].identity if name in self.creds else None,
                    "status": status, "detail": detail, "spent": round(spent, 3),
                    "budget": self.cfg.per_agent_budget_usd, "turns": v.turns, "idle_waits": v.idle_waits,
                    "blocked_min": round((v.blocked_s + sum(stop - c["t"] for c in pending if _is_lab(c) and c.get("t")
                                                            and stop - c["t"] >= BLOCKED_AFTER_S)) / 60, 1),
                    "jobs": [{**j, "age": round(stop - (j["t"] or stop))} for j in v.jobs.values()] if not v.ended else [],
                    "bg_min": round((v.bg_s + sum(stop - (j["t"] or stop) for j in v.jobs.values())) / 60, 1),
                    "bg_done": v.bg_done,
                    "since_last": round(now - v.last_t) if v.last_t else None,
                    "calls": v.calls[-14:][::-1], "n_calls": len(v.calls),
                    "last_text": v.last_text[:1500], "last_text_age": round(now - v.last_text_t) if v.last_text_t else None,
                    "errors": v.errors[-3:], "outside": v.outside[-8:][::-1], "n_outside": len(v.outside),
                })
            gate = self.board_cache["gate"]
            pick = max if self.cfg.gate.higher_is_better else min
            best = pick((g for g in gate if g.get("score") is not None and g.get("confirmed") is not False),
                        key=lambda g: g["score"], default=None)
            return {
                "run": self.cfg.run_name, "model": self.cfg.model, "ns": self.cfg.board.ns, "now": now,
                "budget_total": self.cfg.total_budget_usd, "spent_total": round(sum(a["spent"] for a in agents), 3),
                "wall_hours": self.cfg.max_wall_hours,
                "started": min((v.started for v in self.views.values() if v.started), default=None),
                "agents": agents, "posts": self.board_cache["posts"][::-1][:80], "gate": gate[::-1],
                "best": best, "board_error": self.board_cache["error"],
            }


def serve(cfg: RunConfig, port: int) -> None:
    panel = Panel(cfg)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/api/state"):
                try:
                    self._send(200, json.dumps(panel.state()).encode(), "application/json")
                except Exception as ex:
                    self._send(500, json.dumps({"error": f"{type(ex).__name__}: {ex}"}).encode(), "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"swarm panel for {cfg.run_name}: http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
