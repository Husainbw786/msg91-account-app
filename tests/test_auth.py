"""Login ID + password sign-in, admin user management, lockout."""
import importlib
import sys

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setenv("RECON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_USERNAME", "Admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "Bootstrap-Pass1")
    monkeypatch.setenv("HTTPS_ONLY", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    for m in [m for m in sys.modules if m.startswith("app")]:
        del sys.modules[m]
    main = importlib.import_module("app.main")
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def login(c, u, p):
    return c.post("/auth/login", json={"username": u, "password": p})


def test_admin_flow_and_users(client):
    assert client.get("/api/runs").status_code == 401
    assert login(client, "admin", "wrong-Pass1").status_code == 401
    r = login(client, "ADMIN", "Bootstrap-Pass1")
    assert r.status_code == 200 and r.json()["user"]["must_change"] is True
    assert client.post("/api/me/password", json={"old_password": "Bootstrap-Pass1", "new_password": "short"}).status_code == 400
    assert client.post("/api/me/password", json={"old_password": "Bootstrap-Pass1",
                                                 "new_password": "My-New-Pass42"}).status_code == 200
    r = client.post("/api/users", json={"username": "priya", "name": "Priya"})
    temp = r.json()["temporary_password"]
    uid = r.json()["user"]["id"]
    assert client.get("/api/runs").status_code == 200

    other = type(client)(client.app)
    assert login(other, "priya", temp).status_code == 200
    assert other.get("/api/users").status_code == 403                 # not an admin
    client.patch(f"/api/users/{uid}", json={"active": False})
    assert other.get("/api/runs").status_code == 401                   # signed out on deactivation
    assert login(other, "priya", temp).status_code == 401


def test_lockout_after_five_wrong_passwords(client):
    for _ in range(5):
        assert login(client, "admin", "nope-Wrong1").status_code == 401
    assert login(client, "admin", "Bootstrap-Pass1").status_code == 429


def test_admin_cannot_deactivate_self(client):
    login(client, "admin", "Bootstrap-Pass1")
    me = [u for u in client.get("/api/users").json() if u["username"] == "admin"][0]
    assert client.patch(f"/api/users/{me['id']}", json={"active": False}).status_code == 400


def test_old_numeric_session_id_is_signed_out():
    """Cookies from the SQLite version held uid=1; they must read as signed out, not crash /api/me."""
    from starlette.requests import Request
    from fastapi import HTTPException
    from app import auth
    req = Request({"type": "http", "session": {"uid": 1, "sv": 1}})
    try:
        auth.current_user(req)
        assert False, "should be signed out"
    except HTTPException as e:
        assert e.status_code == 401 and req.session == {}
