"""server/update-f1dash.sh - run for real against a throw-away "server": a bare origin repository, a
checkout of it, a runtime data directory with security state, a .env with a secret, and stand-ins for
systemctl and curl (they record what the script asks for; nothing on this machine is restarted).

Run (from main/):  python -m unittest tests.test_update_script
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "server" / "update-f1dash.sh"
SECRET = "supersecret-remote-token-123"

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
case "$1" in
  cat) [ "$2" = "f1-dashboard.service" ] && [ -f "$FAKE_UNIT" ] && { echo "# $FAKE_UNIT"; cat "$FAKE_UNIT"; exit 0; }; exit 1 ;;
  is-active) shift; [ "$1" = --quiet ] && shift
    if [ "$1" = "f1-dashboard.service" ]; then [ -f "$FAKE_DOWN" ] && exit 3; exit 0; fi
    if [ "$1" = "f1-voyo-player.service" ] && [ -n "${FAKE_PLAYER:-}" ]; then exit 0; fi; exit 3 ;;
  restart) if [ -n "${FAKE_BREAK:-}" ]; then
             if [ -f "$FAKE_BROKE_ONCE" ]; then rm -f "$FAKE_DOWN"; else touch "$FAKE_DOWN" "$FAKE_BROKE_ONCE"; fi
           fi; exit 0 ;;
  *) exit 0 ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env python3
import json, os, sys
from urllib.parse import urlparse
args, out, fmt, method, url = sys.argv[1:], None, None, "GET", None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-w", "-X", "-H", "--data", "--max-time"):
        v = args[i + 1]; i += 2
        if a == "-o": out = v
        elif a == "-w": fmt = v
        elif a == "-X": method = v
        continue
    if a.startswith("http"): url = a
    i += 1
if os.path.exists(os.environ.get("FAKE_DOWN", "/nonexistent")):
    code, body = 0, ""
else:
    m = json.load(open(os.environ["FAKE_CURL_MAP"]))
    path = urlparse(url).path or "/"
    code, body = m.get(f"{method} {path}", m.get(path, [404, ""]))
if out:
    open(out, "w").write(body)
if fmt:
    sys.stdout.write("%03d" % code)
sys.exit(7 if code == 0 else 0)
"""

HEALTHY = {
    "/api/health": [200, '{"ok":true,"recorder":{"state":"REST","busy":false}}'],
    "/": [200, "<html>dashboard</html>"],
    "/api/disk/status": [401, "{}"], "/api/disk/security": [401, "{}"], "/api/tv/status": [401, "{}"],
    "/tv/live/index.m3u8": [401, "{}"], "POST /api/disk/security/decide": [401, "{}"],
    "/tv": [200, '<body class="locked"><div>F1 TV · ACCESS REQUEST</div></body>'],
    "POST /api/tv/auth/status": [200, '{"status":"none"}'],
}


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


@unittest.skipUnless(shutil.which("git") and shutil.which("bash") and shutil.which("flock") and sys.platform.startswith("linux"),
                     "needs git, bash and util-linux")
class UpdateScriptTest(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        seed = self.t / "seed"
        for rel in ("main/server/__init__.py", "main/server/config.py", "main/config/config.toml", "main/requirements.txt",
                    "server/update-f1dash.sh", "server/make-https-cert.sh", "server/config/server.toml",
                    "server/systemd/f1-dashboard.service", "README.md"):
            dst = seed / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / rel, dst)
        (seed / "main" / "tests").mkdir()
        (seed / "main" / "tests" / "__init__.py").write_text("")
        (seed / "main" / "tests" / "test_upd_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n")
        (seed / ".gitignore").write_text(".env\n__pycache__/\n")
        git(self.t, "init", "-q", "-b", "main", str(seed))
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "initial")
        git(self.t, "clone", "-q", "--bare", str(seed), str(self.t / "origin.git"))
        git(seed, "remote", "add", "origin", str(self.t / "origin.git"))
        self.repo = self.t / "repo"
        git(self.t, "clone", "-q", str(self.t / "origin.git"), str(self.repo))
        self.old = git(self.repo, "rev-parse", "HEAD")
        # runtime data with security state
        self.data = self.t / "data"
        (self.data / "auth").mkdir(parents=True)
        (self.data / "auth" / "security.json").write_text('{"disk": {"hash": "$argon2id$v=19$m=65536$x"}, "trusted_device": "abc"}')
        (self.data / "tls").mkdir()
        (self.data / "tls" / "cert.pem").write_text("CERT")
        (self.data / "sync_calibration.json").write_text("{}")
        self.sec_hash = hashlib.sha256((self.data / "auth" / "security.json").read_bytes()).hexdigest()
        env_file = self.repo / "server" / ".env"
        env_file.write_text(f"F1DASH_REMOTE_TOKEN={SECRET}\nF1DASH_SERVER_HTTPS_PORT=0\nF1DASH_VOYO_RECORDING_REQUIRE_MOUNT=\n"
                            f"F1DASH_VOYO_RECORDING_MOUNT_MARKER=\nF1DASH_VOYO_RECORDING_PATH={self.t / 'rec'}\n")
        env_file.chmod(0o600)
        # the systemd unit, systemctl and curl stand-ins, a "venv"
        user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        self.unit = self.t / "f1-dashboard.service"
        self.unit.write_text(f"[Unit]\nDescription=F1\n[Service]\nUser={user}\nWorkingDirectory={self.repo}/main\n"
                             f"Environment=F1DASH_DATA_DIR={self.data}\nEnvironmentFile=-{self.repo}/server/.env\n"
                             f"ExecStart={self.repo}/server/launch.sh\n")
        fb = self.t / "bin"
        fb.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("curl", FAKE_CURL)):
            (fb / name).write_text(body)
            (fb / name).chmod(0o755)
        venv = self.t / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        (venv / "python").chmod(0o755)
        self.map = self.t / "curlmap.json"
        self.set_map(HEALTHY)
        self.log = self.t / "systemctl.log"
        self.backups = self.t / "backups"

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def set_map(self, m):
        self.map.write_text(json.dumps(m))

    def push(self, files: dict, msg="update"):
        seed = self.t / "seed"
        for rel, text in files.items():
            (seed / rel).parent.mkdir(parents=True, exist_ok=True)
            (seed / rel).write_text(text)
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", msg)
        git(seed, "push", "-q", "origin", "main")
        return git(seed, "rev-parse", "HEAD")

    def run_script(self, *args, unit=True, **env_extra):
        if not unit and self.unit.exists():
            self.unit.unlink()
        env = {**os.environ, "PATH": f"{self.t / 'bin'}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "FAKE_UNIT": str(self.unit), "FAKE_CURL_MAP": str(self.map), "FAKE_DOWN": str(self.t / "down"),
               "FAKE_BROKE_ONCE": str(self.t / "broke"), "F1DASH_UPDATE_REQUIRE_ROOT": "0",
               "F1DASH_UPDATE_BACKUP_DIR": str(self.backups), "F1DASH_UPDATE_LOCK": str(self.t / "lock"),
               "F1DASH_VENV": str(self.t / "venv"), "F1DASH_UPDATE_TESTS": "tests.test_upd_ok",
               "F1DASH_UPDATE_HEALTH_TIMEOUT": "6", **env_extra}
        env.pop("F1DASH_DATA_DIR", None)
        r = subprocess.run(["bash", str(self.repo / "server" / "update-f1dash.sh"), "--skip-deps", *args],
                           capture_output=True, text=True, env=env, timeout=300)
        self.assertNotIn(SECRET, r.stdout + r.stderr)                          # never a secret in the output
        return r

    def restarts(self):
        return [ln for ln in (self.log.read_text().splitlines() if self.log.exists() else []) if ln.startswith("restart")]

    def snapshots(self):
        return sorted(p.name for p in self.backups.glob("2*")) if self.backups.exists() else []

    def head(self):
        return git(self.repo, "rev-parse", "HEAD")

    # ------------------------------------------------------------------------------------------------
    def test_help_and_bad_option(self):
        r = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)
        r = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_dry_run_changes_nothing(self):
        new = self.push({"main/server/extra.py": "X = 1\n"})
        refs = git(self.repo, "for-each-ref")
        before = (tree_hash(self.repo), tree_hash(self.data))
        r = self.run_script("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn(f"would fast-forward main {self.old[:12]} -> {new[:12]}", r.stdout)
        self.assertIn("would restart f1-dashboard.service", r.stdout)
        self.assertIn("nothing was changed", r.stdout)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(git(self.repo, "for-each-ref"), refs)               # not even a fetch
        self.assertEqual((tree_hash(self.repo), tree_hash(self.data)), before)
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.restarts(), [])

    def test_refuses_a_dirty_checkout(self):
        self.push({"main/server/extra.py": "X = 1\n"})
        f = self.repo / "main" / "config" / "config.toml"
        f.write_text(f.read_text() + "\n# my local edit\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("local changes", r.stderr)
        self.assertEqual(self.head(), self.old)
        self.assertIn("# my local edit", f.read_text())                       # never discarded
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(self.restarts(), [])

    def test_does_not_update_or_restart_during_a_recording(self):
        self.push({"main/server/extra.py": "X = 1\n"})
        busy = dict(HEALTHY, **{"/api/health": [200, '{"ok":true,"recorder":{"state":"RECORDING","busy":true}}']})
        self.set_map(busy)
        r = self.run_script()
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("RECORDING", r.stderr)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(self.restarts(), [])
        self.assertEqual(self.snapshots(), [])
        # the live stream being written counts too (an older server without the health field)
        self.set_map(HEALTHY)
        (self.data / "live").mkdir()
        (self.data / "live" / "index.m3u8").write_text("#EXTM3U\n")
        r = self.run_script()
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(self.restarts(), [])

    def test_failed_tests_roll_back_and_keep_the_security_state(self):
        self.push({"main/tests/test_upd_ok.py": "import unittest\nclass T(unittest.TestCase):\n"
                   "    def test_ok(self):\n        self.fail('broken release')\n"})
        r = self.run_script()
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn("tests failed", r.stderr)
        self.assertEqual(self.head(), self.old)                               # code back
        self.assertEqual(git(self.repo, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(self.restarts(), [])                                 # service untouched
        self.assertEqual(hashlib.sha256((self.data / "auth" / "security.json").read_bytes()).hexdigest(), self.sec_hash)
        self.assertFalse((self.backups / "last-deploy").exists())              # --rollback never points at it
        snap = self.backups / self.snapshots()[0]
        self.assertEqual(stat.S_IMODE(snap.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((snap / "runtime.tgz").stat().st_mode), 0o600)
        with tarfile.open(snap / "runtime.tgz") as tf:
            names = tf.getnames()
        self.assertIn("auth/security.json", names)
        self.assertIn("tls/cert.pem", names)
        self.assertIn("sync_calibration.json", names)
        self.assertEqual(stat.S_IMODE((snap / "env").stat().st_mode), 0o600)

    def test_update_restart_verify_then_idempotent(self):
        new = self.push({"main/server/extra.py": "X = 1\n"})
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.head(), new)
        self.assertEqual(len(self.restarts()), 1)
        self.assertIn("/tv starts with a fresh approval request", r.stdout)
        self.assertIn(f"previous={self.old}", (self.backups / "last-deploy").read_text())
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(stat.S_IMODE((self.repo / "server" / ".env").stat().st_mode), 0o600)
        self.assertIn(SECRET, (self.repo / "server" / ".env").read_text())     # .env kept as it was
        # again, nothing new: no restart, no backup, state untouched
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already up to date", r.stdout)
        self.assertEqual(len(self.restarts()), 1)
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(hashlib.sha256((self.data / "auth" / "security.json").read_bytes()).hexdigest(), self.sec_hash)

    def test_a_running_player_is_restarted_after_the_dashboard(self):
        # the player unit only Wants= the dashboard now: the update restarts it itself, after the dashboard
        self.push({"main/server/extra.py": "X = 1\n"})
        r = self.run_script(FAKE_PLAYER="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.restarts(), ["restart f1-dashboard.service", "restart f1-voyo-player.service"])
        self.assertIn("f1-voyo-player.service restarted - runs the new code", r.stdout)

    def test_rollback_goes_back_to_the_previous_deploy(self):
        new = self.push({"main/server/extra.py": "X = 1\n"})
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.head(), new)
        r = self.run_script("--rollback", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.head(), new)                                     # dry run: still new
        r = self.run_script("--rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(len(self.restarts()), 2)
        self.assertEqual(len(self.snapshots()), 2)                            # a backup before the rollback too
        self.assertEqual(hashlib.sha256((self.data / "auth" / "security.json").read_bytes()).hexdigest(), self.sec_hash)
        r = self.run_script()                                                  # the next update goes forward again
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.head(), new)

    def test_documentation_only_needs_no_restart(self):
        new = self.push({"README.md": "docs\n"})
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.head(), new)
        self.assertEqual(self.restarts(), [])

    def test_unhealthy_new_version_is_rolled_back(self):
        self.push({"main/server/extra.py": "X = 1\n"})
        r = self.run_script(FAKE_BREAK="1")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertIn("rolled back", r.stderr)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(len(self.restarts()), 2)                             # the update, then the rollback
        self.assertEqual(hashlib.sha256((self.data / "auth" / "security.json").read_bytes()).hexdigest(), self.sec_hash)

    def test_failed_security_check_counts_as_unhealthy(self):
        self.push({"main/server/extra.py": "X = 1\n"})
        leaky = dict(HEALTHY, **{"/api/tv/status": [200, "{}"]})               # e.g. /tv open without approval
        self.set_map(leaky)
        r = self.run_script()
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertIn("FAIL /tv status without approval", r.stdout)
        self.assertEqual(self.head(), self.old)

    def test_development_checkout(self):
        new = self.push({"main/server/extra.py": "X = 1\n"})
        r = self.run_script(unit=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("development checkout", r.stdout)
        self.assertEqual(self.head(), new)
        self.assertEqual(self.restarts(), [])
        self.assertFalse(self.backups.exists())


if __name__ == "__main__":
    unittest.main()
