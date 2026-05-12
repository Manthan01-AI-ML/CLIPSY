"""
backend/core/url_security.py

SSRF protection (master doc Part 8).

Problem: A user submits http://169.254.169.254/latest/meta-data → yt-dlp
requests it → your server leaks AWS credentials. Classic SSRF.

Solution: Before passing any URL to yt-dlp, verify:
  1. Scheme is http/https
  2. Host resolves to a public IP (not private/loopback/link-local)
  3. Host is a recognized video platform (extra defense)
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Private/internal ranges that must NEVER be accessed.
# These match RFC 1918, loopback, link-local, and AWS metadata service.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("10.0.0.0/8"),       # private class A
    ipaddress.ip_network("172.16.0.0/12"),    # private class B
    ipaddress.ip_network("192.168.0.0/16"),   # private class C
    ipaddress.ip_network("169.254.0.0/16"),   # link-local (AWS metadata)
    ipaddress.ip_network("0.0.0.0/8"),        # "this network"
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),         # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
]

# Allowed video platforms. Tight allowlist — far safer than "any URL".
# Add more as you support more sources.
_ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    # "vimeo.com", "www.vimeo.com",     # enable when supported
}


class UnsafeURLError(ValueError):
    """Raised when a URL points to an unsafe destination."""


def validate_youtube_url(url: str) -> str:
    """
    Validate a YouTube URL. Returns the cleaned URL.
    Raises UnsafeURLError on any problem.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise UnsafeURLError(f"Malformed URL: {e}") from e

    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Scheme '{parsed.scheme}' not allowed. Use http or https.")

    if not parsed.hostname:
        raise UnsafeURLError("URL missing hostname")

    host = parsed.hostname.lower()
    if host not in _ALLOWED_HOSTS:
        raise UnsafeURLError(
            f"Host '{host}' not allowed. Allowed: {sorted(_ALLOWED_HOSTS)}"
        )

    # Resolve hostname → IP and check it's not private.
    # Important: this check must run even for allowed hosts, because DNS can be
    # manipulated (DNS rebinding attacks).
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURLError(f"Could not resolve host '{host}': {e}") from e

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        for blocked in _BLOCKED_NETWORKS:
            if ip in blocked:
                raise UnsafeURLError(
                    f"Host '{host}' resolves to blocked IP {ip} ({blocked})"
                )

    return url