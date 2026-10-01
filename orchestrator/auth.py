"""GitHub OAuth login + per-user app tenancy for CodeGhost.

Flow: /auth/login → GitHub authorize → /auth/callback exchanges the code →
session cookie. On /apps the user picks a repo and gets a tenant id +
shared secret; crashes posted with that tenant are verified against the
tenant's secret and delivered with the tenant's token to the tenant's repo.

Tokens live in orchestrator/data/tenants.json (gitignored) — hackathon
scope: single-machine deployment, plaintext at rest.
"""

import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

DATA_DIR = Path(os.getenv("CODEGHOST_DATA_DIR", Path(__file__).parent / "data"))
TENANTS_FILE = DATA_DIR / "tenants.json"

CLIENT_ID = os.getenv("GITHUB_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "")
SCOPE = "repo read:user"

SESSIONS: dict[str, dict] = {}
TENANTS: dict[str, dict] = {}
STATES: set[str] = set()

_lock = threading.Lock()


def _load_tenants() -> None:
    global TENANTS
    try:
        TENANTS = json.loads(TENANTS_FILE.read_text())
    except Exception:
        TENANTS = {}


def _save_tenants() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TENANTS_FILE.write_text(json.dumps(TENANTS, indent=2))
    except Exception:
        pass


_load_tenants()


def oauth_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def authorize_url(redirect_uri: str) -> str:
    state = secrets.token_hex(8)
    with _lock:
        STATES.add(state)
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "allow_signup": "true",
    })
    return "https://github.com/login/oauth/authorize?" + q


def consume_state(state: str) -> bool:
    with _lock:
        if state in STATES:
            STATES.discard(state)
            return True
    return False


async def exchange_code(code: str, redirect_uri: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://github.com/login/oauth/access_token",
                json={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            return r.json().get("access_token")
    except Exception:
        return None


def github_get(token: str, path: str) -> dict:
    r = httpx.get(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def create_session(login: str, token: str, avatar: str) -> str:
    sid = secrets.token_hex(16)
    SESSIONS[sid] = {"login": login, "token": token, "avatar": avatar,
                     "at": time.time()}
    return sid


def get_session(sid: str | None) -> dict | None:
    return SESSIONS.get(sid) if sid else None


def drop_session(sid: str | None) -> None:
    SESSIONS.pop(sid, None)


def create_tenant(login: str, token: str, repo: str, prefix: str = "") -> dict:
    tid = secrets.token_hex(6)
    secret = secrets.token_hex(24)
    prefix = prefix.strip().strip("/")
    if prefix:
        prefix += "/"
    with _lock:
        TENANTS[tid] = {
            "tid": tid,
            "login": login,
            "token": token,
            "repo": repo,
            "prefix": prefix,
            "secret": secret,
            "created": time.time(),
        }
        _save_tenants()
    return TENANTS[tid]


def tenants_of(login: str) -> list[dict]:
    return [t for t in TENANTS.values() if t.get("login") == login]


def get_tenant(tid: str | None) -> dict | None:
    return TENANTS.get(tid) if tid else None


def dev_login() -> dict | None:
    """Offline identity: sign in as the server's own .env PAT holder."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return None
    try:
        user = github_get(token, "/user")
    except Exception:
        return None
    return {"login": user.get("login", "unknown"),
            "token": token,
            "avatar": user.get("avatar_url", "")}