"""Board plumbing: mint one identity per agent, plus the gate's, and post.

Stdlib HTTP only. Credentials land in each agent's PRIVATE dir (mode 0600),
which is outside the agent's workspace, so no agent tool can read them —
only the harness does, to hand them to that agent's Korax MCP process.
"""

from __future__ import annotations

import json
import os
import http.client
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

PROTO = "korax/0.1"


class Credential(BaseModel):
    model_config = ConfigDict(frozen=True)
    url: str
    identity: str
    token: str
    display: str


class BoardError(RuntimeError):
    pass


def _request(url: str, token: str, method: str = "GET", body: dict[str, Any] | None = None,
             timeout: float = 30.0) -> dict[str, Any]:
    """http.client directly: urllib builds an HTTPS handler (and an SSL
    context) even for plain http, and that construction has failed inside
    worker threads here. TLS is used only for https URLs."""
    u = urlsplit(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.hostname or "", u.port, timeout=timeout)
    path = u.path + (f"?{u.query}" if u.query else "")
    data = json.dumps(body).encode() if body is not None else None
    try:
        conn.request(method, path or "/", body=data, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException) as e:
        raise BoardError(f"{method} {url}: {type(e).__name__}: {e}") from e
    finally:
        conn.close()
    if resp.status >= 400:
        raise BoardError(f"{method} {url} -> {resp.status}: {raw.decode(errors='replace')[:500]}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise BoardError(f"{method} {url}: non-JSON reply {raw[:200]!r}") from e


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def mint(url: str, operator_token: str, display: str, dest: Path) -> Credential:
    """Mint `display` on the board, or reuse the credential already saved at
    `dest` — provisioning is re-runnable and never mints twice."""
    if dest.is_file():
        cred = Credential.model_validate_json(dest.read_text())
        if cred.url != url:
            raise BoardError(f"{dest} belongs to {cred.url}, not {url}")
        return cred
    out = _request(f"{url}/identity", operator_token, "POST", {"display": display})
    cred = Credential(url=url, identity=out["id"], token=out["token"], display=display)
    write_private(dest, cred.model_dump_json(indent=2) + "\n")
    return cred


def post(cred: Credential, ns: str, act: str, payload: dict[str, Any],
         refs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return _request(f"{cred.url}/post", cred.token, "POST", {
        "proto": PROTO, "author": cred.identity, "ns": ns, "type": act, "grade": "n/a",
        "refs": refs or [], "payload": payload, "ext": {}})


def whoami(url: str, token: str) -> dict[str, Any]:
    return _request(f"{url}/whoami", token)


def read_since(cred: Credential, ns: str, since: int, limit: int = 1000) -> list[dict[str, Any]]:
    """Envelopes in `ns` with id > since, oldest first (summaries only: no prose)."""
    out = _request(f"{cred.url}/read?ns={ns}&since={since}&limit={limit}&summary=true", cred.token)
    return out["envelopes"]
