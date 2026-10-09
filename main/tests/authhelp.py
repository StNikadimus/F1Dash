"""Test helpers: sign in to /disk the real way (one-time setup code from the data dir -> password), and
approve one /tv page load the real way (trusted /remote device -> challenge -> approval -> page session)."""
from pathlib import Path

PASSWORD = "test password 123"


def disk_login(client, data_dir: Path) -> dict:
    """-> headers with the session's CSRF token for state-changing /disk requests."""
    code = (Path(data_dir) / "auth" / "disk-setup-code").read_text().strip()
    r = client.post("/api/disk/auth/setup", json={"code": code, "password": PASSWORD, "confirm": PASSWORD})
    assert r.status_code == 200, r.text
    return {"X-F1-CSRF": client.get("/api/disk/auth/state").json()["csrf"]}


def tv_approve(app, admin, admin_headers: dict, tv) -> dict:
    """``tv`` (a TestClient = one browser) loads /tv and gets approved by a trusted phone.
    -> the headers this page sends with its protected requests (X-F1-TV-Page)."""
    from starlette.testclient import TestClient
    sec = app.state.security
    if not sec.data.get("trusted_device"):
        phone = TestClient(app)
        assert phone.get("/remote").status_code == 200
        did = sec.device(phone.cookies.get("f1_dev"))[0]
        r = admin.post("/api/disk/security/trust", json={"device": did}, headers=admin_headers)
        assert r.status_code == 200, r.text
    assert "ACCESS REQUEST" in tv.get("/tv").text
    ch = tv.post("/api/tv/auth/request").json()["challenge"]
    rid = next(r["id"] for r in sec.requests.values() if r["status"] == "pending")
    sec.decide(rid, True, by_device=sec.data["trusted_device"])
    st = tv.post("/api/tv/auth/status", headers={"X-F1-TV-Challenge": ch}).json()
    assert st["status"] == "authenticated", st
    return {"X-F1-TV-Page": st["page"]}
