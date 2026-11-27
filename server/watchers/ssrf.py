# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Strict IP-pinned HTTP/HTTPS fetcher with SSRF, DNS-rebinding, and size protections."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

DEFAULT_USER_AGENT = "agy-home-server-watcher/1.0 (+https://github.com/zyecheng/agy-home-server)"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
DEFAULT_MAX_REDIRECTS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


class SSRFError(ValueError):
    """Raised when a URL or resolved IP violates SSRF egress policy."""


class ResponseTooLargeError(ValueError):
    """Raised when an HTTP response body exceeds the configured byte limit."""


@dataclass(frozen=True)
class FetchResult:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes
    not_modified: bool = False


def validate_ip_address(raw_ip: str, *, allow_loopback: bool = False) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Validate that an IP address is strictly public/global (no loopback, RFC1918, multicast, NAT64, etc.)."""
    try:
        ip = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    except ValueError as exc:
        raise SSRFError(f"Invalid IP address: {raw_ip!r}") from exc

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if allow_loopback and ip.is_loopback:
        return ip

    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64_WELL_KNOWN or ip in _NAT64_LOCAL:
            raise SSRFError(f"Blocked NAT64 IPv6 address: {ip}")

    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise SSRFError(f"Blocked non-global IP address: {ip}")

    return ip


def resolve_and_validate_host(
    host: str,
    port: int,
    *,
    allow_loopback: bool = False,
) -> tuple[str, int]:
    """Resolve host via DNS, validate EVERY returned address, and return (pinned_ip, family)."""
    clean_host = (host or "").strip().rstrip(".")
    if not clean_host:
        raise SSRFError("Empty hostname")

    try:
        infos = socket.getaddrinfo(clean_host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SSRFError(f"DNS resolution failed for {clean_host!r}: {exc}") from exc

    if not infos:
        raise SSRFError(f"DNS returned no addresses for {clean_host!r}")

    validated: list[tuple[str, int]] = []
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        raw_ip = str(sockaddr[0])
        ip_obj = validate_ip_address(raw_ip, allow_loopback=allow_loopback)
        validated.append((str(ip_obj), family))

    # Prefer IPv4 when both A and AAAA are validated for predictable connectivity
    for ip_str, family in validated:
        if family == socket.AF_INET:
            return ip_str, family
    return validated[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects strictly to a pre-validated IP literal."""

    def __init__(
        self,
        orig_host: str,
        port: int,
        pinned_ip: str,
        *,
        timeout: float,
    ) -> None:
        super().__init__(orig_host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to a pinned IP while preserving TLS SNI & cert verification."""

    def __init__(
        self,
        orig_host: str,
        port: int,
        pinned_ip: str,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(orig_host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._orig_host = orig_host

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_sock, server_hostname=self._orig_host)
        except Exception:
            raw_sock.close()
            raise


def _read_bounded(resp: http.client.HTTPResponse, max_bytes: int) -> bytes:
    raw_len = resp.getheader("Content-Length")
    if raw_len is not None:
        try:
            if int(raw_len) > max_bytes:
                raise ResponseTooLargeError(
                    f"Response Content-Length exceeds {max_bytes:,} bytes"
                )
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(f"Response stream exceeded {max_bytes:,} bytes")
        chunks.append(chunk)
    raw_body = b"".join(chunks)

    encoding = (resp.getheader("Content-Encoding") or "").strip().lower()
    if encoding in ("gzip", "deflate") and raw_body:
        wbits = 16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
        decomp = zlib.decompressobj(wbits)
        try:
            out = decomp.decompress(raw_body, max_bytes + 1)
            if len(out) > max_bytes or decomp.unconsumed_tail:
                raise ResponseTooLargeError(
                    f"Decompressed response exceeded {max_bytes:,} bytes"
                )
            tail = decomp.flush()
            if len(out) + len(tail) > max_bytes:
                raise ResponseTooLargeError(
                    f"Decompressed response exceeded {max_bytes:,} bytes"
                )
            return out + tail
        except zlib.error as exc:
            raise ValueError(f"Invalid {encoding} response payload: {exc}") from exc
    return raw_body


def validate_url_syntax(url: str) -> tuple[str, str, int, str]:
    """Validate URL scheme/syntax and return (scheme, host, port, request_target)."""
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise SSRFError(f"Unsupported URL scheme {parsed.scheme!r}; only http/https allowed")
    if parsed.username is not None or parsed.password is not None:
        raise SSRFError("URLs with embedded credentials are not allowed")
    host = parsed.hostname
    if not host:
        raise SSRFError("URL is missing hostname")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise SSRFError(f"Invalid port in URL: {exc}") from exc
    if port < 1 or port > 65535:
        raise SSRFError(f"Invalid port: {port}")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return scheme, host, port, path


def safe_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    allow_loopback_for_tests: bool = False,
) -> FetchResult:
    """Fetch a public HTTP(S) URL with IP-pinning on every redirect hop and strict size cap."""
    current_url = url.strip()
    ssl_ctx = ssl.create_default_context()

    for hop in range(max_redirects + 1):
        scheme, host, port, target = validate_url_syntax(current_url)
        pinned_ip, _family = resolve_and_validate_host(
            host, port, allow_loopback=allow_loopback_for_tests
        )

        req_headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        if headers:
            for k, v in headers.items():
                if v is not None and v != "":
                    req_headers[k] = v

        default_port = 443 if scheme == "https" else 80
        host_header = host if port == default_port else f"{host}:{port}"
        if ":" in host and not host.startswith("["):
            host_header = f"[{host}]" if port == default_port else f"[{host}]:{port}"
        req_headers["Host"] = host_header

        conn: http.client.HTTPConnection
        if scheme == "https":
            conn = _PinnedHTTPSConnection(
                host, port, pinned_ip, timeout=timeout, context=ssl_ctx
            )
        else:
            conn = _PinnedHTTPConnection(host, port, pinned_ip, timeout=timeout)

        try:
            conn.request("GET", target, headers=req_headers)
            resp = conn.getresponse()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            status = int(resp.status)

            if status in _REDIRECT_STATUSES:
                location = resp.getheader("Location")
                # Drain minimally before closing
                conn.close()
                if not location:
                    raise SSRFError(f"HTTP {status} redirect missing Location header")
                if hop >= max_redirects:
                    raise SSRFError(f"Exceeded max redirects ({max_redirects})")
                current_url = urljoin(current_url, location.strip())
                continue

            if status == 304:
                return FetchResult(
                    status=304,
                    url=current_url,
                    headers=resp_headers,
                    body=b"",
                    not_modified=True,
                )

            body = _read_bounded(resp, max_bytes=max_bytes)
            return FetchResult(
                status=status,
                url=current_url,
                headers=resp_headers,
                body=body,
                not_modified=False,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    raise SSRFError(f"Exceeded max redirects ({max_redirects})")
