"""SSRF guard for outbound requests to user-supplied URLs (webhooks).

Following the MCP security best practices (spec 2025-11-25) and OWASP SSRF guidance:
* only http/https, no credentials in the URL;
* resolve the host and refuse loopback, private (RFC 1918 / ULA), link-local
  (incl. 169.254.169.254 cloud metadata), CGNAT, multicast, reserved and unspecified
  addresses — for *every* resolved address, not just the first;
* re-check at send time and *pin* the connection to the address that was checked
  (Host header + TLS SNI keep the original name), so a short-TTL DNS-rebinding domain
  can't pass the check and then connect to 127.0.0.1 or 169.254.169.254;
* never follow redirects.
Private targets are allowed only with WEBHOOK_ALLOW_PRIVATE=true or when the host is
listed in WEBHOOK_ALLOWLIST (e.g. an internal n8n/Slack relay on the corporate LAN).
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Iterable
from urllib.parse import urlsplit

from .config import CONFIG

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class UnsafeURL(ValueError):
    pass


def _bad_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
            or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT))


def _resolve(host: str, port: int) -> Iterable[str]:
    try:
        return {ai[4][0] for ai in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)}
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve {host!r}") from e


def check_url(url: str, resolve: bool = True) -> str:
    """Validate a webhook URL; returns it normalised or raises UnsafeURL."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("only http(s) URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeURL("credentials in the URL are not allowed — use the signing secret instead")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeURL("URL has no host")
    if len(url) > 2048:
        raise UnsafeURL("URL too long")
    if host in CONFIG.webhook_allowlist or CONFIG.webhook_allow_private:
        return url
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeURL(f"{host} is a local name")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _bad_ip(literal):
            raise UnsafeURL(f"{host} is a private/loopback/reserved address")
        return url
    if resolve:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        for addr in _resolve(host, port):
            if _bad_ip(ipaddress.ip_address(addr.split("%")[0])):
                raise UnsafeURL(f"{host} resolves to a private/loopback/reserved address ({addr})")
    return url


def pinned(url: str) -> tuple[str, dict, dict]:
    """Validate `url` and return (request_url, extra_headers, httpx_extensions) that make
    the request go to the exact public IP that was validated. Allow-listed/private-allowed
    hosts are returned unpinned (the operator vouched for them)."""
    url = check_url(url, resolve=False)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in CONFIG.webhook_allowlist or CONFIG.webhook_allow_private:
        return url, {}, {}
    try:
        ipaddress.ip_address(host)
        return url, {}, {}                     # literal IP already checked
    except ValueError:
        pass
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addrs = sorted(_resolve(host, port))
    for addr in addrs:
        if _bad_ip(ipaddress.ip_address(addr.split("%")[0])):
            raise UnsafeURL(f"{host} resolves to a private/loopback/reserved address ({addr})")
    ip = addrs[0]
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        netloc_ip += f":{parts.port}"
    target = parts._replace(netloc=netloc_ip).geturl()
    host_header = host + (f":{parts.port}" if parts.port else "")
    ext = {"sni_hostname": host} if parts.scheme == "https" else {}
    return target, {"host": host_header}, ext
