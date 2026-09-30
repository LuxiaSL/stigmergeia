"""The agent's readable board, from the shell: `korax feed` and `korax show`,
plus a forgiving `korax post --mention A B`. Called by shellbin/korax.

Runs INSIDE the agent's sandbox with the system python3 (stdlib only), as
the agent: KORAX_URL / KORAX_TOKEN / KORAX_IDENTITY come from the shell
environment the harness set, and HTTP goes wherever the real CLI's would
(the Claude sandbox's proxy via HTTP_PROXY, or the Codex sandbox's loopback
forwarder).

Why it exists: `read --summary` drops the text, which leaves agents grepping
raw JSON to read the board; agents reach for `korax show <id>` by that name;
and the real CLI refuses `--mention band:A band:B` with "the envelope argument
is not valid JSON" (argparse takes the second band as the positional JSON
envelope).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boardtext  # noqa: E402  (stdlib-only module shared with the harness)

TIMEOUT_S = 30.0


class HelperError(Exception):
    pass


def _get(path: str) -> dict[str, Any]:
    base = os.environ.get("KORAX_URL", "").rstrip("/")
    token = os.environ.get("KORAX_TOKEN", "")
    if not base or not token:
        raise HelperError("KORAX_URL / KORAX_TOKEN are not set in this shell")
    req = urllib.request.Request(f"{base}{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        raise HelperError(f"GET {path} -> {e.code}: {body}") from e
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise HelperError(f"GET {path}: {type(e).__name__}: {e}") from e


def _names() -> dict[str, str]:
    try:
        return boardtext.names_from_identities(_get("/identities"))
    except HelperError:
        return {}  # lines fall back to the id's tail


def _read_all(ns: str, since: int, summary: bool, limit: int = 1000, max_pages: int = 50) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur = since
    for _ in range(max_pages):
        q = urllib.parse.urlencode({"ns": ns, "since": cur, "limit": limit, "include_self": "true",
                                    **({"summary": "true"} if summary else {})})
        body = _get(f"/read?{q}")
        envs = body.get("envelopes") or []
        out += envs
        nxt = body.get("cursor", cur)
        if len(envs) < limit or not isinstance(nxt, int) or nxt <= cur:
            break
        cur = nxt
    return out


def feed_lines(envs: list[dict[str, Any]], names: dict[str, str], me: str | None, chars: int) -> list[str]:
    return [boardtext.one_line(e, names, me, chars) for e in envs]


def cmd_feed(a: argparse.Namespace) -> int:
    me = os.environ.get("KORAX_IDENTITY")
    names = _names()
    older = newer = 0
    if a.for_me:
        if not me:
            raise HelperError("KORAX_IDENTITY is not set in this shell")
        body = _get(f"/feed?{urllib.parse.urlencode({'since': a.since if a.since is not None else -1, 'timeout': 0})}")
        envs = [e for e in body.get("envelopes") or [] if e.get("author") != me]
        where = "for you (DMs, mentions, replies to your posts)"
        older, envs = max(0, len(envs) - a.limit), envs[-a.limit:]
    else:
        ns = a.ns or os.environ.get("SWARM_NS")
        if not ns:
            raise HelperError("no namespace: pass --ns /swarm/<run>")
        where = f"in {ns}"
        if a.since is None:
            # the newest `limit`: ids first (structure only, cheap), then the text of just those
            ids = [e["id"] for e in _read_all(ns, -1, summary=True)]
            start = ids[-a.limit - 1] if len(ids) > a.limit else -1
            older = max(0, len(ids) - a.limit)
            envs = _read_all(ns, start, summary=False, limit=a.limit, max_pages=1)[: a.limit]
        else:
            envs = _read_all(ns, a.since, summary=False, limit=a.limit + 1, max_pages=1)
            newer, envs = max(0, len(envs) - a.limit), envs[: a.limit]
    if not envs:
        print(f"(no posts {where}" + (f" after #{a.since})" if a.since is not None else ")"))
        return 0
    for line in feed_lines(envs, names, me, a.chars):
        print(line)
    if newer:
        print(f"(more after #{envs[-1]['id']}: korax feed --since {envs[-1]['id']})")
    elif older:
        print(f"({older} older not shown: --since <id> reads forward from any id)")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    body = _get(f"/envelope/{int(str(a.id).lstrip('#'))}")
    env = body.get("envelope") or body
    names = _names()
    me = os.environ.get("KORAX_IDENTITY")
    who = boardtext.who(str(env.get("author", "")), names, me)
    ment = boardtext.mentions(env)
    print(f"#{env.get('id')} {who} ({env.get('author')}) {env.get('type')} in {env.get('ns')}"
          f"{boardtext.edges(env.get('refs'))}")
    if ment:
        print("mentions: " + ", ".join(f"{boardtext.who(m, names, me)} ({m})" for m in ment))
    if env.get("evidence"):
        print(f"evidence: {env['evidence']}")
    print()
    p = env.get("payload")
    if isinstance(p, dict) and not (p.get("kind") == "gate-result" or isinstance(p.get("text"), str)):
        print(json.dumps(p, indent=2, default=str))
    else:
        if isinstance(p, dict) and p.get("kind") == "gate-result":
            print(boardtext.payload_text(p))
            print(json.dumps(p, indent=2, default=str))
        else:
            print(boardtext.payload_text(p))
    return 0


def normalize_post_args(argv: list[str]) -> list[str]:
    """`--mention band:A band:B` -> `--mention band:A --mention band:B` (also
    `--mention band:A,band:B`). A bare `band:` word can never be the JSON
    envelope positional, so this changes nothing a correct command meant."""
    out: list[str] = []
    in_mentions = False
    for tok in argv:
        if tok == "--mention":
            in_mentions = True
            out.append(tok)
            continue
        if tok.startswith("--mention="):
            tok_vals = tok.split("=", 1)[1]
            for v in [x for x in tok_vals.split(",") if x]:
                out += ["--mention", v]
            in_mentions = True
            continue
        if in_mentions and tok.startswith("band:"):
            vals = [x for x in tok.split(",") if x]
            if out and out[-1] == "--mention":
                out.append(vals[0])
                vals = vals[1:]
            for v in vals:
                out += ["--mention", v]
            continue
        in_mentions = False
        out.append(tok)
    return out


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("post", "caw"):
        real = os.path.join(os.environ.get("KORAX_REAL_BIN", ""), "korax")
        fixed = [argv[0], *normalize_post_args(argv[1:])]
        os.execv(real, [real, *fixed])
    ap = argparse.ArgumentParser(prog="korax", description="readable board views (swarm shell)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("feed", help="one line per post: #id author TYPE [edges] @mentions: text")
    f.add_argument("--ns", help="namespace (default: this run's, $SWARM_NS)")
    f.add_argument("--since", type=lambda s: int(s.lstrip("#")), help="only posts after this id, oldest first")
    f.add_argument("--limit", type=int, default=20, help="lines (default 20)")
    f.add_argument("--chars", type=int, default=boardtext.LINE_CHARS, help="text characters per line (default 200)")
    f.add_argument("--for-me", action="store_true", help="DMs to you, posts mentioning you, replies to your posts")
    f.set_defaults(func=cmd_feed)
    s = sub.add_parser("show", help="one post in full, readable")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)
    a = ap.parse_args(argv)
    if getattr(a, "limit", 1) < 1:
        ap.error("--limit must be at least 1")
    try:
        return a.func(a)
    except (HelperError, ValueError) as e:
        print(f"korax {a.cmd}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
