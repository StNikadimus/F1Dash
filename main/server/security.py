"""Server-side authentication for /disk, /tv and the trusted /remote device.

Three separate concepts (never mixed):

* **DISK USER** - whoever knows the /disk password. The first password can only be set with a
  one-time setup code the server writes to ``<data>/auth/disk-setup-code`` (readable only by the
  service user / root): knowing the URL is not enough. The password is stored as an Argon2id hash
  (argon2-cffi; scrypt from the standard library only when argon2-cffi is missing).
* **REMOTE DEVICE** - every browser that opens /remote gets a device identity: a random secret in an
  HttpOnly cookie (only its SHA-256 is stored) plus a public id, a short code shown on the phone and a
  name. A device is *not* trusted because it is connected: the disk user makes one device the trusted
  approver in /disk.
* **TV PAGE** - ONE load of the /tv page, approved by the trusted remote device (only that device -
  not the disk user, not the TV itself). Nothing about a browser is remembered: every load of /tv
  ends the previous authorization of that browser and starts a new challenge (random id, short
  expiry, a browser secret in an HttpOnly cookie + a page secret held only in that page's memory).
  An approval is bound to that one challenge and consumed once; it mints a TV page session that needs
  BOTH the HttpOnly session cookie and the page's own secret (a header), lives only in the server's
  memory (a restart ends it) and expires after ``tv_page_hours``.

Disk sessions: ``secrets.token_urlsafe(32)`` tokens in HttpOnly cookies, only their SHA-256 stored (in
``<data>/auth/security.json``, mode 600), with expiry, revocation and a fresh token on every login
(no fixation). State-changing /disk requests also need the session's CSRF token in a header.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("security")

try:                                            # Argon2id - preferred (requirements.txt)
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    _PH = PasswordHasher()                      # argon2id, library defaults (64 MiB, t=3, p=4)
except ImportError:                             # pragma: no cover - fallback: scrypt (stdlib)
    _PH = None

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
MIN_PASSWORD = 10
MAX_PASSWORD = 256


def sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def human_code(n: int = 6) -> str:
    c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(n))
    return c[:3] + "-" + c[3:]


def ua_summary(ua: str) -> str:
    """Enough to recognise a device ("iPhone · Safari"), nothing more."""
    ua = ua or ""
    dev = next((n for p, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("SMART-TV", "Smart TV"),
                               ("SmartTV", "Smart TV"), ("Tizen", "Samsung TV"), ("Web0S", "LG TV"), ("CrKey", "Chromecast"),
                               ("Windows", "Windows"), ("Macintosh", "Mac"), ("Linux", "Linux")) if p in ua), "Browser")
    br = next((n for p, n in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
               if p in ua), "")
    return f"{dev} · {br}" if br else dev


# ---------------------------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------------------------
def hash_password(pw: str) -> str:
    if _PH is not None:
        return _PH.hash(pw)
    salt = secrets.token_bytes(16)                                   # pragma: no cover
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt$32768$8$1${salt.hex()}${dk.hex()}"


def verify_password(stored: str, pw: str) -> bool:
    """Constant-time (the libraries compare in constant time)."""
    if not stored or not pw:
        return False
    if stored.startswith("$argon2"):
        if _PH is None:
            return False
        try:
            return _PH.verify(stored, pw)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    if stored.startswith("scrypt$"):
        try:
            _, n, r, p, salt, dk = stored.split("$")
            calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                  maxmem=128 * 1024 * 1024, dklen=len(bytes.fromhex(dk)))
            return hmac.compare_digest(calc, bytes.fromhex(dk))
        except (ValueError, TypeError):
            return False
    return False


def password_problem(pw: str, confirm: Optional[str]) -> Optional[str]:
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        return f"the password needs at least {MIN_PASSWORD} characters"
    if len(pw) > MAX_PASSWORD:
        return "the password is too long"
    if confirm is not None and not hmac.compare_digest(pw, str(confirm)):
        return "the two passwords are not the same"
    return None


# ---------------------------------------------------------------------------------------------
# rate limiting (per key, sliding window)
# ---------------------------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, limit: int, window_s: float, lockout_s: float = 0.0) -> None:
        self.limit, self.window, self.lockout = limit, window_s, lockout_s
        self.hits: dict[str, deque] = {}
        self.locked: dict[str, float] = {}

    def blocked(self, key: str) -> float:
        """Seconds still blocked (0 = allowed)."""
        now = time.monotonic()
        until = self.locked.get(key, 0)
        if until > now:
            return until - now
        q = self.hits.get(key)
        if q:
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return self.window - (now - q[0])
        return 0.0

    def hit(self, key: str) -> None:
        now = time.monotonic()
        q = self.hits.setdefault(key, deque())
        q.append(now)
        while q and now - q[0] > self.window:
            q.popleft()
        if self.lockout and len(q) >= self.limit:
            self.locked[key] = now + self.lockout
        if len(self.hits) > 5000:                         # bounded memory
            for k in list(self.hits)[:1000]:
                self.hits.pop(k, None)

    def reset(self, key: str) -> None:
        self.hits.pop(key, None)
        self.locked.pop(key, None)


# ---------------------------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------------------------
class Security:
    def __init__(self, auth_dir: Path, cfg: Optional[dict] = None, clock=time.time) -> None:
        self.dir = Path(auth_dir)
        self.path = self.dir / "security.json"
        self.code_path = self.dir / "disk-setup-code"
        c = cfg or {}
        self.tv_ttl = float(c.get("tv_page_hours", 12)) * 3600
        self.disk_ttl = float(c.get("disk_session_hours", 12)) * 3600
        self.reauth_s = float(c.get("reauth_minutes", 10)) * 60
        self.request_ttl = float(c.get("tv_request_seconds", 120))
        self.now = clock
        self._lock = threading.RLock()
        self.requests: dict[str, dict] = {}           # TV authorization challenges - memory only, short-lived
        self.tv_pages: dict[str, dict] = {}           # sha(cookie) -> approved TV page - memory only
        self.connected: dict[str, dict] = {}          # device id -> {"count", "since"} (open /remote sockets)
        self.data: dict[str, Any] = {"disk": {"hash": None}, "trusted_device": None, "devices": {},
                                     "disk_sessions": {}}
        self._load()

    # ------------------------------------------------------------------ persistence (600)
    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for k in ("disk", "devices", "disk_sessions"):
                    if isinstance(d.get(k), dict):
                        self.data[k] = d[k]
                if isinstance(d.get("trusted_device"), str):
                    self.data["trusted_device"] = d["trusted_device"]
                if "tv_sessions" in d:
                    # older versions kept /tv sessions for 30 days: no TV stays authorized across a restart
                    log.warning("%d stored /tv session(s) of an older version ended - every /tv load needs the "
                                "phone's approval now", len(d.get("tv_sessions") or {}))
                    self.save()
        except (OSError, ValueError):
            pass
        self._expire()

    def save(self) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.dir, 0o700)
            except OSError:
                pass
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)

    def _expire(self) -> None:
        now = self.now()
        for store in (self.data["disk_sessions"], self.tv_pages):
            for h in [h for h, s in store.items() if s.get("expires", 0) < now]:
                del store[h]
        devs = self.data["devices"]
        old = [i for i, d in devs.items() if i != self.data.get("trusted_device") and
               now - float(d.get("last_seen") or d.get("created") or 0) > 30 * 86400]
        for i in old:
            del devs[i]

    # ------------------------------------------------------------------ disk password
    @property
    def password_set(self) -> bool:
        return bool(self.data["disk"].get("hash"))

    def ensure_setup_code(self) -> Optional[Path]:
        """No password yet: a one-time setup code in a file only root / the service user can read."""
        if self.password_set:
            if self.code_path.exists():
                self.code_path.unlink()
            return None
        if not self.code_path.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            fd = os.open(self.code_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(human_code(10) + "\n")
            log.warning("/disk has no password yet - the one-time setup code is in %s "
                        "(sudo cat it on the server; it is not written to any log)", self.code_path)
        return self.code_path

    def setup_code_ok(self, code: str) -> bool:
        try:
            real = self.code_path.read_text().strip()
        except OSError:
            return False
        given = re.sub(r"[\s-]", "", str(code or "")).upper()
        return bool(real) and hmac.compare_digest(given.encode(), real.replace("-", "").encode())

    def set_password(self, pw: str) -> None:
        with self._lock:
            self.data["disk"] = {"hash": hash_password(pw), "set_at": self.now()}
            self.data["disk_sessions"] = {}               # every old disk session ends
            self.save()
        if self.code_path.exists():
            self.code_path.unlink()

    def check_password(self, pw: str) -> bool:
        stored = self.data["disk"].get("hash") or ""
        ok = verify_password(stored, pw)
        if ok and _PH is not None and stored.startswith("$argon2") and _PH.check_needs_rehash(stored):
            with self._lock:
                self.data["disk"]["hash"] = _PH.hash(pw)
                self.save()
        return ok

    # ------------------------------------------------------------------ sessions
    def _store(self, kind: str) -> dict:
        """disk sessions are in security.json; TV page sessions only in memory."""
        return self.tv_pages if kind == "tv" else self.data["disk_sessions"]

    def _persist(self, kind: str) -> None:
        if kind != "tv":
            self.save()

    def new_session(self, kind: str, label: str, **meta) -> tuple[str, dict]:
        """kind "disk" -> (token for the cookie, record). Always a fresh random token. (TV page sessions
        come only from consume().)"""
        if kind != "disk":
            raise ValueError("TV sessions only come from an approved challenge")
        token = secrets.token_urlsafe(32)
        now = self.now()
        rec = {"id": secrets.token_hex(6), "created": now, "last_seen": now, "label": label[:80],
               "expires": now + self.disk_ttl, "auth_at": now, "csrf": secrets.token_urlsafe(24), **meta}
        with self._lock:
            self.data["disk_sessions"][sha(token)] = rec
            self.save()
        return token, rec

    def session(self, kind: str, token: Optional[str]) -> Optional[dict]:
        """The session of a cookie. For "tv" this is the cookie alone - the TV endpoints use tv_page(),
        which also needs the page's own secret."""
        if not token or len(token) > 200:
            return None
        store = self._store(kind)
        rec = store.get(sha(token))
        if rec is None:
            return None
        now = self.now()
        if rec.get("expires", 0) < now:
            with self._lock:
                store.pop(sha(token), None)
                self._persist(kind)
            log.info("%s session %s expired", kind.upper(), rec.get("id"))
            return None
        if now - rec.get("last_seen", 0) > 300:            # not every request writes the file
            rec["last_seen"] = now
            with self._lock:
                self._persist(kind)
        return rec

    def revoke(self, kind: str, token: Optional[str] = None, sid: Optional[str] = None) -> bool:
        with self._lock:
            store = self._store(kind)
            key = sha(token) if token else next((h for h, s in store.items() if s.get("id") == sid), None)
            if key is None or key not in store:
                return False
            rec = store.pop(key)
            self._persist(kind)
        log.info("%s session %s (%s) revoked", kind.upper(), rec.get("id"), rec.get("label"))
        return True

    def revoke_all(self, kind: str, except_token: Optional[str] = None) -> int:
        with self._lock:
            keep = sha(except_token) if except_token else None
            store = self._store(kind)
            n = len([h for h in store if h != keep])
            for h in [h for h in store if h != keep]:
                del store[h]
            self._persist(kind)
        log.info("%d %s session(s) revoked", n, kind.upper())
        return n

    def reauth(self, token: str) -> None:
        rec = self.data["disk_sessions"].get(sha(token))
        if rec is not None:
            rec["auth_at"] = self.now()
            self.save()

    def recent(self, rec: dict) -> bool:
        return self.now() - float(rec.get("auth_at") or 0) <= self.reauth_s

    # ------------------------------------------------------------------ remote devices
    def device(self, token: Optional[str]) -> Optional[tuple[str, dict]]:
        if not token or len(token) > 200:
            return None
        h = sha(token)
        for did, d in self.data["devices"].items():
            if hmac.compare_digest(d.get("token_sha", ""), h):
                return did, d
        return None

    def new_device(self, ua: str) -> tuple[str, str, dict]:
        token = secrets.token_urlsafe(32)
        did = secrets.token_hex(8)
        now = self.now()
        rec = {"token_sha": sha(token), "code": human_code(), "name": ua_summary(ua).split(" · ")[0],
               "ua": ua_summary(ua), "created": now, "last_seen": now}
        with self._lock:
            devs = self.data["devices"]
            if len(devs) >= 200:                          # random visitors: drop the oldest untrusted ones
                for i in sorted((i for i in devs if i != self.data.get("trusted_device")),
                                key=lambda i: devs[i].get("last_seen", 0))[:20]:
                    del devs[i]
            devs[did] = rec
            self.save()
        log.info("New remote device %s (%s, code %s)", did, rec["ua"], rec["code"])
        return token, did, rec

    def rename_device(self, did: str, name: str) -> bool:
        name = re.sub(r"[\x00-\x1f<>]", "", str(name or "")).strip()[:40]
        d = self.data["devices"].get(did)
        if not d or not name:
            return False
        d["name"] = name
        self.save()
        return True

    def trusted(self, did: Optional[str]) -> bool:
        return bool(did) and did == self.data.get("trusted_device") and did in self.data["devices"]

    def set_trusted(self, did: Optional[str]) -> None:
        if did is not None and did not in self.data["devices"]:
            raise KeyError("unknown device")
        with self._lock:
            self.data["trusted_device"] = did
            self.save()
        if did:
            d = self.data["devices"][did]
            log.warning("Remote device %s (%s, code %s) is now the trusted /tv approver", did, d.get("name"), d.get("code"))
        else:
            log.warning("No trusted /tv approver device any more")

    def forget_device(self, did: str) -> bool:
        with self._lock:
            if did not in self.data["devices"]:
                return False
            d = self.data["devices"].pop(did)
            if self.data.get("trusted_device") == did:
                self.data["trusted_device"] = None
            self.save()
        log.info("Remote device %s (%s) forgotten", did, d.get("name"))
        return True

    def device_connected(self, did: str, up: bool) -> None:
        c = self.connected.get(did)
        if up:
            if c is None:
                self.connected[did] = {"count": 1, "since": self.now()}
                d = self.data["devices"].get(did) or {}
                d["last_seen"] = self.now()
                log.info("Remote device connected: %s (%s)%s", d.get("name"), d.get("code"),
                         " - trusted approver" if self.trusted(did) else "")
            else:
                c["count"] += 1
        elif c is not None:
            c["count"] -= 1
            if c["count"] <= 0:
                self.connected.pop(did, None)
                d = self.data["devices"].get(did) or {}
                d["last_seen"] = self.now()
                self.save()
                log.info("Remote device disconnected: %s (%s)", d.get("name"), d.get("code"))

    def devices_public(self) -> list[dict]:
        out = []
        for did, d in self.data["devices"].items():
            c = self.connected.get(did)
            out.append({"id": did, "name": d.get("name"), "code": d.get("code"), "device": d.get("ua"),
                        "trusted": self.trusted(did), "connected": c is not None,
                        "connected_since": c["since"] if c else None, "last_seen": d.get("last_seen"),
                        "created": d.get("created")})
        return sorted(out, key=lambda x: (not x["connected"], not x["trusted"], -(x["last_seen"] or 0)))

    # ------------------------------------------------------------------ /tv authorization challenges
    def _expire_requests(self) -> None:
        now = self.now()
        with self._lock:
            for rid, r in list(self.requests.items()):
                if r["status"] in ("pending", "approved") and r["expires"] < now:
                    r["status"] = "expired"
                    log.info("/tv authorization request %s (%s) expired", r["code"], r["device"])
                if now - r["created"] > self.request_ttl + 600:
                    del self.requests[rid]
            for h in [h for h, s in self.tv_pages.items() if s.get("expires", 0) < now]:
                rec = self.tv_pages.pop(h)
                log.info("/tv page %s (%s) expired", rec.get("id"), rec.get("label"))

    def create_request(self, ua: str, requester_device: Optional[str],
                       supersede: Optional[str] = None) -> tuple[str, str, dict]:
        """One load of /tv asks for access -> (browser secret for an HttpOnly cookie, page secret for the
        page's memory, challenge). ``supersede``: the browser secret of this browser's previous challenge,
        which is cancelled (a reload / ASK AGAIN replaces it)."""
        self._expire_requests()
        with self._lock:
            if supersede:
                self.cancel_request(supersede)
            if sum(1 for r in self.requests.values() if r["status"] == "pending") >= 20:
                raise OverflowError("too many open requests")
            browser, page = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            rid = secrets.token_urlsafe(16)
            now = self.now()
            req = {"id": rid, "browser_sha": sha(browser), "page_sha": sha(page), "code": human_code(),
                   "device": ua_summary(ua), "created": now, "expires": now + self.request_ttl, "status": "pending",
                   "requester_device": requester_device, "decided_by": None}
            self.requests[rid] = req
        log.info("/tv authorization requested by %s (request %s)", req["device"], req["code"])
        return browser, page, req

    def cancel_request(self, browser: Optional[str], why: str = "superseded") -> bool:
        """This browser's open challenge ends (the page was reloaded / left, or a new one replaced it)."""
        if not browser or len(browser) > 200:
            return False
        h = sha(browser)
        with self._lock:
            for r in self.requests.values():
                if r["status"] in ("pending", "approved") and hmac.compare_digest(r["browser_sha"], h):
                    r["status"] = why
                    log.info("/tv authorization request %s (%s) %s", r["code"], r["device"], why)
                    return True
        return False

    def request_for(self, browser: Optional[str], page: Optional[str]) -> Optional[dict]:
        """The challenge of THIS page: both the browser's cookie and the page's own secret must match."""
        if not browser or not page or len(browser) > 200 or len(page) > 200:
            return None
        self._expire_requests()
        hb, hp = sha(browser), sha(page)
        return next((r for r in self.requests.values()
                     if hmac.compare_digest(r["browser_sha"], hb) & hmac.compare_digest(r["page_sha"], hp)), None)

    def pending(self) -> list[dict]:
        self._expire_requests()
        return [{"id": r["id"], "code": r["code"], "device": r["device"], "expires_in": round(r["expires"] - self.now())}
                for r in self.requests.values() if r["status"] == "pending"]

    def decide(self, rid: str, approve: bool, by_device: Optional[str] = None) -> str:
        """Only the trusted remote device decides - never the requesting browser, never a /disk login.
        -> "approved" | "denied"; raises PermissionError / KeyError / ValueError."""
        self._expire_requests()
        with self._lock:
            r = self.requests.get(str(rid or ""))
            if r is None:
                raise KeyError("unknown request")
            if r["status"] != "pending":
                raise ValueError(f"request already {r['status']}")
            if by_device is None or not self.trusted(by_device):
                log.warning("/tv request %s: decision from an untrusted remote device %s refused", r["code"], by_device)
                raise PermissionError("this device is not the trusted approver")
            if by_device == r["requester_device"]:
                raise PermissionError("a device cannot approve its own /tv request")
            r["status"] = "approved" if approve else "denied"
            who = (self.data["devices"].get(by_device) or {}).get("name")
            r["decided_by"] = who
        log.warning("/tv authorization %s for %s (request %s) by %s", r["status"].upper(), r["device"], r["code"], who)
        return r["status"]

    def consume(self, r: dict) -> Optional[tuple[str, str, dict]]:
        """The approved page picks up its TV page session - once (atomic: a second call, a replay or a
        late call gets nothing). -> (cookie token, page token, record)."""
        with self._lock:
            if r["status"] != "approved":
                return None
            now = self.now()
            if now > r["expires"]:
                r["status"] = "expired"
                return None
            r["status"] = "consumed"
            cookie, page = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            rec = {"id": secrets.token_hex(6), "created": now, "last_seen": now, "expires": now + self.tv_ttl,
                   "label": r["device"][:80], "approved_by": r.get("decided_by"), "request": r["code"],
                   "page_sha": sha(page)}
            if len(self.tv_pages) >= 100:                 # bounded: the oldest TV page ends
                del self.tv_pages[min(self.tv_pages, key=lambda h: self.tv_pages[h]["created"])]
            self.tv_pages[sha(cookie)] = rec
        log.info("/tv page %s authorized for %s (request %s)", rec["id"], rec["label"], rec["request"])
        return cookie, page, rec

    def tv_page(self, cookie: Optional[str], page: Optional[str]) -> Optional[dict]:
        """An approved TV page: its HttpOnly session cookie AND the page's own secret (only that page has it)."""
        rec = self.session("tv", cookie)
        if rec is None or not page or len(page) > 200:
            return None
        return rec if hmac.compare_digest(rec["page_sha"], sha(page)) else None

    def tv_sessions_public(self) -> list[dict]:
        return [{"id": s["id"], "device": s.get("label"), "created": s.get("created"), "expires": s.get("expires"),
                 "last_seen": s.get("last_seen"), "approved_by": s.get("approved_by")}
                for s in sorted(self.tv_pages.values(), key=lambda s: -s.get("created", 0))]
