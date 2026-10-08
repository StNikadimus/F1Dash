"""Test helper: sign in to /disk the real way (one-time setup code from the data dir -> password)."""
from pathlib import Path

PASSWORD = "test password 123"


def disk_login(client, data_dir: Path) -> dict:
    """-> headers with the session's CSRF token for state-changing /disk requests."""
    code = (Path(data_dir) / "auth" / "disk-setup-code").read_text().strip()
    r = client.post("/api/disk/auth/setup", json={"code": code, "password": PASSWORD, "confirm": PASSWORD})
    assert r.status_code == 200, r.text
    return {"X-F1-CSRF": client.get("/api/disk/auth/state").json()["csrf"]}
