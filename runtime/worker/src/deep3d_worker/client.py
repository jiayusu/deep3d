"""Bounded internal runtime transport. Credentials never enter task files or logs."""

import http.client
import ipaddress
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE = 4 * 1024 * 1024
MAX_REQUEST = 64 * 1024
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class WorkerError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate member")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Invalid JSON number")


def decode(data: bytes) -> dict:
    try:
        result = json.loads(
            data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
        if not isinstance(result, dict):
            raise ValueError
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 32:
                raise ValueError
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
            elif isinstance(value, str):
                value.encode("utf-8")
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise WorkerError("RUNTIME_PROTOCOL") from None


class RuntimeClient:
    def __init__(self, api: str, token: str | None = None, timeout: float = 5):
        try:
            parsed = urllib.parse.urlsplit(api)
            if (
                not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path.rstrip("/")
                or any(ord(ch) <= 32 or ord(ch) == 127 for ch in api)
            ):
                raise ValueError
            loopback = parsed.hostname.lower() == "localhost"
            if not loopback:
                try:
                    loopback = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    pass
            if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
                raise ValueError
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise WorkerError("WORKER_CONFIG") from None
        credential = token if token is not None else os.environ.get("DEEP3D_WORKER_TOKEN")
        if (
            not isinstance(credential, str)
            or not credential
            or any(ord(ch) <= 32 or ord(ch) > 126 for ch in credential)
        ):
            raise WorkerError("WORKER_AUTH")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise WorkerError("WORKER_CONFIG")
        self.api = api.rstrip("/")
        self._token = credential
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def post(self, action: str, payload: dict) -> object:
        if action not in {"claim", "heartbeat", "complete", "fail"}:
            raise WorkerError("WORKER_CAPABILITY")
        try:
            data = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
            if len(data) > MAX_REQUEST:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise WorkerError("RUNTIME_REQUEST") from None
        request = urllib.request.Request(
            self.api + "/v1/worker/" + action,
            data=data,
            method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE + 1)
                if response.status != 200 or len(body) > MAX_RESPONSE:
                    raise WorkerError("RUNTIME_PROTOCOL")
                result = decode(body)
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                body = error.read(min(MAX_RESPONSE, 4096) + 1)
            except OSError:
                body = b""
            error.close()
            if status in {401, 403}:
                raise WorkerError("WORKER_AUTH") from None
            if 300 <= status < 400:
                raise WorkerError("RUNTIME_REDIRECT") from None
            code = (
                "RUNTIME_UNAVAILABLE"
                if status >= 500 or status in {408, 429}
                else "RUNTIME_REJECTED"
            )
            try:
                candidate = decode(body).get("error", {}).get("code")
                if (
                    isinstance(candidate, str)
                    and _CODE.fullmatch(candidate)
                    and self._token not in candidate
                ):
                    code = candidate
            except (WorkerError, AttributeError):
                pass
            raise WorkerError(code, retryable=status >= 500 or status in {408, 429}) from None
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
            raise WorkerError("RUNTIME_UNAVAILABLE", retryable=True) from None
        if result.get("status") != "pass" or "result" not in result:
            raise WorkerError("RUNTIME_PROTOCOL")
        return result["result"]
