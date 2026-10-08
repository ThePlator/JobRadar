"""Outbound-request guard: only talk to public internet hosts (blocks SSRF via job links)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket


async def is_public_host(host: str) -> bool:
    """True only if every address the host resolves to is a public (global) IP."""
    host = host.strip("[]").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass  # a name, not a literal IP
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)
