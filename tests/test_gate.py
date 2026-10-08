from __future__ import annotations

import time

import pytest

from tars.actions import Channel, Tier, Tool, schema
from tars.gate import ConfirmationGate, classify_reply


@pytest.mark.parametrize("reply", ["yes", "Yes.", "yeah", "Yep!", "send it", "go ahead", "Yes, send it.", "ok"])
def test_clear_yes(reply):
    assert classify_reply(reply) == "yes"


@pytest.mark.parametrize("reply", ["no", "No thanks", "cancel", "don't send it", "never mind"])
def test_clear_no(reply):
    assert classify_reply(reply) == "no"


@pytest.mark.parametrize("reply", [
    "yes but change the subject", "wait", "actually make it Friday", "maybe", "hmm",
    "yes, not to Sam", "what's the weather", "", "yes later",
])
def test_anything_else_is_not_a_yes(reply):
    assert classify_reply(reply) == "other"


async def _noop(args):
    return "ok"


def test_irreversible_tools_cannot_exist():
    with pytest.raises(ValueError):
        Tool("delete_all", "x", schema({}), Tier.IRREVERSIBLE, _noop)


def test_affects_others_needs_read_back():
    with pytest.raises(ValueError):
        Tool("send", "x", schema({}), Tier.AFFECTS_OTHERS, _noop)


async def test_pending_expires():
    gate = ConfirmationGate()
    tool = Tool("send", "x", schema({}), Tier.AFFECTS_OTHERS, _noop, read_back=lambda a: "Send it.")
    await gate.check(tool, {}, Channel.PC, tainted=False)
    assert gate.has_pending()
    gate.pending.created = time.time() - gate.PENDING_TTL_SECONDS - 1
    assert not gate.has_pending()
    res = await gate.resolve("yes", Channel.PC)
    assert res.passthrough and not res.executed


async def test_yes_from_phone_does_not_execute():
    ran = []

    async def handler(args):
        ran.append(args)
        return "sent"

    gate = ConfirmationGate()
    tool = Tool("send", "x", schema({}), Tier.AFFECTS_OTHERS, handler, read_back=lambda a: "Send it.")
    await gate.check(tool, {}, Channel.PC, tainted=False)
    res = await gate.resolve("yes", Channel.PHONE)
    assert not res.executed and ran == []
