"""Stand up a swarm board: init, open /swarm to every band, seed the canon.

  python bootstrap/board.py init  --db DIR/board.db      # mint board + operator token file
  korax-server serve --db DIR/board.db --port 7440       # (run it however you run servers)
  python bootstrap/board.py setup --url http://127.0.0.1:7440 --token-file DIR/operator.token \
                                  [--canon DIR] [--rakes <board-dump>.jsonl]

`setup` posts and pins the canon documents from the repository's canon/
directory unless --canon names another. It is idempotent: the grant is
skipped when already in force, an unchanged canon document is not posted
again, and rakes already seeded (matched by origin id) are not posted twice.

Rake seeding is opt-in and off by default: only with --rakes, a JSONL dump
of another Korax board, does setup copy that board's live rakes into the
rakes namespace. They arrive as a searchable corpus, not pushed into any
agent's prompt: each keeps its text and names the board and envelope id it
came from. Edges between rakes are dropped (their ids do not exist here);
the origin id keeps the provenance.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROTO = "korax/0.1"
CANON_DIR = Path(__file__).resolve().parent.parent / "canon"


def req(url: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {url} -> {e.code}: {e.read().decode(errors='replace')[:400]}")


def cmd_init(a: argparse.Namespace) -> int:
    db = Path(a.db)
    if db.exists():
        print(f"{db} exists; not re-initialising", file=sys.stderr)
        return 1
    db.parent.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(["korax-server", "init", "--db", str(db), "--display", "swarm-operator"],
                         capture_output=True, text=True)
    if out.returncode:
        print(out.stderr, file=sys.stderr)
        return out.returncode
    m = re.search(r"^token:\s+(\S+)", out.stdout, re.M)
    if not m:
        print("init succeeded but no token line was found; the board is unusable", file=sys.stderr)
        return 1
    tok = db.parent / "operator.token"
    fd = os.open(tok, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(m.group(1) + "\n")
    print(re.sub(r"^token:.*$", f"token:    (saved to {tok}, mode 0600)", out.stdout, flags=re.M))
    return 0


def cmd_setup(a: argparse.Namespace) -> int:
    url = a.url.rstrip("/")
    token = Path(a.token_file).read_text().strip()
    me = req(f"{url}/whoami", token)["identity"]

    pol = req(f"{url}/policy?ns=/swarm/x", token)
    in_force = json.dumps(pol)
    if '"band:*"' in in_force and "/swarm/**" in in_force:
        print("grant band:* claimant /swarm/** already in force")
    else:
        r = subprocess.run(["korax", "--url", url, "--token", token, "grant", "--ns", "/swarm/**",
                            "band:*", "claimant"], capture_output=True, text=True)
        if r.returncode:
            print(r.stderr, file=sys.stderr)
            return r.returncode
        print("granted band:* claimant on /swarm/**")

    # NOTE in /swarm: the charter's act for 'whenever you just want to say something', which
    # the root policy does not permit. A /swarm nest POLICY copies the root's rules plus NOTE and
    # carries no grants of its own (grants still come from the root), so permissions are unchanged.
    swarm_pol = req(f"{url}/policy?ns=/swarm/x", token)
    if "NOTE" in swarm_pol["payload"].get("acts", []):
        print("NOTE already permitted in /swarm")
    else:
        pol = dict(swarm_pol["payload"])
        pol["acts"] = sorted(set(pol.get("acts", [])) | {"NOTE"})
        pol["grants"] = []
        r = req(f"{url}/post", token, "POST", {"proto": PROTO, "author": me, "ns": "/swarm", "type": "POLICY",
                                               "grade": "n/a", "refs": [], "payload": pol, "ext": {}})
        print(f"permitted NOTE in /swarm (POLICY #{r['id']})")

    if a.canon:
        seed_canon(url, token, me, Path(a.canon))
    if not a.rakes:
        return 0
    seeded = set()
    since = -1
    while True:
        page = req(f"{url}/read?ns=/commons/rakes&since={since}&limit=500", token)
        for e in page["envelopes"]:
            p = e.get("payload")
            if isinstance(p, dict) and "origin_id" in p:
                seeded.add(p["origin_id"])
        if not page["envelopes"]:
            break
        since = page["cursor"]

    envs = [json.loads(line) for line in Path(a.rakes).read_text().splitlines()]
    dead = {r["id"] for e in envs for r in e["refs"] if r["edge"] in ("supersedes", "invalidates")}
    rakes = [e for e in envs if e["ns"].startswith("/commons/rakes") and e["type"] in ("FINDING", "WARN")
             and e["id"] not in dead and e["id"] not in seeded]
    for n, e in enumerate(rakes, 1):
        p = e.get("payload")
        text = p if isinstance(p, str) else json.dumps(p)
        req(f"{url}/post", token, "POST", {
            "proto": PROTO, "author": me, "ns": "/commons/rakes", "type": e["type"], "grade": "n/a",
            "refs": [], "ext": {},
            "payload": {"text": text, "origin": "korax main board", "origin_id": e["id"],
                        "origin_ts": e["ts"], "origin_ns": e["ns"]}})
        if n % 100 == 0:
            print(f"  seeded {n}/{len(rakes)}")
    print(f"rakes: {len(rakes)} seeded, {len(seeded)} already present")
    return 0


def _all(url: str, token: str, ns: str) -> list[dict]:
    out, since = [], -1
    while True:
        page = req(f"{url}/read?ns={ns}&since={since}&limit=500", token)
        if not page["envelopes"]:
            return out
        out += page["envelopes"]
        since = page["cursor"]


def seed_canon(url: str, token: str, me: str, canon_dir: Path) -> None:
    """Post each canon document as a FINDING in the board's canon namespace
    and pin it {class: canon} — Korax's own canon mechanism, so `korax_onboard`
    serves it and acks track it. Re-runnable: an unchanged document is
    skipped; a changed one is posted as a SUPERSEDING version, which the
    existing pin follows (current_version walks supersedes) and which
    voids old acks, as a canon change should."""
    import hashlib
    envs = _all(url, token, "/korax/canon")
    superseded = {r["id"] for e in envs for r in e["refs"] if r["edge"] == "supersedes"}
    docs = {}
    for e in envs:
        p = e.get("payload")
        if e["type"] == "FINDING" and isinstance(p, dict) and "swarm_canon" in p and e["id"] not in superseded:
            docs[p["swarm_canon"]] = e
    for f in sorted(canon_dir.glob("*.md")):
        text = f.read_text()
        sha = hashlib.sha256(text.encode()).hexdigest()
        cur = docs.get(f.name)
        if cur and cur["payload"].get("sha256") == sha:
            print(f"canon {f.name}: unchanged (#{cur['id']})")
            continue
        refs = [{"edge": "supersedes", "id": cur["id"]}] if cur else []
        doc = req(f"{url}/post", token, "POST", {
            "proto": PROTO, "author": me, "ns": "/korax/canon", "type": "FINDING", "grade": "verified",
            "refs": refs, "ext": {}, "payload": {"swarm_canon": f.name, "sha256": sha, "text": text}})
        if cur:
            print(f"canon {f.name}: superseded #{cur['id']} with #{doc['id']} (its pin follows)")
            continue
        pin = req(f"{url}/post", token, "POST", {
            "proto": PROTO, "author": me, "ns": "/korax/canon", "type": "PIN", "grade": "n/a",
            "refs": [{"edge": "pins", "id": doc["id"]}], "ext": {}, "payload": {"class": "canon"}})
        print(f"canon {f.name}: posted #{doc['id']}, pinned by #{pin['id']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--db", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--url", required=True)
    s.add_argument("--token-file", required=True)
    s.add_argument("--rakes", default=None,
                   help="opt-in: a JSONL dump of another Korax board to seed /commons/rakes from (default: no rakes)")
    s.add_argument("--canon", default=str(CANON_DIR),
                   help="directory of canon documents (*.md) to post and pin in /korax/canon (default: the "
                        "repository's canon/; an empty string skips the canon)")
    a = ap.parse_args()
    return cmd_init(a) if a.cmd == "init" else cmd_setup(a)


if __name__ == "__main__":
    sys.exit(main())
