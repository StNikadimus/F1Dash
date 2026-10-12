"""BACKFILL (tools/voyo_backfill.py, server/backfill.py, /disk): the current / last Grand Prix's sessions that are
missing on the disk, from VOYO's recordings, one at a time, by the server VOYO player.

The player side runs against a fake player whose "SessionRecorder" goes through the real states (DISCOVERING ->
OPENING (key) -> VERIFYING -> RECORDING -> DONE, or NOT_FOUND / FAILED) and a fake disk that the dashboard server
would have checked (COMPLETE / INCOMPLETE). The server side runs on the real app.

Run (from main/):  python -m unittest tests.test_backfill
"""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import voyo_backfill as bfm  # noqa: E402
from tools.voyo_backfill import ORDER, Backfill  # noqa: E402
from tools.voyo_session import Target  # noqa: E402

GP = "Singapore Grand Prix"
IDS = {"practice1": "63660752", "practice2": "63660800", "practice3": "63660900", "sprint_qualifying": "63660945",
       "sprint": "63661233", "qualifying": "63661274", "race": "63661400"}
NAMES = {"practice1": "Practice 1", "practice2": "Practice 2", "practice3": "Practice 3",
         "sprint_qualifying": "Sprint Qualifying", "sprint": "Sprint", "qualifying": "Qualifying", "race": "Race"}


def ts(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def window(kind, start, meeting=GP, hours=1.5):
    return {"meeting": meeting, "session_name": NAMES[kind], "kind": kind,
            "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(start + hours * 3600, timezone.utc).isoformat(),
            "open_from": start - 900, "open_until": start + hours * 3600 + 1800}


# the Singapore weekend: every kind in the schedule (FP2 / FP3 were cancelled - VOYO has nothing for them)
WEEKEND = [window(k, ts(2026, 10, 9, 9, 30) + i * 6 * 3600) for i, k in enumerate(ORDER)]
NEXT_GP = [window("practice1", ts(2026, 10, 23, 17, 30), "United States Grand Prix")]
AFTER = ts(2026, 10, 12, 9, 0)                      # Monday after the weekend


class FakeSession:
    """The SessionRecorder's visible surface; ``plan`` decides how it goes."""

    def __init__(self, target, plan):
        self.target, self.plan = target, plan
        self.state, self.key, self.episode, self.selection, self.problem = "DISCOVERING", None, None, None, ""
        self.recording_s, self.end_reason, self.ticks = 0.0, "", 0

    def tick(self):
        self.ticks += 1
        p = self.plan
        if p == "missing":
            self.state = "NOT_FOUND"
            self.selection = {"state": "NOT_FOUND", "reason": f"no {self.target.kind} recording on the page"}
            return
        day = datetime.fromtimestamp(self.target.start, timezone.utc).strftime("%Y%m%d")
        if self.state == "DISCOVERING":
            self.selection = {"state": "SELECTED", "reason": "found"}
            self.key = f"{self.target.kind}-{IDS[self.target.kind]}-{day}"
            self.episode = {"id": IDS[self.target.kind]}
            self.state = "OPENING"
        elif self.state == "OPENING":
            self.state = "VERIFYING"
        elif self.state == "FAILED":                   # SessionRecorder: tried again after retry_after_fail_s
            self.state = "DISCOVERING"
        elif self.state == "VERIFYING":
            if p == "fail":
                self.state, self.problem = "FAILED", "wrong recording: the player plays another episode"
            else:
                self.state = "RECORDING"
        elif self.state == "RECORDING":
            self.recording_s += 600
            if self.recording_s >= 1800:
                self.state, self.end_reason = "DONE", "the recording ended"


class FakePlayer:
    def __init__(self, plans, disk):
        self.plans, self.disk = plans, disk
        self.session = None
        self.opened, self.closed, self.states, self.max_open = [], [], [], 0
        self.url = "https://voyo.si/vsebina/vn-singapurja"

    def event_url(self):
        return self.url

    def open_session(self, target, url):
        assert self.session is None, "a second session while one is open"
        self.opened.append(target)
        self.session = FakeSession(target, self.plans.get(target.kind, "ok"))

    def session_tick(self):
        self.session.tick()
        self.states.append((self.session.target.kind, self.session.state))
        if self.session.state == "DONE":                    # the player's finish_session: the server stores it
            self.disk[self.session.key] = {"status": "closed", "check": "INCOMPLETE" if self.session.target.kind in
                                           self.plans.get("_incomplete", ()) else "COMPLETE", "issues": []}

    def close_session(self, why):
        ss = self.session
        if ss is not None and ss.state == "RECORDING":       # the recording is closed as it is (partial)
            self.disk[ss.key] = {"status": "closed", "check": "INCOMPLETE", "issues": ["video covers 10 of 90 min"]}
        self.closed.append((ss.target.kind if ss else None, why))
        self.session = None


class Harness:
    def __init__(self, plans=None, disk=None, windows=None, saved=None):
        self.disk = dict(disk or {})
        self.player = FakePlayer(dict(plans or {}), self.disk)
        self.saved = {"job": saved}
        self.windows = list(WEEKEND if windows is None else windows)
        self.bf = Backfill(self.player, lambda now: self.windows, self.verify, lambda: self.saved["job"],
                           self.save, Target, log=lambda s: None)
        self.verify_calls = []

    def verify(self, key):
        self.verify_calls.append(key)
        return self.disk.get(key)

    def save(self, job):
        self.saved["job"] = json.loads(json.dumps(job))

    def on(self, run="r1", stop=0):
        self.bf.control({"enabled": True, "run": run, "stop": stop})

    def run(self, now=AFTER, steps=400, live=None, upcoming=()):
        for i in range(steps):
            self.bf.step(now + i * 5, live, list(upcoming))
            if self.bf.job and self.bf.job["state"] == "finished":
                break
        return self.bf.job

    def status(self):
        return {s["kind"]: s["status"] for s in self.bf.job["sessions"]}


class OrderAndSkippingTest(unittest.TestCase):
    def test_sessions_in_the_fixed_order_one_at_a_time(self):
        h = Harness(plans={"practice2": "missing", "practice3": "missing"})
        h.on()
        job = h.run()
        self.assertEqual(job["state"], "finished")
        self.assertEqual([t.kind for t in h.player.opened], list(ORDER))       # looked for in this order
        self.assertEqual(h.status(), {"practice1": "completed", "practice2": "unavailable", "practice3": "unavailable",
                                      "sprint_qualifying": "completed", "sprint": "completed", "qualifying": "completed",
                                      "race": "completed"})
        # never two at once: each session was closed before the next one was opened (FakePlayer asserts it), and
        # the recordings follow each other
        rec = [k for k, st in h.player.states if st == "RECORDING"]
        self.assertEqual(list(dict.fromkeys(rec)), ["practice1", "sprint_qualifying", "sprint", "qualifying", "race"])
        self.assertEqual(job["done"] if "done" in job else h.bf.report()["done"], 7)
        # the scheduled start goes into the key (the live recording's key) and the date check of the selection
        self.assertEqual(h.player.opened[0].start, ts(2026, 10, 9, 9, 30))
        self.assertEqual(h.player.opened[0].meeting, GP)

    def test_missing_sessions_are_said_and_the_job_goes_on(self):
        h = Harness(plans={"practice1": "missing", "sprint": "missing"})
        h.on()
        h.run()
        by = {s["kind"]: s for s in h.bf.job["sessions"]}
        self.assertEqual(by["practice1"]["status"], "unavailable")
        self.assertIn("not available on VOYO", by["practice1"]["reason"])
        self.assertIn("skipped", by["practice1"]["reason"])
        self.assertEqual(by["race"]["status"], "completed")

    def test_a_failed_recording_does_not_stop_the_rest(self):
        h = Harness(plans={"sprint_qualifying": "fail"})
        h.on()
        h.run()
        by = {s["kind"]: s for s in h.bf.job["sessions"]}
        self.assertEqual(by["sprint_qualifying"]["status"], "failed")
        self.assertIn("can retry", by["sprint_qualifying"]["reason"])
        self.assertIn("wrong recording", by["sprint_qualifying"]["reason"])
        self.assertEqual([by[k]["status"] for k in ("sprint", "qualifying", "race")], ["completed"] * 3)
        self.assertNotIn("sprint_qualifying", [k for k, st in h.player.states if st == "RECORDING"])

    def test_sessions_not_in_the_schedule_or_not_over_yet(self):
        sprint_weekend = [w for w in WEEKEND if w["kind"] not in ("practice2", "practice3")]
        h = Harness(windows=sprint_weekend)
        h.on()
        h.run()
        by = {s["kind"]: s for s in h.bf.job["sessions"]}
        self.assertEqual((by["practice2"]["status"], by["practice2"]["reason"]),
                         ("skipped", "not in this weekend's F1 schedule"))
        self.assertNotIn("practice2", [t.kind for t in h.player.opened])
        # Saturday evening: the race is still to come - the live recording records it
        h = Harness()
        h.on()
        h.run(now=ts(2026, 10, 10, 20, 0))
        by = {s["kind"]: s for s in h.bf.job["sessions"]}
        self.assertEqual(by["race"]["status"], "skipped")
        self.assertIn("live recording records it", by["race"]["reason"])

    def test_the_last_grand_prix_is_chosen(self):
        h = Harness(windows=WEEKEND + NEXT_GP)
        h.on()
        h.run(steps=1)
        self.assertEqual(h.bf.job["meeting"], GP)                  # the US GP has not begun yet
        self.assertIsNone(bfm.pick_meeting(NEXT_GP, AFTER))
        h = Harness(windows=[])
        h.on()
        h.run(steps=2)
        self.assertEqual(h.bf.job["state"], "finished")
        self.assertIn("no Grand Prix", h.bf.job["note"])


class VerificationAndOverwriteTest(unittest.TestCase):
    def test_completed_only_after_the_servers_check(self):
        h = Harness(plans={"_incomplete": ("qualifying",)})
        h.on()
        h.run()
        by = {s["kind"]: s for s in h.bf.job["sessions"]}
        self.assertEqual(by["race"]["status"], "completed")
        self.assertIn("COMPLETE", by["race"]["reason"])
        self.assertEqual(by["qualifying"]["status"], "failed")              # recorded, but the check said INCOMPLETE
        self.assertIn("INCOMPLETE", by["qualifying"]["reason"])

    def test_check_still_on_its_way(self):
        h = Harness()
        h.on()
        disk = h.disk
        orig = h.player.session_tick

        def late():                                           # the server has not stored the check yet
            orig()
            ss = h.player.session
            if ss and ss.state == "DONE":
                disk[ss.key] = {"status": "closed", "check": None}
        h.player.session_tick = late
        i = 0
        while h.bf.job is None or h.bf.job["sessions"][0]["status"] != "verifying":
            h.bf.step(AFTER + i * 5, None, [])
            i += 1
        for k in range(10):                                                 # 50 s: within VERIFY_S
            h.bf.step(AFTER + (i + k) * 5, None, [])
        p1 = h.bf.job["sessions"][0]
        self.assertEqual(p1["status"], "verifying")                         # waits for it, not "completed"
        self.assertIsNone(h.player.session)                                 # nothing else started meanwhile
        disk[p1["key"]]["check"] = "COMPLETE"
        h.bf.step(AFTER + (i + 11) * 5, None, [])
        self.assertEqual(h.bf.job["sessions"][0]["status"], "completed")

    def test_recordings_on_the_disk_are_never_recorded_again(self):
        q = f"qualifying-{IDS['qualifying']}-20261010"
        s = f"sprint-{IDS['sprint']}-20261010"
        h = Harness(disk={q: {"status": "closed", "check": "COMPLETE"},
                          s: {"status": "interrupted", "check": "INCOMPLETE"}})
        h.on()
        h.run()
        by = {x["kind"]: x for x in h.bf.job["sessions"]}
        self.assertEqual(by["qualifying"]["status"], "completed")
        self.assertIn("already on the disk", by["qualifying"]["reason"])
        self.assertEqual(by["sprint"]["status"], "skipped")
        self.assertIn("not overwritten", by["sprint"]["reason"])
        recorded = [k for k, st in h.player.states if st in ("VERIFYING", "RECORDING")]
        self.assertNotIn("qualifying", recorded)                            # closed before anything was played
        self.assertNotIn("sprint", recorded)
        self.assertEqual(h.disk[s]["check"], "INCOMPLETE")                  # untouched

    def test_server_not_answering_records_nothing(self):
        h = Harness()

        def down(key):
            raise OSError("connection refused")
        h.bf.verify = down
        h.on()
        h.run()
        self.assertEqual(set(h.status().values()), {"failed"})
        self.assertFalse([k for k, st in h.player.states if st == "RECORDING"])
        self.assertIn("could not ask the dashboard server", h.bf.job["sessions"][0]["reason"])


class ControlTest(unittest.TestCase):
    def test_nothing_before_the_server_said_and_nothing_when_off(self):
        h = Harness()
        h.run(steps=5)
        self.assertIsNone(h.bf.job)
        h.bf.control({"enabled": False, "run": None, "stop": 0})
        h.run(steps=5)
        self.assertIsNone(h.bf.job)
        self.assertEqual(h.player.opened, [])

    def test_off_finishes_the_current_one_and_starts_nothing_new(self):
        h = Harness()
        h.on()
        for i in range(3):
            h.bf.step(AFTER + i * 5, None, [])
        self.assertEqual(h.bf.active, "practice1")
        h.bf.control({"enabled": False, "run": "r1", "stop": 0})
        h.run(steps=30)
        self.assertEqual(h.status()["practice1"], "completed")             # finished safely, not cut off
        self.assertEqual(h.status()["practice2"], "pending")
        self.assertEqual(h.bf.job["state"], "paused")
        self.assertEqual([t.kind for t in h.player.opened], ["practice1"])
        h.on("r1")                                                          # on again: the same run goes on
        h.run()
        self.assertEqual(h.bf.job["run"], "r1")
        self.assertEqual(h.bf.job["state"], "finished")
        self.assertEqual(len([t for t in h.player.opened if t.kind == "practice1"]), 1)

    def test_the_same_run_twice_is_one_job(self):
        h = Harness()
        for _ in range(3):
            h.on("r1")
            h.bf.step(AFTER, None, [])
        self.assertEqual(len(h.player.opened), 1)

    def test_stop_now_ends_the_current_recording_keeping_it(self):
        h = Harness()
        h.on(stop=0)
        for i in range(6):
            h.bf.step(AFTER + i * 5, None, [])
        self.assertEqual(h.player.session.state, "RECORDING")
        key = h.player.session.key
        h.bf.control({"enabled": False, "run": "r1", "stop": 1})
        h.run(steps=60)
        p1 = h.bf.job["sessions"][0]
        self.assertEqual(p1["status"], "failed")
        self.assertIn("stopped on /disk", p1["reason"])
        self.assertIn("partial recording", p1["reason"])
        self.assertEqual(h.disk[key]["check"], "INCOMPLETE")                 # closed, kept, not called complete
        self.assertEqual([t.kind for t in h.player.opened], ["practice1"])   # nothing new after it


class LivePriorityTest(unittest.TestCase):
    def test_nothing_starts_while_a_live_session_is_on(self):
        h = Harness()
        h.on()
        h.run(steps=10, live=NEXT_GP[0])
        self.assertEqual(h.player.opened, [])
        self.assertIn("live session is on", h.bf.job["note"])

    def test_waits_when_a_live_session_would_begin_during_the_recording(self):
        h = Harness()
        h.on()
        soon = dict(NEXT_GP[0], open_from=AFTER + 1800)                     # in 30 min: FP1 would not be done
        h.run(steps=10, upcoming=[soon])
        self.assertEqual(h.player.opened, [])
        self.assertIn("waiting: a live session begins", h.bf.job["note"])
        later = dict(NEXT_GP[0], open_from=AFTER + 12 * 3600)
        h.run(steps=2, upcoming=[later])
        self.assertEqual([t.kind for t in h.player.opened], ["practice1"])

    def test_a_live_window_stops_the_backfill_recording(self):
        h = Harness()
        h.on()
        for i in range(6):
            h.bf.step(AFTER + i * 5, None, [])
        self.assertEqual(h.player.session.state, "RECORDING")
        h.bf.step(AFTER + 40, NEXT_GP[0], [])                               # the live window opened
        self.assertIsNone(h.player.session)                                 # the player is free for the live one
        h.bf.step(AFTER + 45, NEXT_GP[0], [])
        p1 = h.bf.job["sessions"][0]
        self.assertEqual(p1["status"], "failed")
        self.assertIn("live one comes first", p1["reason"])
        h.run(steps=5, live=NEXT_GP[0])
        self.assertEqual([t.kind for t in h.player.opened], ["practice1"])  # nothing new during the live one

    def test_stopped_before_recording_goes_back_to_pending(self):
        h = Harness()
        h.on()
        h.bf.step(AFTER, None, [])                                          # opened, still looking
        h.bf.step(AFTER + 5, NEXT_GP[0], [])
        self.assertEqual(h.bf.job["sessions"][0]["status"], "pending")
        self.assertIn("done later", h.bf.job["sessions"][0]["reason"])
        h.run()
        self.assertEqual(h.status()["practice1"], "completed")


class PersistenceTest(unittest.TestCase):
    def test_a_restart_keeps_the_job_and_says_what_was_interrupted(self):
        h = Harness()
        h.on()
        i = 0
        while not (h.bf.active == "practice2" and h.player.session and h.player.session.state == "RECORDING"):
            h.bf.step(AFTER + i * 5, None, [])
            i += 1
        saved = h.saved["job"]
        self.assertEqual([s["status"] for s in saved["sessions"]][:2], ["completed", "recording"])
        h2 = Harness(saved=saved, disk=h.disk)                               # the player restarted
        self.assertEqual(h2.bf.job["sessions"][1]["status"], "failed")
        self.assertIn("interrupted", h2.bf.job["sessions"][1]["reason"])
        self.assertEqual(h2.saved["job"]["sessions"][1]["status"], "failed")   # saved at once - no false state
        h2.on("r1")
        h2.run()
        self.assertEqual(h2.status()["practice1"], "completed")
        self.assertEqual(h2.status()["race"], "completed")
        self.assertNotIn("practice1", [t.kind for t in h2.player.opened])    # not done twice
        h3 = Harness(saved=h2.saved["job"])
        h3.on("r2")                                                         # a new run (after the finished one)
        h3.run(steps=1)
        self.assertEqual(h3.bf.job["run"], "r2")


class ServerControlTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_switch_runs_persistence_and_states(self):
        from server.backfill import BackfillControl
        c = BackfillControl(self.d / "backfill.json")
        self.assertEqual(c.public(None)["state"], "disabled")
        run = c.set(True)["run"]
        self.assertTrue(run)
        self.assertEqual(c.set(True)["run"], run)                           # clicked again: the same run
        self.assertEqual(c.public(5)["state"], "starting")
        self.assertEqual(c.public(None)["state"], "waiting for the player")
        c.report({"run": "someone-else", "state": "running", "sessions": []})
        self.assertIsNone(c.public(5)["job"])                               # only the current run counts
        job = {"run": run, "meeting": GP, "state": "running", "current": "Sprint", "done": 3, "total": 7,
               "sessions": [{"kind": "sprint", "label": "Sprint", "status": "recording", "reason": "<b>x</b>" * 200,
                             "attempts": 1}]}
        c.report(job)
        p = c.public(5)
        self.assertEqual((p["state"], p["job"]["current"], p["job"]["done"]), ("processing", "Sprint", 3))
        self.assertLessEqual(len(p["job"]["sessions"][0]["reason"]), 300)
        c2 = BackfillControl(self.d / "backfill.json")                      # the dashboard server restarted
        self.assertEqual((c2.wanted["enabled"], c2.wanted["run"], c2.public(5)["job"]["meeting"]), (True, run, GP))
        c2.set(False)
        self.assertEqual(c2.public(5)["state"], "stopping")                 # the current session is finished first
        c2.set(True)
        self.assertEqual(c2.wanted["run"], run)                             # not finished: the same run continues
        c2.report(dict(job, state="finished", current=None, note="7 completed"))
        p = c2.public(5)
        self.assertEqual((p["state"], p["enabled"]), ("finished", False))  # done: OFF by itself
        self.assertNotEqual(c2.set(True)["run"], run)                       # ON again: a new run
        self.assertEqual(c2.stop_now()["stop"], 1)
        self.assertFalse(c2.wanted["enabled"])


class DiskApiTest(unittest.TestCase):
    """/api/disk/backfill on the real app: the /disk session and its CSRF token; the player's heartbeat carries
    the switch out and the job in; the package check is local only."""

    def setUp(self):
        from test_security import Server
        import server.app as appmod
        self.d = Path(tempfile.mkdtemp())
        self._lp = mock.patch.object(appmod, "LOOPBACK", appmod.LOOPBACK | {"testclient"})
        self._lp.start()
        self.s = Server(self.d)
        self.admin = self.s.browser()
        self.H = {"X-F1-CSRF": self.s.setup_password(self.admin)}

    def tearDown(self):
        self.s.close()
        self._lp.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_switch_status_and_heartbeat(self):
        anon = self.s.browser()
        self.assertEqual(anon.get("/api/disk/backfill").status_code, 401)
        self.assertEqual(anon.post("/api/disk/backfill", json={"enabled": True}).status_code, 401)
        self.assertEqual(self.admin.post("/api/disk/backfill", json={"enabled": True}).status_code, 403)   # no CSRF
        self.assertEqual(self.admin.post("/api/disk/backfill", json={"enabled": "yes"}, headers=self.H).status_code, 400)
        r = self.admin.post("/api/disk/backfill", json={"enabled": True}, headers=self.H)
        self.assertEqual(r.status_code, 200, r.text)
        run = r.json()["run"]
        self.assertTrue(r.json()["enabled"])
        # the player's heartbeat: the switch goes out, its job comes in
        ans = self.admin.post("/api/voyo/player/status", content=json.dumps({"state": "idle"})).json()
        self.assertEqual(ans["backfill"], {"enabled": True, "run": run, "stop": 0})
        job = {"run": run, "meeting": GP, "state": "running", "current": "Practice 2", "done": 1, "total": 7,
               "sessions": [{"kind": "practice1", "label": "Practice 1", "status": "completed", "reason": "ok"},
                            {"kind": "practice2", "label": "Practice 2", "status": "recording", "reason": "recording"}]}
        self.admin.post("/api/voyo/player/status", content=json.dumps({"state": "open", "backfill": job}))
        st = self.admin.get("/api/disk/status").json()["backfill"]
        self.assertEqual((st["state"], st["job"]["current"], st["job"]["sessions"][0]["status"]),
                         ("processing", "Practice 2", "completed"))
        self.assertEqual(self.admin.get("/api/disk/backfill").json()["job"]["meeting"], GP)
        r = self.admin.post("/api/disk/backfill", json={"action": "stop"}, headers=self.H)
        self.assertEqual(r.json()["enabled"], False)
        ans = self.admin.post("/api/voyo/player/status", content=json.dumps({"state": "open"})).json()
        self.assertEqual(ans["backfill"]["stop"], 1)
        self.assertTrue(json.loads((self.d / "backfill.json").read_text())["job"]["meeting"] == GP)
        page = self.admin.get("/disk").text
        self.assertIn('id="p-bf"', page)
        self.assertIn("bf-toggle", self.admin.get("/disk-static/disk.js").text)

    def test_package_check_is_local_and_says_only_the_check(self):
        from test_tv_replays import make_pkg
        rec = self.d / "rec"
        pkg = make_pkg(rec, "race-63661400-20261011")
        m = json.loads((pkg / "manifest.json").read_text())
        m["capture"]["check"] = {"state": "INCOMPLETE", "issues": ["video covers 10.0 of 90.0 min"], "recorded_s": 600}
        (pkg / "manifest.json").write_text(json.dumps(m))
        r = self.admin.get("/api/voyo/player/package/race-63661400-20261011")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["check"], r.json()["issues"]), ("INCOMPLETE", ["video covers 10.0 of 90.0 min"]))
        self.assertNotIn(str(self.d), r.text)
        self.assertEqual(self.admin.get("/api/voyo/player/package/qualifying-63661274-20261010").status_code, 404)
        self.assertEqual(self.admin.get("/api/voyo/player/package/..%2F..%2Fetc").status_code, 404)
        self._lp.stop()                                                     # not from this machine: refused
        try:
            self.assertEqual(self.s.browser().get("/api/voyo/player/package/race-63661400-20261011").status_code, 403)
        finally:
            self._lp.start()


class RunLoopTest(unittest.TestCase):
    """The player's real loop (cmd_run) with backfill ON: it records in the gaps, and a live window takes the
    player at once - the backfill recording is stopped first, then the live session is opened; never both."""

    def test_live_window_takes_the_player_from_the_backfill(self):
        import tools.voyo_server_player as vsp
        live_win = dict(window("practice1", AFTER + 3600, "United States Grand Prix"), open_from=AFTER - 60)
        opened, closed, state = [], [], {"st": {}}
        player = mock.Mock()
        player.sp = {"enabled": True, "when": "schedule", "record_sessions": list(ORDER)}
        player.session, player.is_open, player.server = None, False, "http://127.0.0.1:1"
        player.episode_mode.return_value = True
        player.event_url.return_value = "https://voyo.si/vsebina/vn-singapurja"
        player.stream_url.return_value = "https://voyo.si/vsebina/vn-singapurja"
        player.feed_live.return_value = False
        player._flushed, player.done_window = -1e9, None

        def open_session(target, url):
            self.assertIsNone(player.session, "two sessions at once")
            opened.append((target.kind, target.meeting))
            player.session = FakeSession(target, "ok")

        def session_tick():
            player.session.tick()

        def close_session(why):
            closed.append((player.session.target.kind if player.session else None, why))
            if player.session is not None and player.session.target.meeting == GP:
                player.session = None                 # (the live one is kept to look at it afterwards)
        player.open_session.side_effect = open_session
        player.session_tick.side_effect = session_tick
        player.close_session.side_effect = close_session
        loops = {"n": 0}

        def current(_now):
            loops["n"] += 1
            if loops["n"] > 9:
                raise KeyboardInterrupt
            return live_win if loops["n"] >= 6 else None

        def hb(p, nxt, problem=""):
            p.backfill.control({"enabled": True, "run": "r1", "stop": 0})
            return []
        clock = {"t": AFTER}

        def now():
            clock["t"] += 5
            return clock["t"]
        with mock.patch.object(vsp, "Schedule") as S, mock.patch.object(vsp, "heartbeat", hb), \
                mock.patch.object(vsp.time, "sleep", lambda s: None), mock.patch.object(vsp, "find_browser", return_value="c"), \
                mock.patch.object(vsp.signal, "signal"), mock.patch.object(vsp.time, "time", now), \
                mock.patch.object(vsp, "load_state", lambda: dict(state["st"])), \
                mock.patch.object(vsp, "save_state", lambda d: state.update(st=json.loads(json.dumps(d)))), \
                mock.patch.object(vsp, "package_info", lambda server, key: None):
            S.return_value.windows.return_value = []
            S.return_value.all_windows.side_effect = lambda now: WEEKEND
            S.return_value.current.side_effect = current
            vsp.cmd_run(player)
        self.assertEqual(opened[0], ("practice1", GP))                        # the backfill used the gap
        self.assertEqual(opened[1], ("practice1", "United States Grand Prix"))  # then the live one, at once
        self.assertEqual(len(opened), 2)
        self.assertIn("live one comes first", closed[0][1])                   # the backfill's was ended first
        job = state["st"]["backfill_job"]
        self.assertEqual(job["sessions"][0]["status"], "verifying")          # its partial recording is checked
        self.assertIn("live one comes first", job["sessions"][0]["reason"])   # (when the live one is over)
        self.assertGreater(player.session.ticks, 1)                           # the live one was driven, by the live code
        self.assertEqual(player.session.target.meeting, "United States Grand Prix")


class PlayerHeartbeatTest(unittest.TestCase):
    """The player's heartbeat carries its job out and the /disk switch in (tools/voyo_server_player.heartbeat)."""

    def test_heartbeat_round_trip(self):
        from tools import voyo_server_player as vsp
        p = vsp.Player.__new__(vsp.Player)
        p.login_result, p.session, p.capture, p.hint, p.note, p.sp = None, None, None, None, None, {}
        p.bridge = None                                                     # is_open: False
        p.event_url = lambda: "https://voyo.si/vsebina/x"
        p.stream_url = lambda: ""
        p.episode_mode = lambda: True
        h = Harness()
        p.backfill = h.bf
        sent = []

        def post(path, body, timeout=1.5):
            sent.append(body)
            return {"ok": True, "commands": [], "backfill": {"enabled": True, "run": "r9", "stop": 0}}
        with mock.patch.object(vsp, "_post", post), mock.patch.object(vsp, "find_browser", lambda sp: None), \
                mock.patch.object(vsp, "has_widevine", lambda b: False):
            vsp.heartbeat(p, None)
            self.assertEqual(h.bf.ctl, {"enabled": True, "run": "r9", "stop": 0})
            h.bf.step(AFTER, None, [])
            vsp.heartbeat(p, None)
        self.assertIsNone(sent[0]["backfill"])
        self.assertEqual((sent[1]["backfill"]["run"], sent[1]["backfill"]["current"]), ("r9", "Practice 1"))


if __name__ == "__main__":
    unittest.main()
