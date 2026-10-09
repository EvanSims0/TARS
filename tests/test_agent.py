from __future__ import annotations

import json

from conftest import FakeBackend, reply, text, thinking, tool_use

from tars.actions import Channel


def said(on_text) -> str:
    return "".join(on_text.out)


async def test_simple_answer_streams_and_logs(make_agent, spoken, tmp_path):
    backend = FakeBackend(reply(text("It's 72 and sunny.")))
    agent = make_agent(backend)
    result = await agent.handle("What's the weather?", spoken)

    assert said(spoken) == "It's 72 and sunny."
    assert result.models == ["claude-haiku-5-5"]
    assert result.first_text_ms is not None
    req = backend.requests[0]
    assert req["thinking"] == {"type": "disabled"}
    assert req["output_config"] == {"effort": "low"}
    assert "fallbacks" not in req
    # Volatile context goes in the user turn, keeping system + tools cacheable.
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Thursday 08 October 2026" in req["messages"][0]["content"][0]["text"]
    log = (tmp_path / "data").glob("turns-*.jsonl")
    assert json.loads(next(log).read_text().splitlines()[0])["models"] == ["claude-haiku-5-5"]


async def test_tools_sorted_with_eager_streaming(make_agent, spoken):
    backend = FakeBackend(reply(text("Hi.")))
    await make_agent(backend).handle("hi", spoken)
    tools = backend.requests[0]["tools"]
    names = [t["name"] for t in tools]
    assert names[:-1] == sorted(names[:-1]) and names[-1] == "think_harder"
    assert all(t["eager_input_streaming"] for t in tools)


async def test_read_tool_runs_with_cue(make_agent, spoken, recorder):
    backend = FakeBackend(
        reply(tool_use("list_events", {"day": "today"})),
        reply(text("Dentist at three.")),
    )
    await make_agent(backend).handle("What's on today?", spoken)
    assert recorder.calls == [{"day": "today"}]
    assert said(spoken) == "Checking your calendar. Dentist at three."
    tool_result = backend.requests[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["content"] == "Dentist at 3pm"


async def test_send_is_held_until_explicit_yes(make_agent, spoken, recorder):
    backend = FakeBackend(reply(text("Drafting it."), tool_use("send_email", {"to": ["sam@x.com"], "body": "Running late"})))
    agent = make_agent(backend)
    result = await agent.handle("Tell Sam I'm running late", spoken)

    assert recorder.calls == []  # nothing sent
    assert result.awaiting_confirmation
    assert 'Email to sam@x.com saying: "Running late". Should I go ahead?' in said(spoken)

    spoken.out.clear()
    result = await agent.handle("yes", spoken)
    assert recorder.calls == [{"to": ["sam@x.com"], "body": "Running late"}]
    assert said(spoken) == "Sent."
    assert len(backend.requests) == 1  # the yes never went to the model
    # History alternates roles correctly for the next request.
    roles = [m["role"] for m in agent.messages]
    assert all(a != b for a, b in zip(roles, roles[1:]))


async def test_no_cancels_held_send(make_agent, spoken, recorder):
    backend = FakeBackend(reply(tool_use("send_email", {"to": ["a@b.c"], "body": "hi"})))
    agent = make_agent(backend)
    await agent.handle("email a", spoken)
    await agent.handle("no", spoken)
    assert recorder.calls == []
    assert not agent.gate.has_pending()


async def test_hedged_yes_is_not_a_yes(make_agent, spoken, recorder):
    backend = FakeBackend(
        reply(tool_use("send_email", {"to": ["a@b.c"], "body": "hi"})),
        reply(text("Sure, what should it say?")),
    )
    agent = make_agent(backend)
    await agent.handle("email a", spoken)
    await agent.handle("yes but change the wording", spoken)
    assert recorder.calls == []
    last_user = backend.requests[-1]["messages"][-1]["content"][0]["text"]
    assert "yes but change the wording" in last_user
    assert "was not carried out" in last_user


async def test_phone_never_sends_and_parks_draft(make_agent, spoken, recorder):
    backend = FakeBackend(
        reply(tool_use("send_email", {"to": ["a@b.c"], "body": "hi"})),
        reply(text("Saved as a draft.")),
    )
    agent = make_agent(backend)
    await agent.handle("email a", spoken, channel=Channel.PHONE)
    assert recorder.calls == [{"to": ["a@b.c"], "body": "hi"}]  # only the park (draft) handler ran
    assert not agent.gate.has_pending()
    assert len(agent.gate.parked()) == 1


async def test_reading_email_taints_conversation(make_agent, spoken):
    backend = FakeBackend(
        reply(tool_use("read_email", {"id": "1"})),
        reply(tool_use("send_email", {"to": ["evil@x.com"], "body": "money"}, id="tu_2")),
    )
    agent = make_agent(backend)
    await agent.handle("read my latest email", spoken)
    assert agent.tainted
    assert "This conversation has read email" in said(spoken)


async def test_think_harder_escalates_to_sonnet(make_agent, spoken):
    backend = FakeBackend(
        reply(tool_use("think_harder", {})),
        reply(thinking(), text("Tuesday works best.")),
    )
    result = await make_agent(backend).handle("Plan my week", spoken)
    assert result.models == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    deep = backend.requests[1]
    assert deep["thinking"] == {"type": "adaptive"}
    assert deep["fallbacks"] == "default"
    assert "think_harder" not in [t["name"] for t in deep["tools"]]


async def test_sonnet_thinking_blocks_dropped_for_haiku(make_agent, spoken):
    backend = FakeBackend(
        reply(tool_use("think_harder", {})),
        reply(thinking("plan"), text("Done.")),
        reply(text("Sure.")),
    )
    agent = make_agent(backend)
    await agent.handle("Plan my week", spoken)
    await agent.handle("thanks", spoken)
    haiku_req = backend.requests[2]
    blocks = [b["type"] for m in haiku_req["messages"] if m["role"] == "assistant" for b in m["content"]]
    assert "thinking" not in blocks


async def test_no_escalation_near_cap(make_agent, spoken):
    backend = FakeBackend(reply(tool_use("think_harder", {})), reply(text("Here's a quick answer.")))
    agent = make_agent(backend, cap=25.0)
    agent.ledger._append({"kind": "llm", "usd": 21.0})
    result = await agent.handle("Plan my week", spoken)
    assert result.models == ["claude-haiku-5-5", "claude-haiku-5-5"]
    assert "spending cap" in said(spoken)


async def test_capped_month_makes_no_calls(make_agent, spoken):
    backend = FakeBackend()
    agent = make_agent(backend, cap=1.0)
    agent.ledger._append({"kind": "llm", "usd": 1.5})
    await agent.handle("hello", spoken)
    assert backend.requests == []
    assert "cap" in said(spoken)


async def test_invalid_tool_input_is_reported_not_run(make_agent, spoken, recorder):
    backend = FakeBackend(
        reply(tool_use("add_reminder", {"content": 5})),
        reply(text("Let me try again.")),
    )
    await make_agent(backend).handle("remind me", spoken)
    assert recorder.calls == []
    result = backend.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "INVALID_INPUT" in result["content"]


async def test_truncated_tool_call_not_run(make_agent, spoken, recorder):
    backend = FakeBackend(reply(tool_use("add_reminder", {"content": "x"}), stop="max_tokens"))
    await make_agent(backend).handle("remind me", spoken)
    assert recorder.calls == []
    assert "cut off" in said(spoken)


async def test_refusal_is_plain(make_agent, spoken):
    backend = FakeBackend(reply(stop="refusal"))
    await make_agent(backend).handle("something", spoken)
    assert "can't help" in said(spoken)


async def test_undo_reverses_last_action(tmp_path, make_agent, spoken, registry, recorder):
    undone = []

    async def undo():
        undone.append(True)
        return "Removed it."

    registry.tools["add_reminder"].handler = recorder.handler("Added.", undo=undo)
    backend = FakeBackend(reply(tool_use("add_reminder", {"content": "milk"})), reply(text("Added milk.")))
    agent = make_agent(backend)
    await agent.handle("remind me to buy milk", spoken)
    assert await agent.undo.pop() == "Removed it."
    assert undone == [True]


async def test_interrupted_turn_leaves_valid_history(make_agent, spoken):
    import asyncio

    class Hang(FakeBackend):
        async def stream(self, *, on_text, **request):
            if len(self.requests) == 1:
                self.requests.append(request)
                await asyncio.sleep(10)
            return await super().stream(on_text=on_text, **request)

    backend = Hang(reply(tool_use("list_events", {"day": "today"})))
    agent = make_agent(backend)
    task = asyncio.create_task(agent.handle("tell me how coffee is made", spoken))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Every tool_use has a result and roles alternate, so the next request is valid.
    roles = [m["role"] for m in agent.messages]
    assert roles[-1] == "assistant" and all(a != b for a, b in zip(roles, roles[1:]))
    uses = {b["id"] for m in agent.messages for b in m["content"] if b["type"] == "tool_use"}
    results = {b["tool_use_id"] for m in agent.messages for b in m["content"] if b["type"] == "tool_result"}
    assert uses == results


async def test_no_says_cancelled(make_agent, spoken):
    backend = FakeBackend(reply(tool_use("send_email", {"to": ["me@x.com"], "body": "TARS test three"})))
    agent = make_agent(backend)
    await agent.handle("email me a note", spoken)
    spoken.out.clear()
    await agent.handle("no", spoken)
    assert said(spoken) == "Okay, cancelled. Nothing was sent."
