"""Bounded, role-specific HTTP access with no redirects or credential-bearing errors."""

import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID

MAX_NATIVE_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ClientError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _base_url(value: str) -> str:
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or "\\" in value
        ):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError
        return value.rstrip("/")
    except (TypeError, ValueError, UnicodeError):
        raise ClientError(
            "CLIENT_CONFIG", "Use HTTPS or loopback HTTP for the runtime API"
        ) from None


def _tree(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise ValueError
        if isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError
        elif isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (int, float, bool)):
            raise ValueError


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError


def _decode(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        _tree(value)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ClientError("CLIENT_PROTOCOL", "Runtime returned invalid JSON") from None


class RuntimeClient:
    def __init__(
        self, api: str = "http://127.0.0.1:8088", token: str | None = None, timeout: float = 30
    ):
        self._base = _base_url(api)
        self._token = token if token is not None else os.environ.get("DEEP3D_OPERATOR_TOKEN", "")
        if (
            not isinstance(self._token, str)
            or len(self._token) < 24
            or any(not 33 <= ord(c) <= 126 for c in self._token)
        ):
            raise ClientError("CLIENT_AUTH", "Set a valid DEEP3D_OPERATOR_TOKEN")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
        ):
            raise ClientError("CLIENT_CONFIG", "HTTP timeout must be within 0 to 120 seconds")
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _remote_error(self, data: bytes, default: str) -> ClientError:
        code = default
        try:
            value = _decode(data)
            candidate = value.get("error", {}).get("code")
            if (
                isinstance(candidate, str)
                and _CODE.fullmatch(candidate)
                and self._token not in candidate
            ):
                code = candidate
        except (ClientError, AttributeError):
            pass
        # Server messages can contain paths, SQL details or reflected credentials.
        return ClientError(code, "Runtime rejected the request; inspect operator service logs")

    def request(self, method: str, path: str, payload: dict | bytes | None = None):
        if (
            method not in {"GET", "POST"}
            or not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or "#" in path
            or any(ord(c) <= 32 or ord(c) == 127 for c in path)
        ):
            raise ClientError("CLIENT_REQUEST", "Invalid runtime request path or method")
        content_type = "application/json"
        if isinstance(payload, bytes):
            if not payload or len(payload) > MAX_NATIVE_BYTES:
                raise ClientError("CLIENT_INPUT_TOO_LARGE", "Native package must be at most 16 MiB")
            encoded = payload
            content_type = "application/octet-stream"
        elif isinstance(payload, dict):
            try:
                _tree(payload)
                encoded = json.dumps(
                    payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise ClientError(
                    "CLIENT_REQUEST", "Request must be finite, bounded JSON"
                ) from None
            if len(encoded) > MAX_REQUEST_BYTES:
                raise ClientError("CLIENT_INPUT_TOO_LARGE", "Run request exceeds 256 KiB")
        elif payload is None:
            encoded = None
        else:
            raise ClientError("CLIENT_REQUEST", "Unsupported runtime request payload")
        request = urllib.request.Request(
            self._base + path,
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": content_type,
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise ClientError("CLIENT_PROTOCOL", "Runtime returned unexpected HTTP status")
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ClientError("CLIENT_PROTOCOL", "Runtime response exceeds 8 MiB")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                data = exc.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException):
                data = b""
            finally:
                exc.close()
            if 300 <= status < 400:
                raise ClientError("CLIENT_REDIRECT", "Runtime redirects are disabled") from None
            if len(data) > MAX_RESPONSE_BYTES:
                data = b""
            default = "UNAUTHORIZED" if status in {401, 403} else "CLIENT_HTTP"
            raise self._remote_error(data, default) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise ClientError("CLIENT_UNAVAILABLE", "Cannot contact the runtime service") from None
        value = _decode(data)
        if value.get("status") == "error":
            raise self._remote_error(data, "CLIENT_HTTP")
        if value.get("status") != "pass" or "result" not in value:
            raise ClientError("CLIENT_PROTOCOL", "Runtime returned no result envelope")
        return value["result"]

    def download(self, path: str, *, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, dict]:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or "#" in path
            or any(ord(c) <= 32 or ord(c) == 127 for c in path)
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_DOWNLOAD_BYTES
        ):
            raise ClientError("CLIENT_REQUEST", "Invalid artifact request")
        request = urllib.request.Request(
            self._base + path,
            method="GET",
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                digest = response.headers.get("x-content-sha256", "")
                revision = response.headers.get("x-spec-revision", "")
                try:
                    identifier = UUID(revision)
                    length = response.headers.get("content-length")
                    too_large = length is not None and (
                        not length.isdigit() or int(length) > max_bytes
                    )
                    if (
                        response.status != 200
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                        or str(identifier) != revision
                        or identifier.int == 0
                        or too_large
                    ):
                        raise ValueError
                except (ValueError, TypeError):
                    raise ClientError(
                        "CLIENT_PROTOCOL", "Invalid artifact identity or size"
                    ) from None
                data = response.read(max_bytes + 1)
                if not data or len(data) > max_bytes:
                    raise ClientError("CLIENT_PROTOCOL", "Artifact exceeds the download limit")
                if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                    raise ClientError("CLIENT_ARTIFACT_CORRUPT", "Downloaded artifact hash differs")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                data = exc.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, http.client.HTTPException):
                data = b""
            finally:
                exc.close()
            if 300 <= status < 400:
                raise ClientError("CLIENT_REDIRECT", "Runtime redirects are disabled") from None
            if len(data) > MAX_RESPONSE_BYTES:
                data = b""
            default = "UNAUTHORIZED" if status in {401, 403} else "CLIENT_HTTP"
            raise self._remote_error(data, default) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise ClientError(
                "CLIENT_UNAVAILABLE", "Cannot download the runtime artifact"
            ) from None
        return data, {"sha256": digest, "revision_id": revision, "size_bytes": len(data)}
