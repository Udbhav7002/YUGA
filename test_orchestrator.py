import hashlib
import json
import httpx
import hmac
import time

SECRET = b"doomsday-hackathon-secret-2026"
BASE_URL = "http://127.0.0.1:8000"

def test_crash():
    payload = {
        "stack_trace": "Traceback (most recent call last):\n  File \"/app/target_app/main.py\", line 14, in calculate\n    return a / b\nZeroDivisionError: division by zero",
        "exception_type": "ZeroDivisionError",
        "exception_message": "division by zero",
        "method": "GET",
        "route": "/calculate",
        "source_file_rel": "target_app/main.py"
    }

    # Sign the EXACT raw body bytes we POST (per CONTRACT.md)
    raw = json.dumps(payload).encode()
    sig = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-CodeGhost-Signature": sig}

    print("🚀 Sending Mock Crash to Orchestrator...")
    try:
        response = httpx.post(f"{BASE_URL}/crash", content=raw, headers=headers)
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.json()}")
        
        if response.status_code == 202:
            job_id = response.json().get("job_id")
            print(f"\n✅ Crash accepted! Job ID: {job_id}")
            print(f"Polling status for {job_id} for 5 seconds...")
            for _ in range(5):
                time.sleep(1)
                res = httpx.get(f"{BASE_URL}/status/{job_id}")
                print(res.json())
                
    except httpx.ConnectError:
        print("❌ Error: Could not connect to Orchestrator. Is it running on port 8000?")

if __name__ == "__main__":
    test_crash()
