from fastapi.testclient import TestClient
from target_app.main import app

def test_reproduce_crash():
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/calculate", json={"a": 10, "b": 0, "operation": "divide"})
    assert response.status_code != 500