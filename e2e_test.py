import hmac
import hashlib
import json
import httpx
import time

SECRET = b"doomsday-hackathon-secret-2026"
BASE_URL = "http://127.0.0.1:8000"

def run_e2e_test():
    # Randomize line number to bypass Orchestrator deduplication cache
    random_line = __import__('random').randint(10, 1000)

    payload = {
        "stack_trace": f"Traceback (most recent call last):\n  File \"/app/target_app/main.py\", line {random_line}, in calculate\n    return a / b\nZeroDivisionError: division by zero",
        "exception_type": "ZeroDivisionError",
        "exception_message": "division by zero",
        "method": "POST",
        "route": "/calculate",
        "payload": {"a": 10, "b": 0, "operation": "divide"},
        "source_file_rel": "target_app/main.py"
    }

    # Sign the EXACT raw body bytes we POST (per CONTRACT.md)
    raw = json.dumps(payload).encode()
    sig = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-CodeGhost-Signature": sig}

    print("🚀 [1/3] Sending Mock Crash to Orchestrator...")
    try:
        response = httpx.post(f"{BASE_URL}/crash", content=raw, headers=headers)
        if response.status_code != 202:
            print(f"❌ Failed to queue job: {response.json()}")
            return
            
        job_id = response.json().get("job_id")
        print(f"✅ Crash accepted! Job ID: {job_id}")
        
    except httpx.ConnectError:
        print("❌ Error: Could not connect to Orchestrator on port 8000.")
        return

    print(f"\n⏳ [2/3] Polling Pipeline Status for Job {job_id}...")
    start_time = time.time()
    last_stage = None
    
    while True:
        try:
            res = httpx.get(f"{BASE_URL}/status/{job_id}").json()
            status = res.get("status")
            stage = res.get("stage")
            
            if stage != last_stage:
                elapsed = round(time.time() - start_time, 1)
                print(f"   [{elapsed}s] 🔄 Stage changed: {stage}")
                last_stage = stage
                
            if status in ("fixed", "draft_pr", "error", "failed"):
                print(f"\n🏁 [3/3] Pipeline Completed with status: {status.upper()}")
                if status == "error":
                    print(f"❌ Error Detail: {res.get('error')}")
                elif status == "failed":
                    print(f"❌ Failed Reason: {res.get('reason')}")
                else:
                    print(f"✅ Success! PR URL: {res.get('pr_url')}")
                    print(f"⏱️ Total Time: {res.get('elapsed')}s")
                    print(f"📊 Step Breakdown: {res.get('steps')}")
                break
                
            time.sleep(2)
            
        except KeyboardInterrupt:
            print("\n🛑 Polling aborted by user.")
            break
        except Exception as e:
            print(f"\n❌ Error during polling: {e}")
            break

if __name__ == "__main__":
    run_e2e_test()
