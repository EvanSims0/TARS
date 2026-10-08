"""The real SDK backend, against a mocked HTTP stream (no network, no spend)."""

from __future__ import annotations

import json

import httpx2

from tars.agent import AnthropicBackend


def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM = [
    {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
     "model": "claude-sonnet-5-5", "content": [], "stop_reason": None, "stop_sequence": None,
     "usage": {"input_tokens": 12, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Checking. "}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu_1",
     "name": "list_events", "input": {}}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta",
     "partial_json": "{\"day\": \"today\"}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
     "usage": {"output_tokens": 20}},
    {"type": "message_stop"},
]


async def test_sdk_request_shape_and_stream_parse(make_agent):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(200, content=sse(STREAM), headers={"content-type": "text/event-stream"})

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    backend = AnthropicBackend(api_key="sk-test", http_client=http)
    agent = make_agent(backend)
    agent._append("user", [{"type": "text", "text": "plan my week"}])
    texts = []

    async def on_text(t):
        texts.append(t)

    msg = await backend.stream(on_text=on_text, **agent._request("claude-sonnet-5-5", deep=True))
    assert texts == ["Checking. "]
    assert msg.stop_reason == "tool_use"
    assert [b.type for b in msg.content] == ["text", "tool_use"]
    assert msg.content[1].input == {"day": "today"}

    sent = requests[0]
    assert sent.url.path == "/v1/messages"
    body = json.loads(sent.content)
    assert "server-side-fallback-2026-07-01" in sent.headers["anthropic-beta"]
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["stream"] is True
    assert all(t["eager_input_streaming"] for t in body["tools"])
