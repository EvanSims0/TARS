"""One quiet retry for network blips, and never a second send."""

from __future__ import annotations

import httpx
import pytest
import respx

from tars.actions import ToolError
from tars.integrations import gmail, todoist
from tars.integrations.google_auth import TOKEN_URI, GoogleSession
from tars.net import client as net_client

URL = "https://api.example.com/thing"


def client() -> httpx.AsyncClient:
    return net_client(pause=0)


@respx.mock
async def test_a_read_is_retried_once_after_a_blip():
    route = respx.get(URL).mock(side_effect=[httpx.ReadError("reset"), httpx.Response(200, json={"ok": 1})])
    async with client() as http:
        assert (await http.get(URL)).json() == {"ok": 1}
    assert route.call_count == 2


@respx.mock
async def test_a_busy_service_gets_one_more_try_then_the_error_stands():
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(503)])
    async with client() as http:
        assert (await http.get(URL)).status_code == 503
    assert route.call_count == 2


@respx.mock
async def test_a_send_is_never_repeated_once_it_may_have_arrived():
    route = respx.post(URL).mock(side_effect=[httpx.ReadError("reset"), httpx.Response(200)])
    async with client() as http:
        with pytest.raises(httpx.ReadError):
            await http.post(URL, json={"to": "sam@x.com"})
        route.reset()
        route.mock(side_effect=[httpx.Response(503), httpx.Response(200)])
        assert (await http.post(URL, json={})).status_code == 503
    assert route.call_count == 1


@respx.mock
async def test_a_send_that_never_left_the_pc_is_tried_again():
    route = respx.post(URL).mock(side_effect=[httpx.ConnectError("refused"), httpx.Response(200)])
    async with client() as http:
        assert (await http.post(URL, json={})).status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_slow_answers_are_not_retried():
    route = respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    async with client() as http:
        with pytest.raises(httpx.ReadTimeout):
            await http.get(URL)
    assert route.call_count == 1


@respx.mock
async def test_integrations_turn_a_blip_into_a_normal_answer():
    respx.get(f"{todoist.BASE_URL}/projects").mock(side_effect=[
        httpx.ConnectError("blip"), httpx.Response(200, json={"results": [], "next_cursor": None})])
    assert await todoist.TodoistClient("t", client())._paged("/projects") == []


def _session(http: httpx.AsyncClient) -> GoogleSession:
    s = GoogleSession({"refresh_token": "r", "client_id": "c", "client_secret": "s"}, http)
    s._access, s._expires = "old", 9e12  # looks valid locally, but Google says it has expired
    return s


@respx.mock
async def test_an_expired_google_token_is_renewed_and_the_request_repeated():
    refresh = respx.post(TOKEN_URI).respond(json={"access_token": "new", "expires_in": 3600})
    seen = []

    def inbox(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, json={"messages": []})

    respx.get(f"{gmail.API}/messages").mock(side_effect=inbox)
    result = await _session(client()).request("GET", f"{gmail.API}/messages")
    assert result == {"messages": []}
    assert seen == ["Bearer old", "Bearer new"] and refresh.call_count == 1


@respx.mock
async def test_a_revoked_google_sign_in_says_how_to_fix_it():
    respx.post(TOKEN_URI).respond(json={"access_token": "new", "expires_in": 3600})
    route = respx.post(f"{gmail.API}/messages/send").respond(401)
    with pytest.raises(ToolError, match="reconnect Google in Settings"):
        await _session(client()).request("POST", f"{gmail.API}/messages/send", json={"raw": "x"})
    assert route.call_count == 2  # once with the old token, once with a fresh one; 401 means nothing was sent
