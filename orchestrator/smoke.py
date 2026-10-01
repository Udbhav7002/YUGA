"""
Integration Smoke Test (Run me first!)
Proves the Sandbox mechanics work BEFORE involving any LLMs.
Run from the `codeghost/orchestrator` directory.
"""

from sandbox import Sandbox

def main():
    print("Testing Sandbox mechanics (No AI involved)...")
    
    # 1. Fake target app source code
    tree = {
        "main.py": """
from fastapi import FastAPI
app = FastAPI()
@app.post('/calculate')
def calc(): return 1/0
""",
        "middleware.py": "# fake middleware"
    }
    
    # 2. Fake LLM generated test (should FAIL on buggy code)
    test_code = """
from fastapi.testclient import TestClient
from main import app
client = TestClient(app, raise_server_exceptions=False)

def test_reproduce_crash():
    r = client.post("/calculate", json={"a": 10, "b": 0, "operation": "divide"})
    assert r.status_code != 500
"""
    
    sb = Sandbox()
    
    # 3. Test the RED path
    print("\n--- Running RED path (Expect Failure) ---")
    res_red = sb.run_test(test_code, tree)
    print(f"Exit Code: {res_red.exit_code}")
    if res_red.exit_code != 0 and not res_red.test_broken:
        print("✅ RED PATH PASSED! (Test successfully failed on buggy code)")
    else:
        print(f"❌ RED PATH FAILED. Output:\n{res_red.output}")
        return

    # 4. Fake LLM generated fix
    fix_code = """
from fastapi import FastAPI
app = FastAPI()
@app.post('/calculate')
def calc(): return 1
"""

    # 5. Test the GREEN path
    print("\n--- Running GREEN path (Expect Success) ---")
    res_green = sb.run_test(test_code, tree, patched={"main.py": fix_code})
    print(f"Exit Code: {res_green.exit_code}")
    if res_green.exit_code == 0:
        print("✅ GREEN PATH PASSED! (Test successfully passed on fixed code)")
    else:
        print(f"❌ GREEN PATH FAILED. Output:\n{res_green.output}")

if __name__ == "__main__":
    main()
