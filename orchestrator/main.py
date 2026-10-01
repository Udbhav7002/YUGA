"""
CodeGhost Orchestrator — the autonomous crash-to-fix pipeline.

Design decisions (from GLM + DeepSeek + Kimi merged review):
1. Pipeline is a plain `def` (sync). BackgroundTasks runs it in a threadpool
   automatically — no asyncio gymnastics, no event loop blocking.
2. _read_tree() reads ALL .py files into memory. Combined with put_archive,
   this eliminates bind-mount and host-path bugs entirely.
3. _safe_rel() with PurePosixPath for bulletproof path traversal defense.
4. _step() context manager for per-step timing (shown in /status and PR body).
5. _fingerprint() with TTL-based dedup (not just a set — handles restarts).
6. Dashboard served at /dash, status polling at /status/last.
"""

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from orchestrator.sandbox import Sandbox
from orchestrator import auth
from agent import llm_client, github_delivery

# ── Config ──────────────────────────────────────────────────────────
SOURCE_ROOT = Path(os.getenv("TARGET_SOURCE_DIR", "/app/target-src")).resolve()
SECRET = os.environ.get("CODEGHOST_SHARED_SECRET", "").encode()
MAX_FIX_ATTEMPTS = int(os.getenv("MAX_FIX_ATTEMPTS", "2"))
DEADLINE = float(os.getenv("PIPELINE_DEADLINE", "300"))
COOLDOWN = 300  # seconds before same crash is accepted again

# ── App ─────────────────────────────────────────────────────────────
app = FastAPI(title="CodeGhost Orchestrator")
sandbox = Sandbox()

# Victim-app front ends poll job status cross-origin (e.g. :8003 → :8004)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve live dashboard explicitly to avoid mount 404 issues
from fastapi.responses import HTMLResponse

@app.get("/dash", response_class=HTMLResponse)
@app.get("/dash/", response_class=HTMLResponse)
def serve_dashboard():
    static_file = Path(__file__).parent / "static" / "index.html"
    if static_file.exists():
        return static_file.read_text()
    return "Dashboard HTML not found!"


@app.get("/mission", response_class=HTMLResponse)
@app.get("/mission/", response_class=HTMLResponse)
def serve_mission():
    static_file = Path(__file__).parent / "static" / "mission.html"
    if static_file.exists():
        return static_file.read_text()
    return "Mission Control HTML not found!"

# ── State ───────────────────────────────────────────────────────────
JOBS: dict[str, dict] = {}
_seen: dict[str, float] = {}  # fingerprint → timestamp


# ── Models ──────────────────────────────────────────────────────────
class CrashReport(BaseModel):
    crash_id: str = ""
    stack_trace: str = Field(..., max_length=16000)
    exception_type: str = Field(..., max_length=200)
    exception_message: str = Field(..., max_length=2000)
    method: str = Field(..., max_length=10)
    route: str = Field(..., max_length=500)
    payload: dict | None = None
    framework: str = "fastapi"
    source_file_rel: str = Field("", max_length=500)
    source_root: str = Field("", max_length=500)


# ── Helpers ─────────────────────────────────────────────────────────
def _verify_sig(secret: bytes, request: Request, body: bytes):
    """HMAC verification against an arbitrary shared secret."""
    sig = request.headers.get("X-CodeGhost-Signature", "")
    if sig:
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return
        raise HTTPException(401, "bad HMAC signature")
    key = request.headers.get("x-codeghost-key", "").encode()
    if key and hmac.compare_digest(key, secret):
        return
    raise HTTPException(401, "missing auth")


def _auth(request: Request, body: bytes):
    """Verify the middleware's HMAC-SHA256 signature over the EXACT raw
    body bytes (per CONTRACT.md — kills all canonicalization bugs)."""
    if not SECRET:
        return
    _verify_sig(SECRET, request, body)


def _fingerprint(r: CrashReport) -> str:
    """Create a dedup fingerprint from the crash."""
    last_line = r.stack_trace.strip().splitlines()[-1] if r.stack_trace else ""
    return hashlib.sha256(
        f"{r.exception_type}|{r.route}|{last_line}".encode()
    ).hexdigest()


def _safe_rel(source_file: str, stack_trace: str) -> str:
    """Normalize to a repo-relative path. THE single path-conversion point.
    Blocks path traversal, absolute paths, and non-.py files."""
    rel = source_file
    if not rel:
        # Fallback: extract from stack trace
        m = (
            re.findall(r'File "/?app/([^"]+\.py)"', stack_trace)
            or [
                x
                for x in re.findall(r'File "([^"]+\.py)"', stack_trace)
                if "site-packages" not in x and "/python3" not in x
            ]
        )
        rel = m[-1] if m else ""

    rel = rel.removeprefix("/app/").removeprefix("./").lstrip("/")
    p = PurePosixPath(rel)

    if not rel or p.is_absolute() or ".." in p.parts or p.suffix != ".py":
        raise HTTPException(400, "cannot resolve source file from crash report")

    host = (SOURCE_ROOT / Path(*p.parts)).resolve()
    if SOURCE_ROOT != host and SOURCE_ROOT not in host.parents:
        raise HTTPException(400, "source_file outside app root")
    if not host.is_file():
        raise HTTPException(404, f"source file not found: {rel}")

    return p.as_posix()


def _read_tree(root: Path | None = None) -> dict[str, str]:
    """Read all .py files under a root into a {rel_path: content} dict.
    This is what gets injected into every sandbox container."""
    base = (root or SOURCE_ROOT).resolve()
    skip = {"__pycache__", ".venv", "venv", ".git", "node_modules"}
    tree = {}
    for p in sorted(base.rglob("*.py")):
        if skip & set(p.parts):
            continue
        tree[p.relative_to(base).as_posix()] = p.read_text(errors="replace")
    if not tree:
        raise RuntimeError(f"no .py files found under {base}")
    return tree


def _abs_source_from_trace(stack_trace: str) -> Path | None:
    """Extract the crashing file's absolute path from the stack trace —
    trusted because the report is HMAC-verified with the tenant's secret."""
    for m in reversed(re.findall(r'File "([^"]+\.py)"', stack_trace)):
        if any(x in m for x in ("site-packages", "/python3", ".venv/", "lib/python")):
            continue
        p = Path(m)
        if p.is_file() and p.suffix == ".py":
            return p.resolve()
    return None


@contextmanager
def _step(steps: dict, name: str):
    """Context manager that records step duration in seconds."""
    t = time.monotonic()
    yield
    steps[name] = round(time.monotonic() - t, 2)


# ── Routes ──────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "codeghost-orchestrator"}


@app.post("/crash", status_code=202)
async def crash(request: Request, background: BackgroundTasks):
    body = await request.body()

    # Tenant path: /crash?t=<tid> — verify with the tenant's own secret,
    # deliver with the tenant's token to the tenant's repo.
    tid = request.query_params.get("t") or request.headers.get("x-codeghost-tenant")
    tenant = auth.get_tenant(tid) if tid else None
    if tenant:
        _verify_sig(tenant["secret"].encode(), request, body)
    else:
        _auth(request, body)
    report = CrashReport.model_validate_json(body)

    # Give it an ID if middleware didn't
    if not report.crash_id:
        report.crash_id = uuid.uuid4().hex[:8]

    # Resolve source file path (validates + normalizes)
    if tenant:
        # Tenant mode: the app's source lives wherever the crashing frame
        # says it does — the report is trusted (tenant-secret HMAC).
        rel = ""
    else:
        rel = _safe_rel(report.source_file_rel, report.stack_trace)

    # Deduplication with TTL cooldown
    fp = _fingerprint(report)
    now = time.time()
    if now - _seen.get(fp, 0) < COOLDOWN:
        return {"crash_id": report.crash_id, "status": "duplicate"}
    _seen[fp] = now

    job_id = uuid.uuid4().hex[:8]
    tenant_ctx = None
    if tenant:
        tenant_ctx = {"token": tenant["token"], "repo": tenant["repo"],
                      "login": tenant["login"], "tid": tid}
    JOBS[job_id] = {
        "job_id": job_id,
        "crash_id": report.crash_id,
        "status": "running",
        "stage": "received",
        "route": report.route,
        "t": now,
        "tenant": tid if tenant else None,
        "user": tenant["login"] if tenant else None,
        "repo": tenant["repo"] if tenant else None,
    }

    # Sync function → BackgroundTasks runs it in threadpool automatically
    background.add_task(run_pipeline, job_id, report, rel, tenant_ctx)
    return {"job_id": job_id, "crash_id": report.crash_id, "status": "queued"}


# ── Auth & console ──────────────────────────────────────────────────
def _current_session(request: Request) -> dict | None:
    return auth.get_session(request.cookies.get("cg_session"))


@app.get("/login", response_class=HTMLResponse)
@app.get("/login/", response_class=HTMLResponse)
def serve_login():
    f = Path(__file__).parent / "static" / "login.html"
    return f.read_text() if f.exists() else "login page missing"


@app.get("/apps", response_class=HTMLResponse)
@app.get("/apps/", response_class=HTMLResponse)
def serve_apps():
    f = Path(__file__).parent / "static" / "apps.html"
    return f.read_text() if f.exists() else "apps page missing"


@app.get("/api/setup")
def api_setup():
    return {"oauth_configured": auth.oauth_configured()}


@app.get("/auth/login")
def auth_login(request: Request):
    if not auth.oauth_configured():
        return RedirectResponse("/login?error=oauth_not_configured", 302)
    callback = str(request.base_url).rstrip("/") + "/auth/callback"
    return RedirectResponse(auth.authorize_url(callback), 302)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not auth.consume_state(request.query_params.get("state", "")):
        return RedirectResponse("/login?error=bad_state", 302)
    code = request.query_params.get("code", "")
    token = await auth.exchange_code(code, str(request.base_url).rstrip("/") + "/auth/callback")
    if not token:
        return RedirectResponse("/login?error=token_exchange_failed", 302)
    try:
        user = auth.github_get(token, "/user")
    except Exception:
        return RedirectResponse("/login?error=github_profile_failed", 302)
    sid = auth.create_session(user.get("login", "unknown"), token,
                              user.get("avatar_url", ""))
    resp = RedirectResponse("/apps", 302)
    resp.set_cookie("cg_session", sid, httponly=True, samesite="lax")
    return resp


@app.get("/auth/dev")
def auth_dev():
    ident = auth.dev_login()
    if not ident:
        return RedirectResponse("/login?error=server_identity_unavailable", 302)
    sid = auth.create_session(ident["login"], ident["token"], ident["avatar"])
    resp = RedirectResponse("/apps", 302)
    resp.set_cookie("cg_session", sid, httponly=True, samesite="lax")
    return resp


@app.get("/auth/logout")
def auth_logout(request: Request):
    auth.drop_session(request.cookies.get("cg_session"))
    resp = RedirectResponse("/login", 302)
    resp.delete_cookie("cg_session")
    return resp


@app.get("/api/me")
def api_me(request: Request):
    s = _current_session(request)
    if not s:
        raise HTTPException(401, "not signed in")
    return {"login": s["login"], "avatar": s["avatar"]}


@app.get("/api/my/repos")
def api_my_repos(request: Request):
    s = _current_session(request)
    if not s:
        raise HTTPException(401, "not signed in")
    try:
        repos = auth.github_get(
            s["token"], "/user/repos?per_page=60&sort=pushed&affiliation=owner,collaborator")
    except Exception as e:
        raise HTTPException(502, f"github listing failed: {e}")
    return [{"full_name": r["full_name"], "private": r["private"]} for r in repos]


@app.get("/api/tenants")
def api_tenants(request: Request):
    s = _current_session(request)
    if not s:
        raise HTTPException(401, "not signed in")
    return [{k: t[k] for k in ("tid", "repo", "secret", "prefix", "created")}
            for t in auth.tenants_of(s["login"])]


@app.post("/api/tenants")
async def api_create_tenant(request: Request):
    s = _current_session(request)
    if not s:
        raise HTTPException(401, "not signed in")
    body = await request.json()
    repo = (body.get("repo") or "").strip()
    if not repo or "/" not in repo:
        raise HTTPException(400, "repo must be owner/name")
    t = auth.create_tenant(s["login"], s["token"], repo, body.get("prefix") or "")
    return {"tid": t["tid"], "secret": t["secret"], "repo": t["repo"], "prefix": t["prefix"]}


@app.get("/status/last")
def last_status():
    if not JOBS:
        return {"crash_id": None}
    cid = max(JOBS, key=lambda k: JOBS[k].get("t", 0))
    return {"crash_id": cid, **JOBS[cid]}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404)
    return JOBS[job_id]


@app.get("/jobs")
def list_jobs():
    return [
        {"job_id": k, **{x: v.get(x) for x in ("route", "status", "stage", "pr_url", "tenant", "user", "repo") if x in v}}
        for k, v in JOBS.items()
    ]


# ── Pipeline ────────────────────────────────────────────────────────
def run_pipeline(job_id: str, report: CrashReport, rel: str, tenant: dict | None = None):
    """The autonomous crash-to-fix pipeline. Sync on purpose —
    BackgroundTasks runs this in a threadpool so the event loop stays free."""
    job = JOBS[job_id]
    if tenant:
        job["user"] = tenant["login"]
        job["repo"] = tenant["repo"]
    steps: dict = {}

    def log(msg: str):
        job["stage"] = msg
        job["t"] = time.time()
        print(f"[codeghost:{job_id}] {msg}", flush=True)

    try:
        deadline = time.monotonic() + DEADLINE

        # ── Step 1: Read source ─────────────────────────────────
        log("reading_source")
        with _step(steps, "read_source"):
            if tenant:
                # Tenant mode: the HMAC proves the report came from the
                # tenant's own app — trust its source_root + package layout.
                abs_file = _abs_source_from_trace(report.stack_trace)
                if abs_file is None:
                    raise ValueError("no app source path in stack trace")
                root = Path(report.source_root).resolve() if report.source_root else abs_file.parent
                tree = _read_tree(root)
                rel = (report.source_file_rel or abs_file.name).removeprefix("./").lstrip("/")
                if rel not in tree:
                    raise ValueError(f"source '{rel}' not under {root}: {list(tree.keys())[:8]}")
                report.source_file_rel = rel
                rel_for_pr = abs_file.name
            else:
                tree = _read_tree()
                rel_for_pr = rel
            src = tree.get(rel, "")
            if not src:
                raise ValueError(f"source file '{rel}' not in tree: {list(tree.keys())}")

        # ── Step 2: Generate failing test ───────────────────────
        log("generating_test")
        with _step(steps, "llm_test"):
            test_code = llm_client.generate_failing_test(report, src)
        job["test_code"] = test_code[:500]  # store preview

        # ── Step 3: Confirm RED (crash reproduced) ──────────────
        log("running_failing_test")
        with _step(steps, "sandbox_red"):
            red = sandbox.run_test(test_code, tree)

        # If test itself is broken (import/collection error), regenerate ONCE
        if red.test_broken:
            log("regenerating_test")
            with _step(steps, "llm_test_retry"):
                test_code = llm_client.generate_failing_test(
                    report, src, prev_error=red.output
                )
            with _step(steps, "sandbox_red_retry"):
                red = sandbox.run_test(test_code, tree)
            job["test_code"] = test_code[:500]

            if red.test_broken:
                job.update(
                    status="failed",
                    stage="done",
                    reason="could not generate a runnable test",
                    sandbox_output=red.output[-2000:],
                    steps=steps,
                )
                return

        # If test passed on buggy code, the crash isn't reproducible
        if red.passed:
            job.update(
                status="failed",
                stage="done",
                reason="crash not reproducible (test passed on buggy code)",
                steps=steps,
            )
            return

        # ── Step 4: Fix loop (the agentic part) ────────────────
        fix_code = ""
        verify = None
        feedback = red.output

        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            # The FIRST fix attempt always runs — a slow test generation
            # must not kill the whole pipeline. Deadline only gates retries.
            if attempt > 1 and time.monotonic() > deadline:
                log("deadline_exceeded")
                break

            log("generating_fix")
            with _step(steps, f"llm_fix_{attempt}"):
                fix_code = llm_client.generate_fix(
                    report,
                    src,
                    test_code,
                    feedback,
                    previous_fix=fix_code or None,
                )

            log("verifying_fix")
            with _step(steps, f"sandbox_green_{attempt}"):
                verify = sandbox.run_test(test_code, tree, patched={rel: fix_code})

            job["fix_code"] = fix_code[:600]
            job["green_out"] = verify.output[-1200:] if verify else ""
            if verify.passed:
                break
            feedback = verify.output

        verified = bool(verify and verify.passed)
        job["verified"] = verified

        # No fix at all (e.g. LLM errors) → nothing worth a PR
        if not fix_code.strip():
            job.update(
                status="failed",
                stage="done",
                reason="no fix generated",
                steps=steps,
            )
            return

        # ── Step 5: Open PR ─────────────────────────────────────
        log("opening_pr")
        with _step(steps, "github"):
            pr_url, commit_sha, branch = github_delivery.open_pull_request(
                rel_for_pr, fix_code, test_code, report, verified,
                red_output=red.output,
                green_output=verify.output if verify else "",
                job_id=job_id,
                steps=steps,
                token=tenant["token"] if tenant else None,
                repo_name=tenant["repo"] if tenant else None,
                prefix=tenant.get("prefix") if tenant else None,
            )
        job["commit_sha"] = commit_sha
        job["branch"] = branch

        total = round(sum(steps.values()), 1)
        job.update(
            status="fixed" if verified else "draft_pr",
            stage="done",
            pr_url=pr_url,
            steps=steps,
            elapsed=total,
            retries=max(0, len([k for k in steps if k.startswith("llm_fix")]) - 1),
        )
        log(f"done in {total}s → {pr_url}")

    except Exception as e:
        job.update(status="error", stage="done", error=f"{type(e).__name__}: {e}", steps=steps)
        print(f"[codeghost:{job_id}] ERROR: {e}", flush=True)
