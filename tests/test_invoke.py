"""Tests for hermes_a365.invoke — the shared invoke foundation (19w-a / #18)."""

from __future__ import annotations

import pytest

from hermes_a365 import invoke

# ---------------------------------------------------------------------------
# InvokeResponse
# ---------------------------------------------------------------------------


def test_invoke_response_as_dict() -> None:
    assert invoke.InvokeResponse(200, {"task": {}}).as_dict() == {
        "status": 200,
        "body": {"task": {}},
    }
    assert invoke.InvokeResponse(501).as_dict() == {"status": 501, "body": None}


# ---------------------------------------------------------------------------
# classify_chat_type — CC-groupChat vs Teams-group distinction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("conv_type", "path_tag", "expected"),
    [
        ("personal", "A", "dm"),
        ("personal", "B", "dm"),
        ("channel", "B", "channel"),
        ("groupChat", "B", "copilot_chat"),  # Copilot Chat arrives on Path B
        ("groupChat", "A", "group"),  # a genuine Teams group is not Path B
        ("groupChat", "unknown", "group"),  # default is NOT copilot_chat
        (None, "A", "dm"),  # missing conversationType -> personal -> dm
    ],
)
def test_classify_chat_type(conv_type: str | None, path_tag: str, expected: str) -> None:
    conv: dict = {"id": "c1"}
    if conv_type is not None:
        conv["conversationType"] = conv_type
    activity = {"conversation": conv}
    assert invoke.classify_chat_type(activity, path_tag) == expected


@pytest.mark.parametrize("bad_conv", ["19:x@thread.v2", [1, 2], 7])
def test_classify_chat_type_non_dict_conversation_degrades(bad_conv: object) -> None:
    # A truthy non-dict conversation must not crash (C1 regression guard) —
    # `or {}` only catches falsy values, so an isinstance guard is required.
    assert invoke.classify_chat_type({"conversation": bad_conv}, "B") == "dm"


# ---------------------------------------------------------------------------
# build_invoke_context — identity + value parsing
# ---------------------------------------------------------------------------


def _invoke_activity(**over: object) -> dict:
    base = {
        "type": "invoke",
        "name": "task/fetch",
        "value": {"commandId": "x"},
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "from": {"id": "29:user", "aadObjectId": "fallback-oid"},
        "recipient": {"id": "28:bot", "tenantId": "recip-tid"},
        "conversation": {"id": "19:c@thread.v2", "conversationType": "groupChat"},
    }
    base.update(over)
    return base


def test_build_invoke_context_prefers_claims() -> None:
    ctx = invoke.build_invoke_context(
        _invoke_activity(),
        claims={"oid": "claim-oid", "preferred_username": "u@x.io", "tid": "claim-tid"},
        path_tag="B",
    )
    assert ctx.name == "task/fetch"
    assert ctx.value == {"commandId": "x"}
    assert ctx.user_oid == "claim-oid"
    assert ctx.user_upn == "u@x.io"
    assert ctx.tenant_id == "claim-tid"
    assert ctx.chat_type == "copilot_chat"  # groupChat + Path B
    assert ctx.path_tag == "B"
    assert ctx.delegated_token is None
    assert ctx.blueprint_work_iq is None


def test_build_invoke_context_falls_back_to_activity_fields() -> None:
    # No claims -> oid from from.aadObjectId, tenant from recipient.tenantId.
    ctx = invoke.build_invoke_context(_invoke_activity(), claims=None, path_tag="A")
    assert ctx.user_oid == "fallback-oid"
    assert ctx.tenant_id == "recip-tid"
    assert ctx.chat_type == "group"  # groupChat + Path A (not CC)


def test_build_invoke_context_non_dict_value_becomes_empty() -> None:
    ctx = invoke.build_invoke_context(
        _invoke_activity(value="not-a-dict"), claims={}, path_tag="B"
    )
    assert ctx.value == {}


def test_build_invoke_context_non_dict_conversation_degrades() -> None:
    ctx = invoke.build_invoke_context(
        {"name": "task/fetch", "conversation": "19:x@thread.v2"},
        claims={},
        path_tag="B",
    )
    assert ctx.conv == {}
    assert ctx.chat_type == "dm"  # degrades, does not raise


# ---------------------------------------------------------------------------
# Response builders — must NOT carry the #73a AI-generated content label
# ---------------------------------------------------------------------------


def test_task_continue_shape_and_no_ai_label() -> None:
    card = {"type": "AdaptiveCard", "version": "1.5", "body": []}
    resp = invoke.task_continue(card, title="Hermes")
    assert resp.status == 200
    task = resp.body["task"]
    assert task["type"] == "continue"
    assert task["value"]["title"] == "Hermes"
    assert task["value"]["card"]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert task["value"]["card"]["content"] == card
    # No AI-generated content-label entity anywhere in an invoke response.
    assert "entities" not in resp.body
    assert "AIGeneratedContent" not in str(resp.body)


def test_task_message_shape() -> None:
    resp = invoke.task_message("done")
    assert resp.as_dict() == {
        "status": 200,
        "body": {"task": {"type": "message", "value": "done"}},
    }


# ---------------------------------------------------------------------------
# dispatch_invoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_task_fetch() -> None:
    ctx = invoke.build_invoke_context(_invoke_activity(), claims={}, path_tag="B")
    resp = await invoke.dispatch_invoke(ctx)
    assert resp.status == 200
    assert resp.body["task"]["type"] == "continue"
    assert "entities" not in resp.body


@pytest.mark.asyncio
async def test_dispatch_unknown_name_is_501() -> None:
    ctx = invoke.build_invoke_context(
        _invoke_activity(name="composeExtension/query"), claims={}, path_tag="B"
    )
    resp = await invoke.dispatch_invoke(ctx)
    assert resp.status == 501
    assert "composeExtension/query" in resp.body["error"]


@pytest.mark.asyncio
async def test_default_handler_never_raises() -> None:
    ctx = invoke.build_invoke_context(
        _invoke_activity(name="totally/unknown"), claims={}, path_tag="unknown"
    )
    # Must return a clean 501, not raise (a raise would become an HTTP 500).
    resp = await invoke.default_invoke_handler(ctx)
    assert resp.status == 501
