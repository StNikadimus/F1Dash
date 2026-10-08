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
* **TV DEVICE** - a browser that opened /tv and was approved by the trusted remote device (or by the
  disk user in /disk). It gets its own session cookie.

Sessions: ``secrets.token_urlsafe(32)`` tokens in HttpOnly cookies, only their SHA-256 stored (in
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
        self.tv_ttl = float(c.get("tv_session_days", 30)) * 86400
        self.disk_ttl = float(c.get("disk_session_hours", 12)) * 3600
        self.reauth_s = float(c.get("reauth_minutes", 10)) * 60
        self.request_ttl = float(c.get("tv_request_seconds", 120))
        self.now = clock
        self._lock = threading.RLock()
        self.requests: dict[str, dict] = {}           # TV authorization requests - memory only, short-lived
        self.connected: dict[str, dict] = {}          # device id -> {"count", "since"} (open /remote sockets)
        self.data: dict[str, Any] = {"disk": {"hash": None}, "trusted_device": None, "devices": {},
                                     "tv_sessions": {}, "disk_sessions": {}}
        self._load()

    # ------------------------------------------------------------------ persistence (600)
    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for k in ("disk", "devices", "tv_sessions", "disk_sessions"):
                    if isinstance(d.get(k), dict):
                        self.data[k] = d[k]
                if isinstance(d.get("trusted_device"), str):
                    self.data["trusted_device"] = d["trusted_device"]
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
        for kind in ("tv_sessions", "disk_sessions"):
            for h in [h for h, s in self.data[kind].items() if s.get("expires", 0) < now]:
                del self.data[kind][h]
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
    def new_session(self, kind: str, label: str, **meta) -> tuple[str, dict]:
        """kind "disk" | "tv" -> (token for the cookie, record). Always a fresh random token."""
        token = secrets.token_urlsafe(32)
        now = self.now()
        rec = {"id": secrets.token_hex(6), "created": now, "last_seen": now, "label": label[:80],
               "expires": now + (self.disk_ttl if kind == "disk" else self.tv_ttl), **meta}
        if kind == "disk":
            rec.update(auth_at=now, csrf=secrets.token_urlsafe(24))
        with self._lock:
            self.data[f"{kind}_sessions"][sha(token)] = rec
            self.save()
        return token, rec

    def session(self, kind: str, token: Optional[str]) -> Optional[dict]:
        if not token or len(token) > 200:
            return None
        rec = self.data[f"{kind}_sessions"].get(sha(token))
        if rec is None:
            return None
        now = self.now()
        if rec.get("expires", 0) < now:
            with self._lock:
                self.data[f"{kind}_sessions"].pop(sha(token), None)
                self.save()
            log.info("%s session %s expired", kind.upper(), rec.get("id"))
            return None
        if now - rec.get("last_seen", 0) > 300:            # not every request writes the file
            rec["last_seen"] = now
            with self._lock:
                self.save()
        return rec

    def revoke(self, kind: str, token: Optional[str] = None, sid: Optional[str] = None) -> bool:
        with self._lock:
            store = self.data[f"{kind}_sessions"]
            key = sha(token) if token else next((h for h, s in store.items() if s.get("id") == sid), None)
            if key is None or key not in store:
                return False
            rec = store.pop(key)
            self.save()
        log.info("%s session %s (%s) revoked", kind.upper(), rec.get("id"), rec.get("label"))
        return True

    def revoke_all(self, kind: str, except_token: Optional[str] = None) -> int:
        with self._lock:
            keep = sha(except_token) if except_token else None
            store = self.data[f"{kind}_sessions"]
            n = len([h for h in store if h != keep])
            self.data[f"{kind}_sessions"] = {h: s for h, s in store.items() if h == keep}
            self.save()
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

    # ------------------------------------------------------------------ /tv authorization requests
    def _expire_requests(self) -> None:
        now = self.now()
        for rid, r in list(self.requests.items()):
            if r["status"] == "pending" and r["expires"] < now:
                r["status"] = "expired"
                log.info("/tv authorization request %s (%s) expired", r["code"], r["device"])
            if now - r["created"] > self.request_ttl + 600:
                del self.requests[rid]

    def create_request(self, ua: str, requester_device: Optional[str], requester_disk: Optional[str]) -> tuple[str, dict]:
        """-> (poll secret for the TV's HttpOnly cookie, request)."""
        self._expire_requests()
        if sum(1 for r in self.requests.values() if r["status"] == "pending") >= 20:
            raise OverflowError("too many open requests")
        poll = secrets.token_urlsafe(32)
        rid = secrets.token_urlsafe(12)
        now = self.now()
        req = {"id": rid, "poll_sha": sha(poll), "code": human_code(), "device": ua_summary(ua), "created": now,
               "expires": now + self.request_ttl, "status": "pending", "requester_device": requester_device,
               "requester_disk": requester_disk, "decided_by": None, "consumed": False}
        self.requests[rid] = req
        log.info("/tv authorization requested by %s (request %s)", req["device"], req["code"])
        return poll, req

    def request_by_poll(self, poll: Optional[str]) -> Optional[dict]:
        if not poll or len(poll) > 200:
            return None
        self._expire_requests()
        h = sha(poll)
        return next((r for r in self.requests.values() if hmac.compare_digest(r["poll_sha"], h)), None)

    def pending(self) -> list[dict]:
        self._expire_requests()
        return [{"id": r["id"], "code": r["code"], "device": r["device"], "expires_in": round(r["expires"] - self.now())}
                for r in self.requests.values() if r["status"] == "pending"]

    def decide(self, rid: str, approve: bool, by_device: Optional[str] = None, by_disk: Optional[str] = None,
               by_poll: Optional[str] = None) -> str:
        """The server decides who may decide: the trusted remote device, or a /disk session - never the
        requesting browser itself. -> "approved" | "denied"; raises PermissionError / KeyError / ValueError."""
        self._expire_requests()
        r = self.requests.get(str(rid or ""))
        if r is None:
            raise KeyError("unknown request")
        if r["status"] != "pending":
            raise ValueError(f"request already {r['status']}")
        if by_device is not None:
            if not self.trusted(by_device):
                log.warning("/tv request %s: decision from an untrusted remote device %s refused", r["code"], by_device)
                raise PermissionError("this device is not the trusted approver")
            if by_device == r["requester_device"]:
                raise PermissionError("a device cannot approve its own /tv request")
        elif by_disk is not None:
            if by_disk == r["requester_disk"]:
                raise PermissionError("a browser cannot approve its own /tv request")
        else:
            raise PermissionError("no approver")
        if by_poll is not None and hmac.compare_digest(sha(by_poll), r["poll_sha"]):
            raise PermissionError("a browser cannot approve its own /tv request")
        r["status"] = "approved" if approve else "denied"
        who = (self.data["devices"].get(by_device) or {}).get("name") if by_device else "the /disk user"
        r["decided_by"] = who
        log.warning("/tv authorization %s for %s (request %s) by %s", r["status"].upper(), r["device"], r["code"], who)
        return r["status"]

    def consume(self, r: dict) -> Optional[tuple[str, dict]]:
        """The approved TV browser picks up its session - once."""
        if r["status"] != "approved" or r["consumed"]:
            return None
        r["consumed"] = True
        return self.new_session("tv", r["device"], approved_by=r.get("decided_by"))

    def tv_sessions_public(self) -> list[dict]:
        return [{"id": s["id"], "device": s.get("label"), "created": s.get("created"), "expires": s.get("expires"),
                 "last_seen": s.get("last_seen"), "approved_by": s.get("approved_by")}
                for s in sorted(self.data["tv_sessions"].values(), key=lambda s: -s.get("created", 0))]
