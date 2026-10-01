from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from target_app.middleware import install_crash_interceptor

app = FastAPI(title="Target App")

# Install the crash interceptor (catches 500s and sends to Orchestrator)
install_crash_interceptor(app)


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
            raise HTTPException(400, "Division by zero")
        return {"result": req.a / req.b}
    else:
        raise HTTPException(400, "Unknown operation")