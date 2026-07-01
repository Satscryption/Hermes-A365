"""Typed Bot Framework invoke-activity dispatch — slice 19w-a (#18).

Invoke activities are a *synchronous* request/response wire: the reply must
come back in the **same HTTP turn** as the ``POST /api/messages`` request, as
an invokeResponse body (``{"status": int, "body": {...}}``) — NOT via the
async ``send()`` reply path. This module is the shared invoke foundation.
In v0.8.0 the plugin adapter (``plugin/adapter.py``) consumes it: the plugin
has no operator webhook, so a local typed handler must produce the
synchronous invokeResponse. The standalone ``serve`` runtime keeps its
operator-webhook invoke pass-through as its responder (the operator owns the
envelope there) and adopts this registry as typed per-name handlers land
(v0.8.1).

Two hard constraints:

- This module must NOT import from ``plugin/`` — the dependency direction is
  plugin → bridge, and ``activity_bridge`` imports nothing from ``plugin/``.
  That is why ``InvokeContext.conv`` is the raw ``activity.conversation``
  dict, not the plugin's ``ConversationRef`` (which ``serve`` has no access
  to anyway).
- Response builders must NOT route through ``render_reply_activity``. That
  helper stamps the #73a "AI generated" content-label entity, which belongs
  only on *message* activities; a card.invoke.response envelope must never
  inherit it. The builders here construct their envelope directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"


@dataclass(frozen=True)
class InvokeResponse:
    """A Bot Framework invokeResponse — the synchronous HTTP body.

    ``as_dict()`` yields the exact ``{"status": ..., "body": ...}`` shape the
    BF connector reads directly from the HTTP response body.
    """

    status: int
    body: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "body": self.body}


@dataclass(frozen=True)
class InvokeContext:
    """Everything a per-name invoke handler needs, assembled once per turn."""

    name: str
    activity: dict[str, Any]  # raw inbound activity (escape hatch)
    value: dict[str, Any]  # parsed activity.value ({} when absent/non-dict)
    user_oid: str
    user_upn: str
    tenant_id: str
    service_url: str
    conv: dict[str, Any]  # raw activity.conversation — NOT a ConversationRef
    chat_type: str  # "dm" | "group" | "channel" | "copilot_chat"
    path_tag: str  # "A" | "B" | "unknown"
    # 19w-b seam: the TokenFactory a tool-backed handler mints tokens with.
    # None for token-free names like task/fetch; wired when a v0.8.1 name needs it.
    delegated_token: Any | None = None
    # #21 hook seam (Work IQ V2 amplifiers); unwired in v0.8.0.
    blueprint_work_iq: Any | None = None


InvokeHandler = Callable[[InvokeContext], Awaitable["InvokeResponse"]]


def classify_chat_type(activity: dict[str, Any], path_tag: str) -> str:
    """Classify the invoke's chat surface, distinguishing Copilot Chat from a
    genuine Teams group.

    Copilot Chat is wire-indistinguishable from a Teams group chat
    (``conversationType=groupChat``, ``channelId=msteams``,
    ``19:…@thread.v2``). The ONLY discriminator inside Hermes-A365's lane is
    the inbound path tag: Path A is agentic-user 1:1 only (never groupChat),
    so a groupChat arriving on Path B is Copilot Chat. This is deliberately
    the minimal, non-defeatable signal — no channelData/tenant heuristic.
    """
    conv = activity.get("conversation") or {}
    conv_type = str(conv.get("conversationType") or "personal")
    if conv_type == "channel":
        return "channel"
    if conv_type == "groupChat":
        return "copilot_chat" if path_tag == "B" else "group"
    return "dm"


def build_invoke_context(
    activity: dict[str, Any],
    *,
    claims: dict[str, Any] | None,
    path_tag: str,
) -> InvokeContext:
    """Assemble an ``InvokeContext`` from a *validated* invoke activity + the
    decoded JWT claims.

    Identity fields prefer the validated JWT claims (``oid``/``upn``/``tid``)
    and fall back to activity fields (``from.aadObjectId``,
    ``conversation.tenantId``) so a claim-shape surprise on the live walk
    degrades gracefully rather than crashing.
    """
    claims = claims or {}
    conv = activity.get("conversation")
    conv = conv if isinstance(conv, dict) else {}
    sender = activity.get("from")
    sender = sender if isinstance(sender, dict) else {}
    recipient = activity.get("recipient")
    recipient = recipient if isinstance(recipient, dict) else {}
    value = activity.get("value")
    value = value if isinstance(value, dict) else {}
    return InvokeContext(
        name=str(activity.get("name") or ""),
        activity=activity,
        value=value,
        user_oid=str(claims.get("oid") or sender.get("aadObjectId") or ""),
        user_upn=str(claims.get("preferred_username") or claims.get("upn") or ""),
        tenant_id=str(
            claims.get("tid") or conv.get("tenantId") or recipient.get("tenantId") or ""
        ),
        service_url=str(activity.get("serviceUrl") or ""),
        conv=conv,
        chat_type=classify_chat_type(activity, path_tag),
        path_tag=path_tag,
    )


# ── Response builders (card.invoke.response envelopes) ─────────────────────
# Build the invokeResponse body DIRECTLY. NEVER call render_reply_activity
# (it stamps the #73a AI-generated content-label entity — invalid here).


def task_continue(card: dict[str, Any], *, title: str = "") -> InvokeResponse:
    """A task module that continues with an Adaptive Card (``taskInfo``)."""
    value: dict[str, Any] = {
        "card": {"contentType": _ADAPTIVE_CARD_CONTENT_TYPE, "content": card}
    }
    if title:
        value["title"] = title
    return InvokeResponse(status=200, body={"task": {"type": "continue", "value": value}})


def task_message(text: str) -> InvokeResponse:
    """A task module that closes with a plain message."""
    return InvokeResponse(status=200, body={"task": {"type": "message", "value": text}})


# ── Handlers ───────────────────────────────────────────────────────────────


async def _handle_task_fetch(ctx: InvokeContext) -> InvokeResponse:
    """v0.8.0 foundation handler for ``task/fetch``.

    Proves the invoke wire end-to-end: an inbound ``task/fetch`` returns a
    synchronous ``taskInfo`` card in the same HTTP turn. The card is
    synthesized locally — wiring rich task content to the agent loop needs a
    synchronous agent-reply channel the plugin runtime does not yet have (the
    agent loop is fire-and-forget), so real task content is deferred to the
    per-name children (v0.8.1). This handler needs no tool token.
    """
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": [
            {
                "type": "TextBlock",
                "text": "Request received.",
                "weight": "Bolder",
                "wrap": True,
            }
        ],
    }
    return task_continue(card, title="Hermes")


async def default_invoke_handler(ctx: InvokeContext) -> InvokeResponse:
    """Unknown invoke name → 501 (not-implemented). Total — never raises, so an
    unrecognised name yields a clean invokeResponse, not a 500 that Microsoft
    would read as an outage."""
    return InvokeResponse(
        status=501, body={"error": f"invoke name not implemented: {ctx.name}"}
    )


# The per-name registry. v0.8.0 ships exactly one typed name; every other name
# falls through to ``default_invoke_handler`` (plugin) or the operator-webhook
# pass-through fallback (serve). Children (#73/#76/#77/#82, search, signin …)
# register here in v0.8.1.
INVOKE_REGISTRY: dict[str, InvokeHandler] = {
    "task/fetch": _handle_task_fetch,
}


async def dispatch_invoke(
    ctx: InvokeContext, *, registry: dict[str, InvokeHandler] | None = None
) -> InvokeResponse:
    """Dispatch an ``InvokeContext`` to its registered handler, else the 501
    default. Never raises for an unknown name; a *handler's* own exception is
    the caller's concern (the route wraps it into a graceful envelope)."""
    reg = INVOKE_REGISTRY if registry is None else registry
    handler = reg.get(ctx.name, default_invoke_handler)
    return await handler(ctx)
