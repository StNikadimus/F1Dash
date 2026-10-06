#!/usr/bin/env python3
"""Linux input-event -> dashboard remote bridge.

Reads key presses from a Linux input device (an IR receiver decoded by the
kernel's rc-core, a USB remote, a keyboard, ...) and forwards the key names to
the dashboard's whitelisted remote API:

    POST http://<server>:8080/api/remote/key   {"key": "KEY_UP"}

It never grabs the device (unless --grab), so other programs still receive
the same key presses.

    pip install evdev
    python3 server/bridge/evdev_bridge.py --list
    python3 server/bridge/evdev_bridge.py --test --device /dev/input/event3
    python3 server/bridge/evdev_bridge.py --server http://192.168.1.10:8080 --name gpio_ir_recv
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover
    sys.exit("python-evdev is required:  pip install evdev")

log = logging.getLogger("evdev_bridge")


def list_devices() -> None:
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        caps = d.capabilities().get(ecodes.EV_KEY, [])
        print(f"{path:22} {d.name!r:40} phys={d.phys!r} keys={len(caps)}")


def find_device(path: str | None, name: str | None) -> evdev.InputDevice:
    if path:
        return evdev.InputDevice(path)
    for p in evdev.list_devices():
        d = evdev.InputDevice(p)
        if name and name.lower() in d.name.lower():
            return d
    raise FileNotFoundError(f"no input device matching name {name!r}")


def send(server: str, key: str, token: str | None) -> None:
    body = json.dumps({"key": key}).encode()
    req = urllib.request.Request(server.rstrip("/") + "/api/remote/key", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Remote-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            log.info("%s -> %s", key, r.status)
    except urllib.error.HTTPError as e:
        log.warning("%s rejected by server: HTTP %s %s", key, e.code, e.read()[:120])
    except OSError as e:
        log.warning("server unreachable: %s", e)


def run(args) -> None:
    last: dict[str, float] = {}
    while True:
        try:
            dev = find_device(args.device, args.name)
            log.info("Listening on %s (%s)%s", dev.path, dev.name, " [grabbed]" if args.grab else "")
            if args.grab:
                dev.grab()
            for ev in dev.read_loop():
                if ev.type != ecodes.EV_KEY:
                    continue
                if ev.value == 0 or (ev.value == 2 and not args.repeat):
                    continue
                name = ecodes.KEY.get(ev.code) or ecodes.BTN.get(ev.code)
                if isinstance(name, list):
                    name = name[0]
                if not name:
                    continue
                now = time.monotonic()
                if now - last.get(name, 0) < args.debounce:
                    continue
                last[name] = now
                if args.test:
                    print(f"{name}  (code {ev.code}, value {ev.value})", flush=True)
                else:
                    send(args.server, name, args.token)
        except KeyboardInterrupt:
            return
        except (OSError, FileNotFoundError) as e:
            log.warning("input device problem: %s - retrying in 5 s", e)
            time.sleep(5)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--device", help="/dev/input/eventN")
    ap.add_argument("--name", help="substring of the input device name (alternative to --device)")
    ap.add_argument("--token", help="remote token if configured on the server")
    ap.add_argument("--list", action="store_true", help="list input devices and exit")
    ap.add_argument("--test", action="store_true", help="only print key names, do not send")
    ap.add_argument("--repeat", action="store_true", help="forward auto-repeat events")
    ap.add_argument("--debounce", type=float, default=0.15, help="seconds between identical keys")
    ap.add_argument("--grab", action="store_true", help="exclusive access (other programs stop seeing keys)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.list:
        list_devices()
        return
    if not args.device and not args.name:
        ap.error("--device or --name is required")
    run(args)


if __name__ == "__main__":
    main()
