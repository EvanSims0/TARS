"""The memory map server and the data behind it."""

from __future__ import annotations

import http.client
import json

import pytest

from tars.memory import Vault
from tars.memory_map import MemoryMap, fact_id, memory_data, mentions


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "vault")
    v.remember("My sister Jess's birthday is March 3", "People")
    v.remember("Jess is allergic to peanuts", "Health")
    v.remember("Prefers oat milk in coffee", "Preferences")
    v.remember("Prefers window seats", "Preferences")
    return v


def test_facts_are_grouped_and_names_link_across_categories(vault):
    data = memory_data(vault)
    assert data["count"] == 4
    by_cat = {c["name"]: [f["text"] for f in c["facts"]] for c in data["categories"]}
    assert by_cat["Preferences"] == ["Prefers oat milk in coffee", "Prefers window seats"]
    # "Jess" appears mid-sentence, so it links two facts; "Prefers" only ever starts one.
    assert list(data["mentions"]) == ["Jess"] and len(data["mentions"]["Jess"]) == 2
    assert data["obsidian"].startswith("obsidian://open?path=")


def test_mentions_ignore_days_months_and_single_facts(vault):
    facts = vault.facts()
    assert "March" not in mentions(facts)


def _request(url: str, method: str = "GET", path: str | None = None, headers=None, body=None):
    from urllib.parse import urlparse

    u = urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    conn.request(method, path or f"{u.path}?{u.query}", body=body, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, resp.read()


def test_server_needs_the_token_and_a_loopback_host(vault):
    opened = []
    memory_map = MemoryMap(vault, open_browser=opened.append)
    url = memory_map.open()
    try:
        assert opened == [url] and url.startswith("http://127.0.0.1:")
        status, page = _request(url)
        assert status == 200 and memory_map.token.encode() in page
        assert _request(url, path="/?t=wrong")[0] == 403
        assert _request(url, path="/api/memory")[0] == 403
        # A page on another site that resolves its own name to 127.0.0.1 still gets nothing.
        token = {"X-Tars-Token": memory_map.token}
        assert _request(url, path="/api/memory", headers={**token, "Host": "evil.example:80"})[0] == 403
        status, body = _request(url, path="/api/memory", headers=token)
        assert status == 200 and json.loads(body)["count"] == 4
    finally:
        memory_map.stop()


def test_forget_from_the_map_removes_exactly_one_fact(vault):
    memory_map = MemoryMap(vault, open_browser=lambda url: None)
    url = memory_map.start()
    try:
        target = next(f for f in vault.facts() if f.text.startswith("Prefers window"))
        headers = {"X-Tars-Token": memory_map.token, "Content-Type": "application/json"}
        status, body = _request(url, "POST", "/api/forget", headers, json.dumps({"id": fact_id(target)}))
        assert status == 200 and json.loads(body) == {"forgot": "Prefers window seats"}
        assert {f.text for f in vault.facts()} == {
            "My sister Jess's birthday is March 3", "Jess is allergic to peanuts", "Prefers oat milk in coffee"}
        assert _request(url, "POST", "/api/forget", headers, json.dumps({"id": fact_id(target)}))[0] == 404
        assert _request(url, "POST", "/api/forget", {"Content-Type": "application/json"}, "{}")[0] == 403
    finally:
        memory_map.stop()


async def test_voice_tool_opens_the_map(tmp_path):
    from tars.local_tools import memory_map_tool

    opened = []
    tool = memory_map_tool(lambda: opened.append(True) or "url")
    assert await tool.handler({}) == "The memory map is open in the browser on the PC."
    assert opened == [True]
