from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from target_app.middleware import install_crash_interceptor
from target_app.presidio_scrubber import warmup

app = FastAPI(title="Target App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Install the crash interceptor (catches 500s and sends to Orchestrator)
install_crash_interceptor(app)

# Pre-warm the Presidio scrubber so the first crash response is instant
warmup()


class CalcRequest(BaseModel):
    a: float
    b: float
    operation: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/calculate")
def calculate(req: CalcRequest):
    """
    BUGGY ENDPOINT: Does not handle ZeroDivisionError.
    This is our rehearsed demo bug.
    """
    if req.operation == "add":
        return {"result": req.a + req.b}
    elif req.operation == "subtract":
        return {"result": req.a - req.b}
    elif req.operation == "multiply":
        return {"result": req.a * req.b}
    elif req.operation == "divide":
        if req.b == 0:
            raise HTTPException(400, "Division by zero is not allowed")
        return {"result": req.a / req.b}
    else:
        raise HTTPException(400, "Unknown operation")


@app.get("/explode")
def explode():
    """
    Demo bug #2: crash whose message carries 'customer secrets' —
    shows Presidio scrubbing before data reaches the AI.
    """
    raise RuntimeError(
        "Payment gateway auth failed with api_key=sk-abc123XYZdef456GHI789jkl012 "
        "for customer dev@corp.com"
    )