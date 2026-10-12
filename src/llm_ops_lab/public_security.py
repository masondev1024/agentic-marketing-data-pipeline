"""Public ingress controls for the single-operator investigation API.

The existing ``RequestBoundary`` should wrap this boundary so request IDs and
body/resource limits remain outermost. This boundary validates transport and
identity before forwarding an ASGI request, without reading or buffering bodies.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import stat
import threading
import time
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

_ALLOWED_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)(?:\."
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*\Z"
)
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_TRUSTED_LOOPBACK_NETWORKS = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))
_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"strict-transport-security", b"max-age=86400"),
)
_AUTH_FAILURE = (401, "authentication_required")


@dataclass(frozen=True, slots=True, repr=False)
class PublicConfig:
    """Validated public-boundary configuration; the bearer secret is never rendered."""

    allowed_host: str
    token: InitVar[bytes | str]
    requests_per_minute: int = 30
    token_bucket_capacity: int = 5
    anonymous_live_health: bool = True
    _token_digest: bytes = field(init=False, repr=False)

    def __post_init__(self, token: bytes | str) -> None:
        host = self.allowed_host
        if not isinstance(host, str) or not _ALLOWED_HOST_RE.fullmatch(host):
            raise ValueError("allowed_host must be a DNS hostname without a scheme or port")
        object.__setattr__(self, "allowed_host", host.lower())

        token = token.encode("utf-8") if isinstance(token, str) else token
        if not isinstance(token, bytes) or len(token) < 32:
            raise ValueError("bearer token must contain at least 32 bytes")
        if b"\r" in token or b"\n" in token:
            raise ValueError("bearer token must be a single line")
        object.__setattr__(self, "_token_digest", hashlib.sha256(token).digest())

        if type(self.requests_per_minute) is not int or not 1 <= self.requests_per_minute <= 10_000:
            raise ValueError("requests_per_minute must be between 1 and 10000")
        if type(self.token_bucket_capacity) is not int or not (
            1 <= self.token_bucket_capacity <= 10_000
        ):
            raise ValueError("token_bucket_capacity must be between 1 and 10000")
        if not isinstance(self.anonymous_live_health, bool):
            raise ValueError("anonymous_live_health must be a boolean")

    def __repr__(self) -> str:
        return (
            "PublicConfig(allowed_host={!r}, requests_per_minute={!r}, "
            "token_bucket_capacity={!r}, anonymous_live_health={!r}, token=<redacted>)"
        ).format(
            self.allowed_host,
            self.requests_per_minute,
            self.token_bucket_capacity,
            self.anonymous_live_health,
        )

    def verify_token(self, presented: bytes) -> bool:
        """Compare a presented token without retaining or logging its plaintext value."""
        if not isinstance(presented, bytes):
            return False
        return hmac.compare_digest(hashlib.sha256(presented).digest(), self._token_digest)

    @classmethod
    def from_sources(
        cls,
        *,
        allowed_host: str,
        token_env: str | None = "OLIST_API_TOKEN",
        token_file: str | os.PathLike[str] | None = None,
        requests_per_minute: int = 30,
        token_bucket_capacity: int = 5,
        anonymous_live_health: bool = True,
    ) -> "PublicConfig":
        """Load a token from exactly one source, enforcing owner-only file permissions."""
        if token_file is not None:
            path = Path(token_file)
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                raise ValueError("configured bearer-token file is unavailable") from None
            try:
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError("bearer-token path must be a regular file")
                if file_stat.st_uid != os.geteuid() or file_stat.st_mode & 0o077:
                    raise ValueError(
                        "bearer-token file must be owned by this user and mode 0600 or stricter"
                    )
                with os.fdopen(descriptor, "rb") as token_file_handle:
                    descriptor = -1
                    token = token_file_handle.read(4097)
                if len(token) > 4096:
                    raise ValueError("bearer-token file exceeds the size limit")
            except OSError:
                raise ValueError("configured bearer-token file is unavailable") from None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            if token.endswith(b"\r\n"):
                token = token[:-2]
            elif token.endswith(b"\n"):
                token = token[:-1]
        elif token_env is not None:
            value = os.environ.get(token_env)
            if value is None:
                raise ValueError("configured bearer-token environment variable is missing")
            token = value.encode("utf-8")
        else:
            raise ValueError("configure token_file or token_env")

        return cls(
            allowed_host=allowed_host,
            token=token,
            requests_per_minute=requests_per_minute,
            token_bucket_capacity=token_bucket_capacity,
            anonymous_live_health=anonymous_live_health,
        )


class PublicBoundary:
    """Authenticate and constrain the HTTP surface before forwarding to the app."""

    def __init__(self, app: Any, config: PublicConfig):
        self.app = app
        self.config = config
        self._rate_lock = threading.Lock()
        self._tokens = float(config.token_bucket_capacity)
        self._last_refill = time.monotonic()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        request_id = state.setdefault("request_id", str(uuid4()))
        headers = scope.get("headers", [])
        grouped: dict[bytes, list[bytes]] = {}
        for name, value in headers:
            grouped.setdefault(name.lower(), []).append(value)

        host_values = grouped.get(b"host", [])
        if len(host_values) != 1 or not self._host_matches(host_values[0]):
            await self._reject(scope, receive, send, request_id, 400, "invalid_host")
            return

        origin_values = grouped.get(b"origin", [])
        if origin_values:
            await self._reject(scope, receive, send, request_id, 403, "browser_origin_not_allowed")
            return

        if len(grouped.get(b"x-forwarded-proto", [])) > 1:
            await self._reject(scope, receive, send, request_id, 400, "invalid_forwarded_proto")
            return

        if not self._is_trusted_loopback_peer(scope):
            await self._reject(scope, receive, send, request_id, 403, "trusted_proxy_required")
            return
        forwarded_proto = grouped.get(b"x-forwarded-proto", [])
        if len(forwarded_proto) != 1 or forwarded_proto[0].strip().lower() != b"https":
            await self._reject(scope, receive, send, request_id, 400, "https_required")
            return

        path = scope.get("path", "")
        method = scope.get("method", "").upper()
        allowed_route = (path, method) in {
            ("/v1/investigations", "POST"),
            ("/health/live", "GET"),
            ("/health/ready", "GET"),
            ("/metrics", "GET"),
        }
        if not allowed_route:
            await self._reject(scope, receive, send, request_id, 404, "not_found")
            return

        anonymous_live = (
            self.config.anonymous_live_health and path == "/health/live" and method == "GET"
        )
        auth_values = grouped.get(b"authorization", [])
        if len(auth_values) > 1:
            await self._reject(scope, receive, send, request_id, 401, _AUTH_FAILURE[1])
            return
        if anonymous_live and not auth_values:
            state["principal"] = "anonymous-health"
        else:
            if len(auth_values) != 1 or not self._authorization_valid(auth_values[0]):
                await self._reject(scope, receive, send, request_id, 401, _AUTH_FAILURE[1])
                return
            state["principal"] = "operator"
            state["principal_id"] = "operator"

        if path == "/v1/investigations":
            idem_values = grouped.get(b"idempotency-key", [])
            try:
                idempotency_key = idem_values[0].decode("ascii") if len(idem_values) == 1 else ""
            except UnicodeDecodeError:
                idempotency_key = ""
            if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
                await self._reject(scope, receive, send, request_id, 400, "invalid_idempotency_key")
                return
            state["idempotency_key"] = idempotency_key
            if not self._take_rate_token():
                await self._reject(scope, receive, send, request_id, 429, "rate_limit_exceeded")
                return

        await self.app(scope, receive, self._secure_send(send))

    def _host_matches(self, raw_host: bytes) -> bool:
        try:
            host = raw_host.decode("ascii")
        except UnicodeDecodeError:
            return False
        return host.lower() == self.config.allowed_host

    @staticmethod
    def _is_trusted_loopback_peer(scope) -> bool:
        peer = scope.get("client")
        if not peer:
            return False
        try:
            address = ipaddress.ip_address(peer[0])
        except (TypeError, ValueError):
            return False
        return any(address in network for network in _TRUSTED_LOOPBACK_NETWORKS)

    def _authorization_valid(self, header: bytes) -> bool:
        scheme, separator, credentials = header.partition(b" ")
        if not separator or scheme.lower() != b"bearer" or not credentials:
            return False
        if b" " in credentials or b"\t" in credentials:
            return False
        return self.config.verify_token(credentials)

    def _take_rate_token(self) -> bool:
        now = time.monotonic()
        refill_per_second = self.config.requests_per_minute / 60.0
        with self._rate_lock:
            elapsed = max(0.0, now - self._last_refill)
            self._tokens = min(
                float(self.config.token_bucket_capacity),
                self._tokens + elapsed * refill_per_second,
            )
            self._last_refill = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True

    def _secure_send(self, send):
        async def wrapped(message):
            if message["type"] == "http.response.start":
                enforced = {name for name, _ in _SECURITY_HEADERS}
                message["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in enforced
                ] + list(_SECURITY_HEADERS)
            await send(message)

        return wrapped

    async def _reject(self, scope, receive, send, request_id, status: int, code: str) -> None:
        scope.setdefault("state", {})["outcome"] = {"error_code": code}
        payload = json.dumps(
            {"request_id": request_id, "error": {"code": code}},
            separators=(",", ":"),
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
            (b"x-request-id", str(request_id).encode("ascii", errors="ignore")),
            *_SECURITY_HEADERS,
        ]
        if status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        if status == 429:
            headers.append((b"retry-after", b"1"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload, "more_body": False})
