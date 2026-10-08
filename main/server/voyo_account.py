"""The VOYO account the server VOYO player signs in with (entered on the /disk page).

Stored in ``<data>/auth/voyo_credentials.json`` (file 600, folder 700 - readable only by the
service user). The API never returns the password: only whether one is set and a masked e-mail.
The player (tools/voyo_server_player.py) types them into VOYO's own sign-in form when VOYO asks
(and on "LOGIN NOW") - the same as a password manager would; nothing of VOYO's stream or DRM is
touched. The F1 stream page address is kept in ``<data>/voyo_server_player.json`` (``page_url``).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


def mask_email(e: str) -> str:
    if "@" not in (e or ""):
        return "***" if e else ""
    user, dom = e.split("@", 1)
    return (user[:1] + "***" if user else "***") + "@" + dom


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _read(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class VoyoAccount:
    def __init__(self, creds_path: Path, state_path: Path, config_url: str = "") -> None:
        self.creds_path = creds_path
        self.state_path = state_path
        self.config_url = config_url or ""
        self.commands: list[str] = []                 # for the player: picked up with its heartbeat
        self.last_login: Optional[dict] = None

    # ------------------------------------------------------------------ credentials
    def credentials(self) -> dict:
        return _read(self.creds_path)

    def save(self, email: Optional[str] = None, password: Optional[str] = None,
             stream_url: Optional[str] = None) -> list[str]:
        changed = []
        if email is not None or password:
            cur = self.credentials()
            if email is not None:
                email = email.strip()
                if email and ("@" not in email or len(email) > 200):
                    raise ValueError("that is not an e-mail address")
                cur["email"] = email
                changed.append("e-mail")
            if password:
                if len(password) > 300:
                    raise ValueError("password too long")
                cur["password"] = password
                changed.append("password")
            cur["saved_at"] = time.time()
            _write_private(self.creds_path, cur)
        if stream_url is not None:
            stream_url = stream_url.strip()
            if stream_url:
                u = urlparse(stream_url)
                if u.scheme not in ("http", "https") or not u.netloc:
                    raise ValueError("the stream page must be a web address (https://...)")
            st = _read(self.state_path)
            st["page_url"] = stream_url
            st["page_url_saved_at"] = time.time()
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(st, indent=1), encoding="utf-8")
            changed.append("stream page")
        return changed

    def forget(self) -> None:
        try:
            self.creds_path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------ for the page
    def stream_url(self) -> tuple[str, str]:
        """The F1 stream page the player opens and where it comes from."""
        st = _read(self.state_path)
        if st.get("page_url"):
            return st["page_url"], "set on the /disk page"
        if self.config_url:
            return self.config_url, "[voyo.server_player] stream_url"
        if st.get("last_url"):
            return st["last_url"], "learned at voyo-player.sh login"
        return "", "not set"

    def public(self) -> dict:
        c = self.credentials()
        url, src = self.stream_url()
        host = urlparse(url).netloc if url else ""
        return {"email": mask_email(c.get("email", "")), "email_set": bool(c.get("email")),
                "password_set": bool(c.get("password")), "saved_at": c.get("saved_at"),
                "stream_url": url, "stream_url_source": src,
                "stream_url_warning": "not a voyo.si page" if host and "voyo" not in host.lower() else None,
                "last_login": self.last_login, "pending": list(self.commands)}

    def take_commands(self) -> list[str]:
        cmds, self.commands = self.commands, []
        return cmds
