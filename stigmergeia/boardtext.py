"""One-line renderings of board envelopes, shared by the harness (the in-turn
board pulse, the continue digest) and the agent's shell (`korax feed`/`show`).

STDLIB ONLY and no package-relative imports: `shellbin/korax_helper.py` runs
inside the agent's sandbox with the system python3 and imports this file by
path, outside any venv.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

LINE_CHARS = 200


def short_name(display: str) -> str:
    """`<run>-a03` -> `a03`, `<run>-gate` -> `GATE`. Displays are minted as
    `<run>-<agent>`; anything else is returned whole."""
    tail = display.rsplit("-", 1)[-1] if display else display
    if tail == "gate":
        return "GATE"
    if len(tail) >= 2 and tail[0] == "a" and tail[1:].isdigit():
        return tail
    return display


def names_from_identities(body: Any) -> dict[str, str]:
    """identity id -> short name, from a `/identities` reply (tolerant of shape)."""
    rows: Iterable[Any] = []
    if isinstance(body, dict):
        rows = body.get("identities") or body.get("bands") or []
    elif isinstance(body, list):
        rows = body
    out: dict[str, str] = {}
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            out[r["id"]] = short_name(str(r.get("display") or r["id"]))
    return out


def gate_line(p: dict[str, Any]) -> str:
    tag = ("NEW RECORD" if p.get("record") else "ties the record" if p.get("tie")
           else "confirmed" if p.get("confirmed") else "UNCONFIRMED" if "confirmed" in p else "one batch")
    code = f"; code {p['code']}" if p.get("code") else ""
    return f"{p.get('agent', '?')} {p.get('policy', '')}: {p.get('score', p.get('mean'))} ({tag}){code}"


def payload_text(payload: Any) -> str:
    """The human text of a payload: a string as is, `text` of an object, a
    gate result as one sentence, anything else as compact JSON."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        if payload.get("kind") == "gate-result":
            return gate_line(payload)
        if isinstance(payload.get("text"), str):
            return payload["text"]
    return json.dumps(payload, separators=(",", ":"), default=str)


def squash(text: str, chars: int = LINE_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= chars else flat[: chars - 1] + "…"


def edges(refs: Any) -> str:
    """`[derives-from #12, replies #9]`, or '' when there are none."""
    out = []
    for r in refs or []:
        if isinstance(r, dict) and "id" in r:
            out.append(f"{r.get('edge') or r.get('rel') or 'ref'} #{r['id']}")
    return f" [{', '.join(out)}]" if out else ""


def mentions(env: dict[str, Any]) -> list[str]:
    node: Any = env.get("ext")
    for key in ("korax", "mentions"):
        node = node.get(key) if isinstance(node, dict) else None
    return [m for m in node if isinstance(m, str)] if isinstance(node, list) else []


def who(identity: str, names: dict[str, str], me: str | None = None) -> str:
    if me and identity == me:
        return "you"
    return names.get(identity) or (identity[-6:] if identity else "?")


def one_line(env: dict[str, Any], names: dict[str, str], me: str | None = None,
             chars: int = LINE_CHARS) -> str:
    """`#id aNN TYPE [edges]: first ~200 chars of the text`."""
    text = payload_text(env.get("payload"))
    if not text and env.get("payload_bytes"):
        text = f"({env['payload_bytes']} bytes: korax show {env.get('id')})"
    ment = mentions(env)
    mtag = f" @{','.join(who(m, names, me) for m in ment)}" if ment else ""
    return (f"#{env.get('id')} {who(str(env.get('author', '')), names, me)} {env.get('type', '?')}"
            f"{edges(env.get('refs'))}{mtag}: {squash(text, chars)}")
