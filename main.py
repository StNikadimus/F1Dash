#!/usr/bin/env python3
"""F1 TV Dashboard - start the server.

    python main.py                 # mode from config/config.toml (default: live)
    python main.py --test          # simulator (TEST MODE)
    python main.py --replay        # replay [replay] source from the config
    python main.py --replay data/recordings/xyz.jsonl.gz
    python main.py --replay latest # newest finished session from the F1 archive
    python main.py --vod           # follow a VOYO recording (session detected from its title)
    python main.py --vod 11253     # ... of this OpenF1 session_key
    python main.py --f1-login      # sign in to F1 TV again (opens the browser)
    python main.py --f1-status     # F1 TV sign-in state;  --f1-logout deletes it
    python main.py --diagnose 120  # which live topics / cars actually deliver data (then exit)
"""
from __future__ import annotations

import argparse
import logging
import sys

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 or newer is required")

import uvicorn  # noqa: E402

from server.app import create_app  # noqa: E402
from server.config import load_config  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Self-hosted F1 live timing dashboard for TV screens")
    p.add_argument("--config", help="path to config TOML (default: config/config.toml)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--live", action="store_true", help="use the official F1 live timing feed")
    g.add_argument("--test", action="store_true", help="run the built-in simulator (TEST MODE)")
    g.add_argument("--replay", nargs="?", const="", metavar="SOURCE",
                   help="replay a recording file, an F1 archive path or 'latest'")
    g.add_argument("--vod", nargs="?", const="auto", metavar="SESSION_KEY",
                   help="follow a VOYO recording: session from the VOYO title (auto) or an OpenF1 session_key")
    p.add_argument("--f1-login", action="store_true",
                   help="forget the stored F1 TV sign-in and sign in again in the browser (live mode)")
    p.add_argument("--f1-logout", action="store_true", help="delete the stored F1 TV sign-in and exit")
    p.add_argument("--f1-status", action="store_true", help="show the F1 TV sign-in state (no secrets) and exit")
    p.add_argument("--diagnose", nargs="?", const=90.0, type=float, metavar="SECONDS",
                   help="connect to F1 live timing for SECONDS (default 90), print which topics / cars "
                        "actually deliver data, and exit")
    p.add_argument("--speed", type=float, help="replay speed factor")
    p.add_argument("--delay", type=float, help="fixed delay of N seconds instead of the VOYO playback clock")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    args = p.parse_args()

    # bootstrap logging before config so config messages are visible
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    cfg = load_config(args.config)
    if args.live:
        cfg["source"]["mode"] = "live"
    elif args.test:
        cfg["source"]["mode"] = "test"
    elif args.vod is not None:
        cfg["source"]["mode"] = "vod"
        cfg["vod"]["session_key"] = args.vod
    elif args.replay is not None:
        cfg["source"]["mode"] = "replay"
        if args.replay:
            cfg["replay"]["source"] = args.replay
    if args.speed:
        cfg["replay"]["speed"] = args.speed
        cfg["test"]["time_scale"] = args.speed
    if args.delay is not None:
        # fixed delay on the F1 event-time axis (no VOYO clock)
        cfg["source"]["delay_seconds"] = args.delay
        cfg["sync"]["broadcast_delay_seconds"] = args.delay
        cfg["sync"]["mode"] = "DELAY" if args.delay > 0 else "LIVE"
    if args.host:
        cfg["server"]["host"] = args.host
    if args.port:
        cfg["server"]["port"] = args.port

    if args.f1_status or args.f1_logout:
        from server.app import make_auth
        auth = make_auth(cfg)
        if args.f1_logout:
            print("F1 TV sign-in deleted." if auth.logout() else "No stored F1 TV sign-in.")
            return
        info = auth.public_info()
        print(f"F1 TV subscription mode: {'ENABLED' if info['subscription'] else 'DISABLED'}")
        print(f"Sign-in: {info['state']} ({info['reason']})")
        if info["state"] == "VALID":
            print(f"Product: {info['product'] or '-'} · status: {info['subscription_status'] or '-'} · "
                  f"valid until: {info['expires_utc']}")
        return
    if args.f1_login:
        cfg["source"]["mode"] = "live"
        cfg["f1_tv"]["subscription"] = True
        cfg["_force_login"] = True
    if args.diagnose is not None:
        cfg["source"]["mode"] = "live"
        logging.getLogger().setLevel("INFO")
        import asyncio
        from server.app import diagnose
        asyncio.run(diagnose(cfg, max(10.0, float(args.diagnose))))
        return

    level = str(cfg["server"].get("log_level", "INFO")).upper()
    logging.getLogger().setLevel(level)
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    app = create_app(cfg)
    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    logging.getLogger("main").info("Dashboard: http://%s:%d/  (mode: %s)",
                                   "localhost" if host in ("0.0.0.0", "::") else host, port,
                                   cfg["source"]["mode"].upper())
    uvicorn.run(app, host=host, port=port, log_level=level.lower(), access_log=False,
                ws_max_size=64 * 1024, proxy_headers=False)


if __name__ == "__main__":
    main()
