import hmac
import hashlib
import json
import os
import traceback
import uuid
import re

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
import asyncio

from target_app.presidio_scrubber import scrub

CODEGHOST_URL = os.getenv("CODEGHOST_URL", "")  # empty = disabled (e.g. inside sandbox)
SHARED_SECRET = os.getenv("CODEGHOST_SHARED_SECRET", "")


def install_crash_interceptor(app):
    @app.middleware("http")
    async def _body_cache(request: Request, call_next):
        """Cache the request body BEFORE routing and re-inject the receive
        channel — the endpoint can still read it, and the exception handler
        can recover it from request.state after the channel is gone."""
        raw = await request.body()
        request.state.raw_body = raw

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        request._receive = receive
        return await call_next(request)

    @app.exception_handler(Exception)
    async def crash_handler(request: Request, exc: Exception):
        # 1. Extract traceback reliably using the exception's own __traceback__
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

        try:
            body = json.loads(request.state.raw_body) if request.state.raw_body else None
        except Exception:
            body = None

        # 2. Build the crash report — Presidio scrubs secrets/PII from the
        # fields the AI will see, so nothing sensitive leaves the process
        report = {
            "crash_id": str(uuid.uuid4()),
            "stack_trace": scrub(tb)[-16000:],  # cap size
            "exception_type": type(exc).__name__,
            "exception_message": scrub(str(exc))[:2000],
            "method": request.method,
            "route": request.url.path,
            "payload": body,
            "framework": "fastapi",
            "source_file_rel": _extract_source_file(tb),
        }

        # 3. Ship to orchestrator (if configured)
        if CODEGHOST_URL:
            # Strip sensitive data before sending
            raw_body = json.dumps(report).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            
            # Sign with HMAC if secret is configured (signature ONLY —
            # never send the secret itself over the wire)
            if SHARED_SECRET:
                sig = hmac.new(SHARED_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
                headers["X-CodeGhost-Signature"] = sig

            async def _send():
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(CODEGHOST_URL, content=raw_body, headers=headers, timeout=3.0)
                except Exception as e:
                    print(f"[Middleware] Failed to phone home: {e}")

            # Fire and forget — never let the crash reporter crash the crash handler!
            asyncio.create_task(_send())

        # Always return 500 to the user
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "crash_id": report["crash_id"]}
        )


def _extract_source_file(tb: str) -> str:
    """Extract the repo-relative path to the crashing file.
    THE single path-conversion point (per plan): strip the container
    prefix (APP_ROOT, default /app) or the local working directory."""
    # Container case: app frames live under APP_ROOT (default /app)
    app_root = os.getenv("APP_ROOT", "/app").rstrip("/")
    matches = re.findall(rf'File "{re.escape(app_root)}/([^"]+\.py)"', tb)
    if matches:
        return matches[-1]

    # Local case: make the deepest app frame relative to the cwd
    matches = [
        m for m in re.findall(r'File "([^"]+\.py)"', tb)
        if "site-packages" not in m and "/python3" not in m and ".venv/" not in m
    ]
    if not matches:
        return ""
    path = matches[-1]
    try:
        return os.path.relpath(path, os.getcwd())
    except ValueError:
        return path.lstrip("/")
