import traceback
import os
import json
import hmac
import hashlib
import asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

def get_relative_source_file(tb):
    """
    Walk traceback frames backwards to extract the first app-level source file 
    as a repo-relative path, skipping framework internals.
    """
    for frame in reversed(traceback.extract_tb(tb)):
        filename = frame.filename
        if any(x in filename for x in ['site-packages', 'lib/python', 'starlette', 'fastapi']):
            continue
        
        # Convert to a relative path from the current working directory
        try:
            return os.path.relpath(filename, os.getcwd())
        except ValueError:
            return filename
            
    return "unknown"

def install_crash_interceptor(app: FastAPI, codeghost_url: str, secret: str):
    """
    Installs global exception handler to intercept crashes, sign them, 
    and forward to the CodeGhost orchestrator.
    """
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        # SANDBOX-AWARE: skip intercepting if running inside test sandbox
        if os.getenv("CODEGHOST_SILENT") == "1":
            return JSONResponse(
                status_code=500,
                content={"error": "Internal Server Error", "detail": str(exc)}
            )
            
        # Extract source file
        source_file = get_relative_source_file(exc.__traceback__)
        
        # Capture stack trace
        stack_trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        
        # Capture payload
        payload = ""
        try:
            body = await request.body()
            if body:
                payload = body.decode('utf-8')
        except Exception:
            pass

        # Strip sensitive headers
        sensitive_headers = {'authorization', 'cookie', 'x-api-key', 'x-auth-token'}
        safe_headers = {
            k: v for k, v in request.headers.items() 
            if k.lower() not in sensitive_headers
        }
        
        # Build payload for orchestrator
        crash_data = {
            "stack_trace": stack_trace,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "method": request.method,
            "route": request.url.path,
            "payload": payload,
            "headers": safe_headers,
            "framework": "fastapi",
            "source_file": source_file
        }
        
        # Serialize and generate HMAC signature
        json_body_bytes = json.dumps(crash_data).encode('utf-8')
        signature = hmac.new(
            secret.encode('utf-8'), 
            json_body_bytes, 
            hashlib.sha256
        ).hexdigest()
        
        async def send_crash_report():
            """Send crash report async to avoid blocking."""
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    await client.post(
                        codeghost_url,
                        content=json_body_bytes,
                        headers={
                            "Content-Type": "application/json",
                            "X-CodeGhost-Signature": signature
                        }
                    )
            except Exception as e:
                # Never let middleware failure crash the crash handler
                print(f"CodeGhost Interceptor failed to report crash: {e}")

        # Fire and forget crash report
        asyncio.create_task(send_crash_report())
        
        # Always return 500 JSON response with error detail
        return JSONResponse(
            status_code=500,
            content={"error": "Internal Server Error", "detail": str(exc)}
        )
