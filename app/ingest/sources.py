"""Validation of external sources (GitHub, Drive, live URLs).

Principle: we never fetch an arbitrary user-supplied URL. GitHub and Drive links
are parsed into (owner, repo) / file-id and fetched through the provider's API
host only. Live-app URLs go through an SSRF check and, in production, an egress
proxy that blocks private address space (see DESIGN.md §7).
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

GITHUB_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$"
)
DRIVE_RE = re.compile(r"^https://drive\.google\.com/(?:drive/folders/|file/d/)(?P<id>[A-Za-z0-9_-]{10,})")


class InvalidSource(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GitHubRepo:
    owner: str
    repo: str

    @property
    def archive_api_url(self) -> str:
        # Tarball via the API host - no git protocol, no submodules, no hooks executed.
        return f"https://api.github.com/repos/{self.owner}/{self.repo}/tarball"


def parse_github_url(url: str) -> GitHubRepo:
    m = GITHUB_RE.match(url.strip())
    if not m:
        raise InvalidSource("invalid_github_url", "expected https://github.com/<owner>/<repo>")
    repo = m.group("repo")
    if repo in {".", ".."}:
        raise InvalidSource("invalid_github_url", "invalid repository name")
    return GitHubRepo(m.group("owner"), repo)


def parse_drive_url(url: str) -> str:
    m = DRIVE_RE.match(url.strip())
    if not m:
        raise InvalidSource("invalid_drive_url", "expected a drive.google.com folder or file link")
    return m.group("id")


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local  # includes 169.254.169.254 (cloud metadata)
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_live_url(url: str, resolve: bool = True) -> str:
    """Reject URLs that could be used for SSRF. Resolution is re-checked at
    connect time by the egress proxy to defeat DNS rebinding."""
    p = urlparse(url.strip())
    if p.scheme not in {"https", "http"}:
        raise InvalidSource("ssrf_blocked", "only http(s) URLs are allowed")
    if not p.hostname:
        raise InvalidSource("ssrf_blocked", "URL has no host")
    if p.username or p.password:
        raise InvalidSource("ssrf_blocked", "credentials in URL are not allowed")
    if p.port and p.port not in (80, 443, 8080, 8443):
        raise InvalidSource("ssrf_blocked", f"port {p.port} not allowed")
    host = p.hostname
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        if host == "localhost" or host.endswith((".internal", ".local", ".localhost")):
            raise InvalidSource("ssrf_blocked", "internal hostname") from None
        if not resolve:
            return url
        try:
            candidates = list({ai[4][0] for ai in socket.getaddrinfo(host, None)})
        except socket.gaierror as e:
            raise InvalidSource("unresolvable_host", f"cannot resolve {host}") from e
    if not all(_is_public_ip(ip) for ip in candidates):
        raise InvalidSource("ssrf_blocked", "URL resolves to a non-public address")
    return url
