"""Offline transport tests: these do not claim to exercise a live language model."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from deep3d_agent.provider import AnthropicProvider, ProviderError

TOKEN = "test-credential-do-not-log"
MODEL = "fixture-model"
TEXT_RESPONSE = {
    "role": "assistant",
    "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "done"}],
}
TOOL_RESPONSE = {
    "role": "assistant",
    "stop_reason": "tool_use",
    "content": [
        {"type": "text", "text": "Inspecting the native project."},
        {"type": "tool_use", "id": "tool-1", "name": "inspect_project", "input": {}},
    ],
}
TOOLS = [{"name": "inspect_project", "input_schema": {"type": "object", "properties": {}}}]


@pytest.fixture
def server():
    state = {"status": 200, "body": json.dumps(TOOL_RESPONSE).encode(), "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            state["requests"].append(
                {
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            if state["status"] == 302:
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leaked")
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: instance.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    state["url"] = f"http://127.0.0.1:{instance.server_port}"
    yield state
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


def complete(provider):
    return provider.complete(
        "Use only defined CAD tools.", [{"role": "user", "content": "edit"}], TOOLS
    )


@pytest.mark.parametrize(
    "base_path,expected_path",
    [
        ("", "/v1/messages"),
        ("/", "/v1/messages"),
        ("/v1", "/v1/messages"),
        ("/proxy/v1/", "/proxy/v1/messages"),
        ("/v1/messages", "/v1/messages"),
    ],
)
def test_request_and_tool_response(server, base_path, expected_path):
    provider = AnthropicProvider(MODEL, server["url"] + base_path, TOKEN)
    assert complete(provider) == {"content": TOOL_RESPONSE["content"], "stop_reason": "tool_use"}
    request = server["requests"][0]
    assert request["path"] == expected_path
    assert request["headers"]["x-api-key"] == TOKEN
    assert request["headers"]["authorization"] == "Bearer " + TOKEN
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    assert json.loads(request["body"]) == {
        "model": MODEL,
        "max_tokens": 1600,
        "system": "Use only defined CAD tools.",
        "messages": [{"role": "user", "content": "edit"}],
        "tools": TOOLS,
        "tool_choice": {"type": "auto"},
    }


def test_environment_and_text_response(monkeypatch, server):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", server["url"])
    server["body"] = json.dumps(TEXT_RESPONSE).encode()
    assert complete(AnthropicProvider(MODEL))["content"] == TEXT_RESPONSE["content"]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "PROVIDER_AUTH"),
        (403, "PROVIDER_AUTH"),
        (429, "PROVIDER_RATE_LIMIT"),
        (503, "PROVIDER_UNAVAILABLE"),
        (408, "PROVIDER_UNAVAILABLE"),
        (400, "PROVIDER_REQUEST"),
        (302, "PROVIDER_PROTOCOL"),
    ],
)
def test_error_bodies_never_leak_and_redirect_is_not_followed(server, status, code):
    server["status"] = status
    server["body"] = f"private error: {TOKEN} {server['url']}".encode()
    with pytest.raises(ProviderError) as failure:
        complete(AnthropicProvider(MODEL, server["url"], TOKEN))
    assert failure.value.code == code
    assert TOKEN not in str(failure.value)
    assert server["url"] not in str(failure.value)
    assert len(server["requests"]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com?token=secret",
        "https://example.com/#secret",
        "https://example.com:99999",
        "https://example.com\n/",
        "file:///secret",
        "//example.com",
        "https://",
        "https://example.com:bad",
    ],
)
def test_invalid_endpoints_are_redacted(url):
    with pytest.raises(ProviderError) as failure:
        AnthropicProvider(MODEL, url, TOKEN)
    assert failure.value.code == "PROVIDER_CONFIG"
    assert url not in str(failure.value)
    assert TOKEN not in str(failure.value)


@pytest.mark.parametrize("timeout", [0, -1, 121, float("nan"), True, "60"])
def test_invalid_timeout(timeout):
    with pytest.raises(ProviderError, match="timeout"):
        AnthropicProvider(MODEL, "https://example.com", TOKEN, timeout)


@pytest.mark.parametrize("token", ["", "secret\nvalue", "secret value", "secreté"])
def test_invalid_credentials_are_redacted(token):
    with pytest.raises(ProviderError) as failure:
        AnthropicProvider(MODEL, "https://example.com", token)
    assert failure.value.code == "PROVIDER_AUTH"
    assert str(failure.value) == "A valid provider credential is required"


def test_oversized_response(server):
    server["body"] = b" " * (2 * 1024 * 1024 + 1)
    with pytest.raises(ProviderError) as failure:
        complete(AnthropicProvider(MODEL, server["url"], TOKEN))
    assert failure.value.code == "PROVIDER_PROTOCOL"


@pytest.mark.parametrize(
    "response",
    [
        {},
        [],
        {**TEXT_RESPONSE, "content": []},
        {**TEXT_RESPONSE, "content": [{"type": "text", "text": " "}]},
        {**TEXT_RESPONSE, "stop_reason": "max_tokens"},
        {**TEXT_RESPONSE, "stop_reason": "tool_use"},
        {**TOOL_RESPONSE, "stop_reason": "end_turn"},
        {**TOOL_RESPONSE, "content": [TOOL_RESPONSE["content"][1]] * 2},
        {**TEXT_RESPONSE, "role": "user"},
        {**TEXT_RESPONSE, "content": [{"type": "thinking", "thinking": "private"}]},
    ],
)
def test_malformed_or_truncated_response_is_rejected(server, response):
    server["body"] = json.dumps(response).encode()
    with pytest.raises(ProviderError) as failure:
        complete(AnthropicProvider(MODEL, server["url"], TOKEN))
    assert failure.value.code == "PROVIDER_PROTOCOL"


@pytest.mark.parametrize(
    "change",
    [{"input": []}, {"id": ""}, {"id": "a b"}, {"name": "arbitrary.code()"}, {"name": 123}],
)
def test_malformed_tool_fields_are_rejected(server, change):
    response = copy.deepcopy(TOOL_RESPONSE)
    response["content"][1].update(change)
    server["body"] = json.dumps(response).encode()
    with pytest.raises(ProviderError) as failure:
        complete(AnthropicProvider(MODEL, server["url"], TOKEN))
    assert failure.value.code == "PROVIDER_PROTOCOL"


@pytest.mark.parametrize(
    "body",
    [
        b"{} garbage",
        b'{"content":[],"content":[]}',
        b'{"content":[],"value":NaN}',
        b'{"content":[],"value":1e999}',
        b"\xff",
    ],
)
def test_non_json_duplicate_or_nonfinite_response(server, body):
    server["body"] = body
    with pytest.raises(ProviderError) as failure:
        complete(AnthropicProvider(MODEL, server["url"], TOKEN))
    assert failure.value.code == "PROVIDER_PROTOCOL"


def test_network_errors_are_redacted():
    provider = AnthropicProvider(MODEL, "http://127.0.0.1:1/private", TOKEN, 0.1)
    with pytest.raises(ProviderError) as failure:
        complete(provider)
    assert failure.value.code == "PROVIDER_UNAVAILABLE"
    assert "private" not in str(failure.value)
    assert TOKEN not in str(failure.value)


def test_invalid_or_oversized_outbound_request_is_rejected_before_network():
    provider = AnthropicProvider(MODEL, "http://127.0.0.1:1", TOKEN)
    for value in (float("nan"), "x" * (2 * 1024 * 1024)):
        with pytest.raises(ProviderError) as failure:
            provider.complete("system", [{"role": "user", "content": value}], TOOLS)
        assert failure.value.code == "PROVIDER_REQUEST"
