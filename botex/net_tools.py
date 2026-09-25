# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Restricted, read-only public web access for BoteX."""
import asyncio
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit

try:
    from .security import mask_secrets
except (ImportError, ValueError):
    from security import mask_secrets

_TEXT_CONTENT_TYPES = {
    "text/html", "text/plain", "text/markdown", "application/json",
    "application/xml", "text/xml",
}
_REDIRECT_CODES = {301, 302, 303, 307, 308}
NET_POLICIES = {"allowlist", "caller", "public", "off"}


def _normalize_host(host: str) -> str:
    host = (host or "").rstrip(".").lower()
    if not host:
        return ""
    if ":" in host:
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _clean_host_entries(values) -> list[str]:
    hosts = []
    for raw in values or []:
        value = str(raw).strip()
        if not value:
            continue
        if value == "*":
            raise ValueError(
                "Wildcard '*' is not an allowlist entry; use net.policy='public'."
            )
        if "://" in value:
            parts = _parse_url(value)
            value = parts.hostname or ""
        elif any(ch in value for ch in "/?#"):
            raise ValueError(f"Invalid allowed host entry: {raw}")
        host = _normalize_host(value)
        if host:
            hosts.append(host)
    return hosts


def _validate_url_rules(values) -> list[str]:
    rules = []
    for raw in values or []:
        value = str(raw).strip()
        if value:
            _parse_url(value)
            rules.append(value)
    return rules


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    host = _normalize_host(host)
    for raw in allowed_hosts or []:
        item = _normalize_host(str(raw).lstrip("*."))
        if item and (host == item or host.endswith("." + item)):
            return True
    return False


def _effective_port(parts) -> int:
    return parts.port or (443 if parts.scheme == "https" else 80)


def _url_allowed(parts, allowed_urls: list[str]) -> bool:
    current_host = _normalize_host(parts.hostname)
    current_path = parts.path or "/"
    current_port = _effective_port(parts)
    for raw in allowed_urls or []:
        try:
            rule = _parse_url(raw)
        except ValueError:
            continue
        if (rule.scheme != parts.scheme
                or _normalize_host(rule.hostname) != current_host
                or _effective_port(rule) != current_port):
            continue
        rule_path = rule.path or "/"
        if rule.query or not (rule_path == "/" or rule_path.endswith("/")):
            if current_path == rule_path and parts.query == rule.query:
                return True
        elif current_path.startswith(rule_path):
            return True
    return False


def _parse_url(url: str):
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string.")
    url = url.strip()
    if len(url) > 2048:
        raise ValueError("URL is too long.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("URL contains control characters.")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValueError(f"Invalid URL: {exc}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Only absolute http(s) URLs are allowed.")
    if parts.username or parts.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL contains an invalid port.") from exc
    default_port = 443 if parts.scheme == "https" else 80
    if port not in (None, default_port):
        raise ValueError("Only default http/https ports are allowed.")
    return parts


def _public_ips(host: str, scheme: str = "https") -> list[str]:
    host = _normalize_host(host)
    port = 443 if scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Host could not be resolved: {host}") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise ValueError(f"Host could not be resolved: {host}")
    for value in addresses:
        ip = ipaddress.ip_address(value)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"Host resolves to a non-public address: {host}")
    return addresses


def resolve_net_scope(net_cfg: dict, request_hosts=None, request_urls=None) -> dict:
    """Resolve server policy + per-run grants into a host/URL authorization scope."""
    if not net_cfg.get("enabled"):
        return {"enabled": False, "policy": "off", "allowed_hosts": [], "allowed_urls": []}
    policy = str(net_cfg.get("policy", "caller")).strip().lower()
    if policy not in NET_POLICIES:
        raise ValueError(
            "Unknown net.policy "
            f"'{policy}'. Expected one of: allowlist, caller, public, off."
        )
    if policy == "off":
        return {"enabled": True, "policy": "off", "allowed_hosts": [], "allowed_urls": []}

    config_hosts = _clean_host_entries(net_cfg.get("allowed_hosts", []))
    config_urls = _validate_url_rules(net_cfg.get("allowed_urls", []))
    hosts = _clean_host_entries(request_hosts or [])
    urls = _validate_url_rules(request_urls or [])

    if policy == "allowlist":
        if hosts and not all(_host_allowed(host, config_hosts) for host in hosts):
            raise ValueError("Per-run host is outside the configured net.allowed_hosts.")
        if urls:
            for rule in urls:
                parts = _parse_url(rule)
                if not (_host_allowed(parts.hostname, config_hosts)
                        or _url_allowed(parts, config_urls)):
                    raise ValueError(
                        "Per-run URL is outside the configured net scope."
                    )
        # Per-run grants replace the configured defaults for this run, but have
        # already been validated as a subset of the configured scope above.
        if hosts or urls:
            allowed_hosts = hosts
            allowed_urls = urls
        else:
            allowed_hosts = config_hosts
            allowed_urls = config_urls
    elif policy == "caller":
        if hosts or urls:
            allowed_hosts = hosts
            allowed_urls = urls
        else:
            allowed_hosts = config_hosts
            allowed_urls = config_urls
    else:
        # Public mode permits every public host unless the caller narrows the
        # run with net_allowed_hosts / net_allowed_urls.
        allowed_hosts = hosts
        allowed_urls = urls

    if policy != "public" and not allowed_hosts and not allowed_urls:
        raise ValueError(
            "read_url is enabled but no host or URL authorization was provided."
        )
    return {
        "enabled": True,
        "policy": policy,
        "allowed_hosts": allowed_hosts,
        "allowed_urls": allowed_urls,
    }


def _validate_url_parts(url: str, allowed_hosts: list[str], *,
                        policy: str = "allowlist",
                        allowed_urls: list[str] | None = None):
    if policy not in NET_POLICIES:
        raise ValueError(
            f"Unknown net.policy '{policy}'. Expected one of: "
            "allowlist, caller, public, off."
        )
    parts = _parse_url(url)
    if policy == "public":
        if allowed_hosts or allowed_urls:
            allowed = (_host_allowed(parts.hostname, allowed_hosts)
                       or _url_allowed(parts, allowed_urls or []))
        else:
            allowed = True
    elif policy == "off":
        raise ValueError("Network access is disabled by net.policy=off.")
    else:
        allowed = (_host_allowed(parts.hostname, allowed_hosts)
                   or _url_allowed(parts, allowed_urls or []))
    if not allowed:
        raise ValueError(
            f"Host or URL is not authorized for this run: {parts.hostname}"
        )
    return parts


def _validate_url(url: str, allowed_hosts: list[str], *,
                  policy: str = "allowlist",
                  allowed_urls: list[str] | None = None) -> str:
    parts = _validate_url_parts(
        url, allowed_hosts, policy=policy, allowed_urls=allowed_urls
    )
    _public_ips(parts.hostname, parts.scheme)
    return url


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a prevalidated IP while preserving SNI."""

    def __init__(self, host: str, ip: str, port: int, timeout: int, context: ssl.SSLContext):
        super().__init__(ip, port=port, timeout=timeout, context=context)
        self._sni_host = host

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._sni_host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, ip: str, port: int, timeout: int):
        super().__init__(ip, port=port, timeout=timeout)


def _connection(parts, ip: str, timeout_s: int):
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        return _PinnedHTTPSConnection(
            _normalize_host(parts.hostname), ip, port, timeout_s,
            ssl.create_default_context()
        )
    return _PinnedHTTPConnection(ip, port, timeout_s)


def _host_header(parts) -> str:
    host = _normalize_host(parts.hostname)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port:
        host += f":{parts.port}"
    return host


def _read_url_sync(url: str, *, allowed_hosts: list[str], allowed_urls: list[str],
                   policy: str, timeout_s: int, max_bytes: int,
                   max_redirects: int) -> dict:
    current = url
    for _ in range(max_redirects + 1):
        try:
            parts = _validate_url_parts(
                current, allowed_hosts, policy=policy, allowed_urls=allowed_urls
            )
            ip = _public_ips(parts.hostname, parts.scheme)[0]
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "url": mask_secrets(current),
            }
        connection = _connection(parts, ip, timeout_s)
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            connection.request(
                "GET", path,
                headers={
                    "Host": _host_header(parts),
                    "User-Agent": "BoteX-read_url/1",
                },
            )
            response = connection.getresponse()
            if response.status in _REDIRECT_CODES:
                location = response.getheader("Location")
                if not location:
                    return {"ok": False, "error": "Redirect without Location."}
                current = urljoin(current, location)
                continue
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].lower()
            if content_type not in _TEXT_CONTENT_TYPES:
                return {"ok": False, "error": f"Unsupported content type: {content_type or 'unknown'}"}
            data = response.read(max_bytes + 1)
            text = data[:max_bytes].decode("utf-8", errors="replace")
            return {
                "ok": True,
                "url": mask_secrets(current),
                "status_code": response.status,
                "content_type": content_type,
                "truncated": len(data) > max_bytes,
                "content": mask_secrets(text),
            }
        except (http.client.HTTPException, ssl.SSLError, UnicodeError,
                TimeoutError, OSError) as exc:
            return {
                "ok": False,
                "error": f"Request failed: {exc}",
                "url": mask_secrets(current),
            }
        finally:
            connection.close()
    return {"ok": False, "error": f"Too many redirects (limit {max_redirects})."}


async def read_url(url: str, *, allowed_hosts: list[str] | None = None,
                   allowed_urls: list[str] | None = None,
                   policy: str = "allowlist", timeout_s: int = 20,
                   max_bytes: int = 200000, max_redirects: int = 3) -> dict:
    """Fetch bounded, public, text-only content without forwarding secrets."""
    if timeout_s <= 0 or max_bytes <= 0 or max_redirects < 0:
        return {"ok": False, "error": "Invalid network limits."}
    return await asyncio.to_thread(
        _read_url_sync, url,
        allowed_hosts=allowed_hosts or [],
        allowed_urls=allowed_urls or [],
        policy=policy,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
        max_redirects=max_redirects)


async def fetch_url(url: str, *, net_cfg: dict, max_bytes: int | None = None) -> dict:
    """Caller-side public fetch. The caller's URL is itself the authorization."""
    policy = str(net_cfg.get("policy", "caller")).strip().lower()
    if not net_cfg.get("enabled") or policy == "off":
        return {"ok": False, "error": "Network access is disabled."}
    if policy == "allowlist":
        try:
            scope = resolve_net_scope(net_cfg)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        effective_policy = "allowlist"
        allowed_hosts = scope["allowed_hosts"]
        allowed_urls = scope["allowed_urls"]
    elif policy in ("caller", "public"):
        effective_policy = "public"
        allowed_hosts = []
        allowed_urls = []
    else:
        return {"ok": False, "error": f"Unknown net.policy '{policy}'."}

    configured_max = int(net_cfg.get("max_bytes", 200000))
    requested = int(max_bytes or configured_max)
    if requested <= 0 or configured_max <= 0:
        return {"ok": False, "error": "Invalid network limits."}
    return await read_url(
        url,
        policy=effective_policy,
        allowed_hosts=allowed_hosts,
        allowed_urls=allowed_urls,
        timeout_s=int(net_cfg.get("timeout_s", 20)),
        max_bytes=min(requested, configured_max),
        max_redirects=int(net_cfg.get("max_redirects", 3)),
    )
