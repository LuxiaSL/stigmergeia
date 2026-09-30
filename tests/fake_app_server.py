#!/usr/bin/env python3
"""A scripted stand-in for `codex app-server --listen stdio://`.

Speaks the wire protocol (one JSON object per line) closely enough to drive
CodexAgent: initialize, thread/start, turn/start, turn/interrupt. What a turn
does is chosen by FAKE_SCENARIO; everything the server receives (requests and
responses to its own requests) is appended to FAKE_LOG as JSON lines.
"""
import json
import os
import sys
import threading

LOG = os.environ.get("FAKE_LOG")
SCENARIO = os.environ.get("FAKE_SCENARIO", "tools")
lock = threading.Lock()
pending: dict = {}
next_id = [1000]
interrupted = threading.Event()


def log(obj):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(obj) + "\n")


def send(obj):
    with lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def request(method, params):
    """Send a server->client request and wait for the answer."""
    rid = next_id[0]
    next_id[0] += 1
    ev = threading.Event()
    pending[rid] = [ev, None]
    send({"id": rid, "method": method, "params": params})
    ev.wait(30)
    return pending.pop(rid)[1]


def note(method, params):
    send({"method": method, "params": params})


def usage(total_in, cached, out):
    b = {"inputTokens": total_in, "cachedInputTokens": cached, "outputTokens": out, "reasoningOutputTokens": 0,
         "totalTokens": total_in + out, "cacheWriteInputTokens": 0}
    note("thread/tokenUsage/updated", {"threadId": "th1", "turnId": "tu1", "tokenUsage": {"total": b, "last": b}})


def run_turn(ws):
    if SCENARIO == "tools":
        r1 = request("item/tool/call", {"threadId": "th1", "turnId": "tu1", "callId": "c1", "tool": "Write",
                                        "arguments": {"file_path": "hello.txt", "content": "hi"}})
        log({"tool_result": "c1", "result": r1})
        r2 = request("item/tool/call", {"threadId": "th1", "turnId": "tu1", "callId": "c2", "tool": "Write",
                                        "arguments": {"file_path": "/etc/evil.txt", "content": "x"}})
        log({"tool_result": "c2", "result": r2})
        r3 = request("item/tool/call", {"threadId": "th1", "turnId": "tu1", "callId": "c3", "tool": "korax_post",
                                        "namespace": "korax",
                                        "arguments": {"ns": "/swarm/x", "type": "WARN", "payload": "no effect"}})
        log({"tool_result": "c3", "result": r3})
        r4 = request("item/commandExecution/requestApproval", {"threadId": "th1", "turnId": "tu1", "itemId": "i"})
        log({"approval": r4})
        r5 = request("currentTime/read", {})
        log({"clock": r5})
        usage(1000, 400, 50)
        usage(3000, 2400, 120)
        note("item/completed", {"threadId": "th1", "turnId": "tu1",
                                "item": {"type": "agentMessage", "id": "m1", "text": "done", "phase": "final_answer"}})
        note("turn/completed", {"threadId": "th1", "turn": {"id": "tu1", "items": [], "status": "completed"}})
    elif SCENARIO == "hang":
        usage(10, 0, 1)
        interrupted.wait(60)
        note("turn/completed", {"threadId": "th1", "turn": {"id": "tu1", "items": [], "status": "interrupted"}})
    elif SCENARIO == "leak":
        note("item/completed", {"threadId": "th1", "turnId": "tu1",
                                "item": {"type": "commandExecution", "id": "x", "command": "ls", "status": "completed"}})
        interrupted.wait(10)
        note("turn/completed", {"threadId": "th1", "turn": {"id": "tu1", "items": [], "status": "interrupted"}})
    elif SCENARIO == "budget":
        usage(10_000_000, 0, 1_000_000)
        interrupted.wait(10)
        note("turn/completed", {"threadId": "th1", "turn": {"id": "tu1", "items": [], "status": "interrupted"}})


def main():
    log({"argv": sys.argv[1:], "codex_home": os.environ.get("CODEX_HOME")})
    ws = os.getcwd()
    for line in sys.stdin:
        msg = json.loads(line)
        if "method" not in msg:  # a response to one of our requests
            entry = pending.get(msg.get("id"))
            if entry:
                entry[1] = msg
                entry[0].set()
            continue
        log({"recv": msg})
        m, rid = msg["method"], msg.get("id")
        if m == "initialize":
            send({"id": rid, "result": {"userAgent": "fake/0.0"}})
        elif m == "thread/start":
            send({"id": rid, "result": {"thread": {"id": "th1"}}})
        elif m == "thread/resume":
            send({"id": rid, "result": {"thread": {"id": "th1"}}})
        elif m == "turn/start":
            send({"id": rid, "result": {"turn": {"id": "tu1", "items": [], "status": "inProgress"}}})
            threading.Thread(target=run_turn, args=(ws,), daemon=True).start()
        elif m == "turn/interrupt":
            interrupted.set()
            send({"id": rid, "result": {}})
        elif rid is not None:
            send({"id": rid, "result": {}})


if __name__ == "__main__":
    main()
