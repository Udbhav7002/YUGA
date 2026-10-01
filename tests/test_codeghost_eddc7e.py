from fastapi.testclient import TestClient
from target_app.main import app

def test_reproduce_crash():
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/explode", json=None)
    assert response.status_code != 500