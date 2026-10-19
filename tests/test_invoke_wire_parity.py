"""Cross-runtime invoke wire parity (#132).

Teams reads an invoke's HTTP status as the invoke status and its body as
the invokeResponse body. Failed invokes and benign acks are HTTP 200 with
a JSON null body on both the serve runtime and the plugin runtime. This
test fails if those wires diverge.

A retry of an idempotent local name such as ``task/fetch`` still re-renders
on the plugin (#96). That is a real invokeResponse, not an empty ack, and
is covered by the plugin route tests. The dedupe case here is the empty
ack: serve does not re-forward a deduped invoke, and a repeated
``fileConsent/invoke`` accept with nothing pending (unknown consent, or a
replay after the upload entry was popped) acks the same way.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from test_activity_bridge import _cfg, _client_for, _inbound_message_activity
from test_agent365_plugin import _make_adapter, _make_inbound, adapter_mod

from hermes_a365.invoke import empty_invoke_ack

_CANONICAL = (200, b"null")
_AUTH = {"Authorization": "Bearer a.b.c"}


def _wire(response: Any) -> tuple[int, bytes]:
    return response.status_code, response.content


def _serve_invoke(activity_id: str) -> dict[str, Any]:
    return {
        **_inbound_message_activity(),
        "id": activity_id,
        "type": "invoke",
        "name": "adaptiveCard/action",
    }


def _serve_post(
    activity: dict[str, Any],
    *,
    webhook_response: Any = None,
    webhook_status: int = 200,
) -> Any:
    cfg = _cfg()
    cfg.skip_jwt_validation = True
    capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
    with _client_for(
        cfg,
        capture=capture,
        webhook_response=webhook_response,
        webhook_status=webhook_status,
    ) as client:
        return client.post("/api/messages", json=activity)


def _plugin_app(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, TestClient]:
    adapter = _make_adapter(monkeypatch)
    bridge = adapter_mod._import_bridge()
    monkeypatch.setattr(
        bridge,
        "validate_inbound_jwt",
        AsyncMock(return_value={"oid": "o1", "tid": "t1"}),
    )
    adapter._http_client = MagicMock()
    return adapter, TestClient(adapter.build_app())


def _file_consent(
    *,
    action: str,
    activity_id: str,
    consent_id: str = "consent-1",
) -> dict[str, Any]:
    body = _make_inbound(conv_id="conv-fc", activity_id=activity_id)
    body["type"] = "invoke"
    body["name"] = adapter_mod._FILE_CONSENT_INVOKE
    body["value"] = {"action": action, "context": {"consentId": consent_id}}
    return body


def _serve_outcome(outcome: str) -> tuple[int, bytes]:
    if outcome == "benign_ack":
        # Operator envelope is not an invokeResponse object, so there is
        # no body to forward.
        response = _serve_post(
            _serve_invoke("benign-1"),
            webhook_response={"invokeResponse": "not-an-object"},
        )
        return _wire(response)
    if outcome == "dedupe":
        cfg = _cfg()
        cfg.skip_jwt_validation = True
        capture: dict[str, Any] = {"webhook": [], "reply": [], "token": []}
        activity = _serve_invoke("dedupe-1")
        with _client_for(
            cfg,
            capture=capture,
            webhook_response={
                "invokeResponse": {"status": 200, "body": {"text": "ok"}}
            },
        ) as client:
            first = client.post("/api/messages", json=activity)
            second = client.post("/api/messages", json=activity)
        assert first.status_code == 200
        assert first.json() == {"text": "ok"}
        assert len(capture["webhook"]) == 1
        return _wire(second)
    if outcome == "handler_error":
        # Webhook returned 200 with a JSON array. That is not an
        # invokeResponse object; reading it as one must not 500.
        response = _serve_post(
            _serve_invoke("handler-1"),
            webhook_response=["not-an-object"],
        )
        return _wire(response)
    if outcome == "webhook_error":
        response = _serve_post(_serve_invoke("webhook-1"), webhook_status=500)
        return _wire(response)
    raise AssertionError(outcome)


def _plugin_outcome(outcome: str, monkeypatch: pytest.MonkeyPatch) -> tuple[int, bytes]:
    adapter, client = _plugin_app(monkeypatch)
    if outcome == "benign_ack":
        response = client.post(
            "/api/messages",
            json=_file_consent(action="decline", activity_id="benign-1"),
            headers=_AUTH,
        )
        return _wire(response)
    if outcome == "dedupe":
        # No pending upload: the accept is an idempotent re-ack, including
        # when Bot Framework retries the same activity id.
        activity = _file_consent(action="accept", activity_id="dedupe-1")
        first = client.post("/api/messages", json=activity, headers=_AUTH)
        second = client.post("/api/messages", json=activity, headers=_AUTH)
        assert _wire(first) == _CANONICAL
        assert first.json() is None
        return _wire(second)
    if outcome == "handler_error":

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(adapter_mod.invoke, "dispatch_invoke", boom)
        response = client.post(
            "/api/messages",
            json=_invoke_task(),
            headers=_AUTH,
        )
        assert adapter._handled_events == []
        return _wire(response)
    if outcome == "webhook_error":
        # The plugin has no operator webhook. A file-consent crash is the
        # other failure that used to emit a different empty body (``{}``).

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("file-consent crashed")

        monkeypatch.setattr(adapter, "_handle_file_consent", boom)
        response = client.post(
            "/api/messages",
            json=_file_consent(action="accept", activity_id="webhook-1"),
            headers=_AUTH,
        )
        return _wire(response)
    raise AssertionError(outcome)


def _invoke_task() -> dict[str, Any]:
    body = _make_inbound(conv_id="conv-I", activity_id="handler-1")
    body["type"] = "invoke"
    body["name"] = "task/fetch"
    body["value"] = {"commandId": "x"}
    return body


@pytest.mark.parametrize(
    "outcome",
    ["benign_ack", "dedupe", "handler_error", "webhook_error"],
)
def test_invoke_empty_ack_matches_across_runtimes(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ack = empty_invoke_ack()
    assert (ack.status, ack.body) == (200, None)
    serve = _serve_outcome(outcome)
    plugin = _plugin_outcome(outcome, monkeypatch)
    assert serve == plugin == _CANONICAL
    assert serve[0] < 500
