"""Bounded Anthropic Messages transport for the restricted CAD tool loop.

Credentials are used only in request headers. Error responses and transport
exception details are deliberately excluded from public errors and run records.
"""

import http.client
import ipaddress
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _protocol(message: str = "Provider returned an invalid Messages response") -> ProviderError:
    return ProviderError("PROVIDER_PROTOCOL", message)


def _endpoint(base_url: str) -> str:
    try:
        if not isinstance(base_url, str) or any(ord(ch) <= 32 or ord(ch) == 127 for ch in base_url):
            raise ValueError
        parsed = urllib.parse.urlsplit(base_url)
        hostname = parsed.hostname
        if (
            not hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in base_url
            or "#" in base_url
        ):
            raise ValueError
        # Force port parsing here, before urllib can report a credential-bearing URL.
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        loopback = hostname.lower() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError
        path = parsed.path.rstrip("/")
        if path.endswith("/v1/messages"):
            pass
        elif path.endswith("/v1"):
            path += "/messages"
        else:
            path += "/v1/messages"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    except (TypeError, ValueError, UnicodeError):
        raise ProviderError("PROVIDER_CONFIG", "Invalid provider base URL") from None


def _json_tree(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError("JSON nesting limit")
        if isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite number")
        elif isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("JSON member must be a string")
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise ValueError("Unsupported JSON value")


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _response(data: bytes) -> dict:
    try:
        response = json.loads(
            data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
        _json_tree(response)
        if not isinstance(response, dict) or response.get("role", "assistant") != "assistant":
            raise ValueError
        reason = response.get("stop_reason")
        if reason == "max_tokens":
            raise _protocol("Model response was truncated; no tools were executed")
        if reason not in {"tool_use", "end_turn"}:
            raise ValueError
        blocks = response.get("content")
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= 128:
            raise ValueError
        normalized = []
        ids = set()
        meaningful = False
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError
            if block.get("type") == "text":
                if not isinstance(block.get("text"), str):
                    raise ValueError
                normalized.append({"type": "text", "text": block["text"]})
                meaningful |= bool(block["text"].strip())
            elif block.get("type") == "tool_use":
                identifier, name, arguments = block.get("id"), block.get("name"), block.get("input")
                if (
                    not isinstance(identifier, str)
                    or not 1 <= len(identifier) <= 256
                    or any(ord(ch) <= 32 or ord(ch) == 127 for ch in identifier)
                    or identifier in ids
                    or not isinstance(name, str)
                    or not _TOOL_NAME.fullmatch(name)
                    or not isinstance(arguments, dict)
                ):
                    raise ValueError
                ids.add(identifier)
                normalized.append(
                    {"type": "tool_use", "id": identifier, "name": name, "input": arguments}
                )
                meaningful = True
            else:
                raise ValueError
        if not meaningful or bool(ids) != (reason == "tool_use"):
            raise ValueError
        result = {"content": normalized, "stop_reason": reason}
        # Metadata remains optional: it never determines tool execution or success.
        for key in ("id", "model", "usage"):
            if key in response:
                result[key] = response[key]
        return result
    except ProviderError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _protocol() from None


class AnthropicProvider:
    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 60,
    ):
        if (
            not isinstance(model, str)
            or not 1 <= len(model) <= 256
            or any(ord(ch) <= 32 or ord(ch) == 127 for ch in model)
        ):
            raise ProviderError("PROVIDER_CONFIG", "A provider model identifier is required")
        configured_token = token
        if configured_token is None:
            configured_token = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
                "ANTHROPIC_AUTH_TOKEN"
            )
        if (
            not isinstance(configured_token, str)
            or not configured_token
            or any(ord(ch) <= 32 or ord(ch) > 126 for ch in configured_token)
        ):
            raise ProviderError("PROVIDER_AUTH", "A valid provider credential is required")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
        ):
            raise ProviderError("PROVIDER_CONFIG", "Provider timeout must be within 0–120 seconds")
        self.model = model
        self.timeout = timeout
        self._url = _endpoint(
            base_url if base_url is not None else os.environ.get(
                "ANTHROPIC_BASE_URL", "https://api.anthropic.com"
            )
        )
        self._token = configured_token
        self._opener = urllib.request.build_opener(_NoRedirect())

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        try:
            if not isinstance(system, str) or not isinstance(messages, list) or not messages:
                raise ValueError
            if not isinstance(tools, list):
                raise ValueError
            payload = {
                "model": self.model,
                "max_tokens": 1600,
                "system": system,
                "messages": messages,
                "tools": tools,
                "tool_choice": {"type": "auto"},
            }
            _json_tree(payload)
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_REQUEST_BYTES:
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ProviderError(
                "PROVIDER_REQUEST", "Invalid or oversized Messages request"
            ) from None
        request = urllib.request.Request(
            self._url,
            data=encoded,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": self._token,
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise _protocol("Provider returned an unexpected HTTP status")
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise _protocol("Provider response exceeds 2 MiB")
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status in {401, 403}:
                raise ProviderError("PROVIDER_AUTH", "Provider rejected the credential") from None
            if status == 429:
                raise ProviderError("PROVIDER_RATE_LIMIT", "Provider rate limit reached") from None
            if 300 <= status < 400:
                raise _protocol("Provider redirects are disabled") from None
            if status >= 500 or status in {408, 409}:
                raise ProviderError(
                    "PROVIDER_UNAVAILABLE", "Provider is temporarily unavailable"
                ) from None
            raise ProviderError(
                "PROVIDER_REQUEST", "Provider rejected the Messages request"
            ) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise ProviderError(
                "PROVIDER_UNAVAILABLE", "Cannot complete the provider request"
            ) from None
        return _response(data)
