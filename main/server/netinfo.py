"""Where the phone remote can be reached: http://<this PC>:<port>/remote.

LAN address: the source address the OS picks for an outgoing route (a UDP "connect" sends no
packet), plus the addresses the host name resolves to. Tailscale: the source address towards
Tailscale's MagicDNS address 100.100.100.100 - only when that is in Tailscale's 100.64.0.0/10
range (otherwise Tailscale is not running). Nothing is hard-coded; nothing is sent anywhere.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Optional

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _route_ip(target: str) -> Optional[str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.2)
        s.connect((target, 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def addresses() -> dict:
    lan: list[str] = []
    ts: list[str] = []

    def add(ip: Optional[str]) -> None:
        if not ip:
            return
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return
        if a.version != 4 or a.is_loopback or a.is_link_local or a.is_unspecified:
            return
        if ip not in lan and ip not in ts:
            (ts if a in TAILSCALE_NET else lan).append(ip)
    add(_route_ip("192.168.0.1"))           # the LAN interface the OS would use
    add(_route_ip("8.8.8.8"))
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    add(_route_ip("100.100.100.100"))       # Tailscale MagicDNS: only answers with a 100.x address when running
    return {"lan": lan, "tailscale": ts}


def remote_info(host: str, port: int, token: str = "") -> dict:
    """URLs of the phone remote. ``host`` = the address the server is bound to."""
    q = f"?token={token}" if token else ""
    local_only = host in ("127.0.0.1", "localhost", "::1")
    addrs = {"lan": [], "tailscale": []} if local_only else addresses()
    mk = lambda ip: f"http://{ip}:{port}/remote{q}"            # noqa: E731
    urls = [mk(ip) for ip in addrs["lan"]]
    ts = [mk(ip) for ip in addrs["tailscale"]]
    return {"url": (urls or ts or [f"http://localhost:{port}/remote{q}"])[0], "lan": urls, "tailscale": ts,
            "bound": host, "port": port, "local_only": local_only, "token_required": bool(token),
            "note": ("The server listens on 127.0.0.1 only - set [server] host = \"0.0.0.0\" for phones."
                     if local_only else
                     f"Same Wi-Fi as this PC. Windows Firewall: allow inbound TCP {port} (Private network).")}
