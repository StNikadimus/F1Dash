"""F1 TV account sign-in for the live-timing feed (``[f1_tv] subscription = true``).

How it works (no password, API key or cookie is ever typed into the terminal):

1. The server opens ``http://127.0.0.1:<port>/f1tv/login`` in the default browser. That page
   (served by this dashboard, nothing third-party) opens the official F1 login
   (account.formula1.com) in a new tab, where you sign in normally.
2. After signing in, one click on the "F1 Dashboard sign-in" bookmark (drag it to the bookmarks
   bar once) on any formula1.com page hands the session's *subscription token* - and only that -
   to this local server (a form POST to 127.0.0.1; it never leaves your computer). Where a
   browser blocks bookmarklets, the same page accepts the ``login-session`` cookie value.
3. The token is checked (it is a JWT: expiry, subscription status / product) and stored in
   ``data/auth/f1tv_auth.json`` (file mode 0600, git-ignored). The live client reconnects with
   it as ``Authorization: Bearer`` - exactly what the official apps do.

Later starts reuse the stored token without opening the browser. An expired token, one F1
rejects (HTTP 401/403), a deleted file or ``python main.py --f1-login`` start the sign-in again;
meanwhile the anonymous public feed keeps the dashboard running.

The token is never logged, never sent to a dashboard / WebSocket client, never recorded.
F1 decides per connection which topics it streams; nothing here assumes what a tier provides.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import secrets
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("f1tv_auth")

F1_LOGIN_URL = ("https://account.formula1.com/#/en/login?redirect="
                + urllib.parse.quote("https://f1tv.formula1.com/", safe=""))
EXPIRY_MARGIN_S = 120            # treat a token expiring within this as expired
BROWSER_REOPEN_S = 600           # do not reopen the browser more often than this (unless forced)


# ----------------------------------------------------------------------------- token helpers
def parse_token(raw: str) -> Optional[str]:
    """Accept a bare JWT or the complete (URL-encoded) ``login-session`` cookie value."""
    import re
    if not raw:
        return None
    raw = str(raw).strip().strip('"')
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if raw.startswith("eyJ") and raw.count(".") == 2:
        return raw
    try:
        data = json.loads(urllib.parse.unquote(raw))
        tok = (data.get("data") or {}).get("subscriptionToken")
        if tok:
            return tok
    except (ValueError, AttributeError):
        pass
    m = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", urllib.parse.unquote(raw))
    return m.group(0) if m else None


def token_claims(token: str) -> dict:
    """The JWT payload (not verified - F1 verifies it when connecting). {} if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def token_info(token: Optional[str], now: Optional[float] = None) -> dict:
    """Non-secret facts about a token: expiry, subscription status / product (for diagnostics)."""
    if not token:
        return {"present": False}
    c = token_claims(token)
    exp = c.get("exp")
    now = time.time() if now is None else now
    try:
        exp = float(exp) if exp is not None else None
    except (TypeError, ValueError):
        exp = None
    return {
        "present": True,
        "readable": bool(c),
        "expires_utc": datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="seconds") if exp else None,
        "expired": bool(exp is not None and exp - EXPIRY_MARGIN_S <= now),
        "subscription_status": c.get("SubscriptionStatus"),
        "subscribed_product": c.get("SubscribedProduct"),
    }


# ----------------------------------------------------------------------------- storage
class AuthStore:
    """Only the subscription token (+ when it was stored) - nothing else of the session."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Optional[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("F1 TV sign-in file %s is unreadable - ignoring it", self.path)
            return None
        tok = data.get("subscription_token") if isinstance(data, dict) else None
        return tok if isinstance(tok, str) and tok.count(".") == 2 else None

    def save(self, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        body = json.dumps({"subscription_token": token,
                           "stored_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def key(self) -> str:
        """Per-installation key of the sign-in bookmark (not an F1 credential): only a request
        carrying it can store a token. Created once, kept next to the token file."""
        p = self.path.with_name("signin_key")
        try:
            k = p.read_text(encoding="utf-8").strip()
            if len(k) >= 16:
                return k
        except OSError:
            pass
        k = secrets.token_urlsafe(18)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(k)
        return k


# ----------------------------------------------------------------------------- manager
class AuthManager:
    """State: DISABLED (subscription = false) / NO_LOGIN / WAITING_LOGIN / VALID / EXPIRED /
    REJECTED. ``token()`` is what the live client sends; None = connect anonymously."""

    def __init__(self, cfg: dict, store: AuthStore, override: str = "",
                 opener: Callable[[str], Any] = webbrowser.open) -> None:
        self.subscription = bool(cfg.get("subscription", True))
        self.open_browser = bool(cfg.get("open_browser", True))
        self.store = store
        self.opener = opener
        self.login_url: Optional[str] = None       # set by the app once the HTTP port is known
        self._override = parse_token(override) if override else None   # live.f1tv_token / F1TV_TOKEN
        self._token: Optional[str] = None
        self.state = "DISABLED"
        self.reason = "subscription = false in [f1_tv]"
        self.version = 0                            # bumps when a new token becomes usable
        self._changed: Optional[asyncio.Event] = None
        self._opened_at = -1e9
        self.confirmed = False                      # F1 accepted a connection with the current token
        if override and not self._override:
            log.error("live.f1tv_token / F1TV_TOKEN is set but is not a JWT or login-session cookie - ignored")
        if self.subscription:
            self._load()

    # -------------------------------------------------------------- state
    def _load(self) -> None:
        tok = self._override or self.store.load()
        if tok is None:
            self.state, self.reason = "NO_LOGIN", "no stored F1 TV sign-in"
            self._token = None
            return
        info = token_info(tok)
        if info["expired"]:
            self.state, self.reason = "EXPIRED", f"stored F1 TV sign-in expired ({info['expires_utc']})"
            self._token = None
            return
        self._token = tok
        self.state, self.reason = "VALID", "stored F1 TV sign-in"

    def token(self) -> Optional[str]:
        """The token to connect with, re-checked for expiry each time (None: anonymous)."""
        if not self.subscription:
            return None
        if self._token is None and self.state in ("NO_LOGIN", "WAITING_LOGIN"):
            self._load()                            # e.g. the file was restored meanwhile
            if self.state == "NO_LOGIN" and self._opened_at > 0:
                self.state = "WAITING_LOGIN"
        if self._token is not None and token_info(self._token)["expired"]:
            log.warning("F1 TV sign-in expired - signing in again is needed")
            self._token = None
            self.state, self.reason = "EXPIRED", "F1 TV sign-in expired"
        return self._token

    def needs_login(self) -> bool:
        return self.subscription and self.token() is None

    def public_info(self) -> dict:
        """Safe for logs / dashboards / diagnostics: never the token itself."""
        info = token_info(self._token) if self._token else {"present": False}
        return {"subscription": self.subscription, "state": self.state, "reason": self.reason,
                "confirmed_by_f1": bool(self.confirmed and self._token),
                "product": info.get("subscribed_product"), "subscription_status": info.get("subscription_status"),
                "expires_utc": info.get("expires_utc"), "login_url": self.login_url if self.subscription else None}

    # -------------------------------------------------------------- events
    def _event(self) -> asyncio.Event:
        if self._changed is None:
            self._changed = asyncio.Event()
        return self._changed

    async def wait_change(self, version: int) -> None:
        while self.version == version:
            ev = self._event()
            ev.clear()
            if self.version != version:
                return
            await ev.wait()

    def _bump(self) -> None:
        self.version += 1
        if self._changed is not None:
            self._changed.set()

    # -------------------------------------------------------------- sign-in flow
    def request_login(self, why: str, force: bool = False) -> None:
        """Open the local sign-in page in the default browser (rate limited unless forced)."""
        if not self.subscription:
            return
        if self.state == "NO_LOGIN":
            self.state = "WAITING_LOGIN"       # (EXPIRED / REJECTED keep saying why)
            self.reason = why
        now = time.monotonic()
        url = self.login_url
        if url is None:
            log.warning("F1 TV sign-in needed (%s) - the sign-in page opens once the server is up", why)
            return
        if not force and now - self._opened_at < BROWSER_REOPEN_S:
            return
        self._opened_at = now
        log.warning("F1 TV sign-in needed (%s). Sign in in the browser: %s", why, url)
        if self.open_browser:
            try:
                if not self.opener(url):
                    log.warning("Could not open a browser - open %s yourself", url)
            except Exception:  # noqa: BLE001
                log.warning("Could not open a browser - open %s yourself", url)

    def complete(self, raw: str) -> tuple[bool, str]:
        """A sign-in arrived (bookmark / pasted cookie). Returns (ok, message for the page)."""
        tok = parse_token(raw or "")
        if not tok:
            return False, "No F1 sign-in found. Sign in at formula1.com first, then use the bookmark again."
        info = token_info(tok)
        if not info["readable"]:
            return False, "That is not an F1 TV sign-in token."
        if info["expired"]:
            return False, "That F1 sign-in has already expired - sign out and in again at formula1.com."
        self.store.save(tok)
        self._token = tok
        self.confirmed = False
        self.state, self.reason = "VALID", "signed in via the browser"
        log.info("F1 TV sign-in stored (%s, %s, valid until %s)", info.get("subscribed_product") or "product unknown",
                 info.get("subscription_status") or "status unknown", info.get("expires_utc"))
        self._bump()
        return True, (f"Signed in: {info.get('subscribed_product') or 'F1 account'}"
                      f" ({info.get('subscription_status') or 'status unknown'}), valid until {info.get('expires_utc')}.")

    def confirm(self) -> None:
        """F1 accepted the authenticated connection (negotiate + handshake with the token)."""
        if not self.confirmed:
            self.confirmed = True

    def reject(self, why: str) -> None:
        """F1 refused the token (HTTP 401/403): forget it and sign in again."""
        log.warning("F1 rejected the F1 TV sign-in (%s) - it is removed; signing in again is needed", why)
        if self._override and self._token == self._override:
            self._override = None
        self.store.clear()
        self._token = None
        self.confirmed = False
        self.state, self.reason = "REJECTED", f"rejected by F1 ({why})"
        self.request_login("F1 rejected the stored sign-in")

    def logout(self) -> bool:
        self._token = None
        self._override = None
        self.state, self.reason = ("NO_LOGIN" if self.subscription else "DISABLED"), "signed out"
        return self.store.clear()


# ----------------------------------------------------------------------------- HTTP pages
def bookmarklet(callback_url: str, key: str) -> str:
    """Runs on a formula1.com page you are signed in on: posts the login-session cookie (it holds
    the subscription token) to this local server - a top-level form POST, so it works without
    CORS, and the value is not put into any URL."""
    js = (
        "(function(){var m=document.cookie.match(/(?:^|; )login-session=([^;]*)/);"
        "if(!m){alert('F1 Dashboard: not signed in on this formula1.com page - sign in first.');return;}"
        "var f=document.createElement('form');f.method='POST';f.action=" + json.dumps(callback_url) + ";"
        "f.target='_blank';"
        "[['key'," + json.dumps(key) + "],['session',m[1]]].forEach(function(p){"
        "var i=document.createElement('input');i.type='hidden';i.name=p[0];i.value=p[1];f.appendChild(i);});"
        "document.body.appendChild(f);f.submit();f.remove();})();"
    )
    return "javascript:" + urllib.parse.quote(js, safe="(){};,=:'/.!?&*+-_[]")


def login_page(callback_url: str, key: str, info: dict) -> str:
    bm = html.escape(bookmarklet(callback_url, key), quote=True)
    state = html.escape(f"{info.get('state')} - {info.get('reason') or ''}")
    login = html.escape(F1_LOGIN_URL, quote=True)
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>F1 Dashboard - F1 TV sign-in</title>
<style>body{{font:16px system-ui,sans-serif;background:#0b0e12;color:#e6ebf0;max-width:760px;margin:40px auto;padding:0 16px}}
a.b,button{{display:inline-block;background:#e10600;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none;border:0;font:inherit;cursor:pointer}}
a.bm{{background:#2f6fd6;cursor:grab}} code,input{{font:14px monospace}} input{{width:100%;padding:8px;background:#141a21;color:#e6ebf0;border:1px solid #333}}
li{{margin:12px 0}} .st{{color:#9aa4ae}} .ok{{color:#3ad07a}}</style></head><body>
<h1>F1 TV sign-in</h1>
<p class="st" id="st">Current state: {state}</p>
<ol>
<li><a class="b" href="{login}" target="_blank" rel="noopener">Open the official F1 sign-in</a> and sign in with your F1 TV account as usual.</li>
<li>Drag this button to your bookmarks bar (once): <a class="b bm" href="{bm}">F1 Dashboard sign-in</a></li>
<li>On the formula1.com tab where you are signed in, click that bookmark. This page then shows "Signed in".</li>
</ol>
<p class="st">Only the subscription token goes to this dashboard on your computer (127.0.0.1); your password never does.
It is stored in <code>data/auth/f1tv_auth.json</code> and is never shown to dashboards or written to recordings.</p>
<details><summary>Browser blocks bookmarklets?</summary>
<p class="st">On the signed-in formula1.com tab open DevTools → Application → Cookies → <code>login-session</code>, copy its value and paste it here:</p>
<form method="POST" action="{html.escape(callback_url, quote=True)}"><input type="hidden" name="key" value="{html.escape(key, quote=True)}">
<input name="session" autocomplete="off" placeholder="login-session value"><p><button>Sign in</button></p></form></details>
<script>setInterval(function(){{fetch('/f1tv/status').then(function(r){{return r.json()}}).then(function(s){{
var e=document.getElementById('st');e.textContent='Current state: '+s.state+(s.product?' - '+s.product:'')+(s.expires_utc?' (valid until '+s.expires_utc+')':'');
e.className=s.state==='VALID'?'ok':'st';}}).catch(function(){{}})}},2000);</script>
</body></html>"""


def result_page(ok: bool, message: str) -> str:
    color = "#3ad07a" if ok else "#ff5a5f"
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>F1 Dashboard sign-in</title></head>"
            f"<body style='font:18px system-ui;background:#0b0e12;color:#e6ebf0;max-width:700px;margin:60px auto'>"
            f"<h1 style='color:{color}'>{'Signed in' if ok else 'Sign-in failed'}</h1><p>{html.escape(message)}</p>"
            f"<p>{'You can close this tab - the dashboard reconnects with your F1 TV sign-in.' if ok else ''}</p>"
            f"</body></html>")
