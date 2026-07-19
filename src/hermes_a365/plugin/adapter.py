"""Hermes gateway platform adapter — Microsoft Agent 365.

Slice 19n ports the bridge runtime under ``Agent365Adapter``: the
FastAPI ``/api/messages`` route, JWT validation, idempotency dedupe,
serviceUrl host-suffix gate, and outbound user-FIC chain that have
been baking in ``hermes_a365.activity_bridge`` since slices 19a-19j
now live behind Hermes' ``BasePlatformAdapter`` lifecycle.

Inbound flow::

    A365 / MCP Platform
        → POST {tunnel}/api/messages
        → JWT validation (slice 19f, AAD-v2)
        → idempotency dedupe (slice 19i)
        → serviceUrl suffix gate (slice 19j)
        → MessageEvent → self.handle_message(event)
        → Hermes agent loop runs

Outbound flow::

    Hermes calls self.send(chat_id, content, metadata=...)
        → look up cached inbound activity for chat_id
        → render reply activity (text + optional Adaptive Card)
        → mint outbound user-FIC bearer (slice 19e)
        → POST {serviceUrl}/v3/conversations/{conv}/activities/{activity}

The plugin imports the existing bridge helpers from
``hermes_a365.activity_bridge`` rather than copy-pasting ~600 lines —
that module is the single source of truth for the inbound validation
+ outbound auth machinery, and stays intact for the legacy ``serve``
entry point operators may still be running.

Configuration in ``config.yaml``::

    gateway:
      platforms:
        agent365:
          enabled: true
          extra:
            slug: inbox-helper
            port: 3978
            host: 127.0.0.1                       # bind interface
            blueprint_client_secret: ""           # or via env
            generated_config_path: ""             # default cwd/a365.generated.config.json

Or via environment variables:

- ``A365_TENANT_ID`` — tenant the bridge serves
- ``A365_APP_ID`` — blueprint app id
- ``AA_INSTANCE_ID`` — agent instance id
- ``HERMES_BRIDGE_PORT`` — port for FastAPI (default 3978)
- ``A365_BLUEPRINT_CLIENT_SECRET`` — bootstrap credential for
  user-FIC chain (otherwise read from generated config)

The plugin imports the Hermes harness's ``BasePlatformAdapter`` at
module level. When running outside a Hermes process (CI / unit tests
in this repo), the test fixture ``tests/test_agent365_plugin.py``
inserts stub modules into ``sys.modules`` so the import resolves
without requiring the harness on PYTHONPATH.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import stat
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hermes harness imports.
# ---------------------------------------------------------------------------

from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platforms.base import (  # noqa: E402
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key  # noqa: E402

from hermes_a365 import invoke  # noqa: E402
from hermes_a365._common import quote_path_segment, validate_slug  # noqa: E402

# Plugin-local imports — these don't depend on the Hermes harness.
from .conversations import ConversationRef, ConversationRegistry  # noqa: E402

# Bridge helpers are imported lazily inside methods so missing optional
# extras (e.g. fastapi for `activity-bridge serve`) produce a clear runtime
# error rather than blowing up at gateway-load time.

# #76 — inbound Teams file/media. A file upload arrives as an attachment with
# this contentType and a ``content.downloadUrl`` (pre-authenticated); inline
# images arrive as ``image/*`` attachments whose ``contentUrl`` needs the bot's
# reply bearer. Downloaded media lands in the platform media cache and its local
# path goes into ``MessageEvent.media_urls`` for the gateway's auto-vision /
# document path (mirrors the whatsapp_cloud adapter). Copilot Chat sends no
# attachments (files unsupported there), so this is a no-op on CC turns.
_TEAMS_FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"
# 25 MiB safety cap; the gateway enforces its own config-driven cap downstream.
_MAX_INBOUND_MEDIA_BYTES = 25 * 1024 * 1024

# #76c — outbound Teams file transfer. A local file is offered via a
# FileConsentCard; on Accept the user's ``fileConsent/invoke`` carries a
# pre-authenticated OneDrive ``uploadUrl`` we PUT the bytes to, then we confirm
# with a FileInfoCard. Personal (Teams 1:1) scope only — group/channel needs
# Graph (#76 slice d) and degrades to a text fallback here.
_FILE_CONSENT_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.consent"
_FILE_INFO_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.info"
_FILE_CONSENT_INVOKE = "fileConsent/invoke"
# Single-PUT upload cap (the OneDrive session accepts one range for the whole
# file well above this); chunked upload for larger files is a follow-up.
_MAX_OUTBOUND_FILE_BYTES = 25 * 1024 * 1024
# Review-F3 — a FileConsentCard the user never answers is stale after this; the
# pending entry is also popped on accept/decline and bounded by _MAX_CORRELATOR_ENTRIES.
_PENDING_UPLOAD_TTL_SEC = 3600.0

# Review-F1/F2 — trust-boundary allowlists for file transfer (same #100 threat
# model: activity-body URLs are attacker-influencable). Inbound image
# ``contentUrl`` is served by the Bot Framework *connector* and receives the reply
# bearer, so it is pinned to the connector allowlist
# (``DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES``) via ``_is_safe_fetch_url``.
#
# R2-P1: SharePoint/OneDrive file hosts (inbound ``downloadUrl`` + outbound
# ``uploadUrl``) are NOT pinned by a ``.sharepoint.com`` suffix — that zone is
# CUSTOMER-registrable (Microsoft documents that a requested SharePoint domain may
# already belong to another tenant), so a suffix match would accept an
# attacker-owned tenant's upload session and exfiltrate the user's file. They must
# be pinned to the deployment's own tenant hosts, supplied EXACTLY via
# ``A365_FILE_HOST_ALLOWLIST`` (comma-separated, e.g.
# ``contoso.sharepoint.com,contoso-my.sharepoint.com``). Empty ⇒ fail-closed: file
# transfer is refused until the operator configures the tenant host(s).


def _is_safe_fetch_url(url: str, suffixes: tuple[str, ...]) -> bool:
    """Review-F1/F2: True iff ``url`` is an https URL on an allowlisted Microsoft
    host and NOT an IP literal — the precondition before we attach the reply bearer
    to an inbound image ``contentUrl``. Reuses the bridge's fail-closed
    ``_is_trusted_service_url`` shape-matcher (https + non-registrable suffix /
    exact-host pin), then rejects IP-literal hosts as defence-in-depth. Used for
    the *connector* allowlist only — NOT for SharePoint file hosts (see
    ``_is_allowed_file_host``). Fail-closed on anything malformed / off-allowlist."""
    bridge = _import_bridge()
    if not bridge._is_trusted_service_url(url, suffixes):
        return False
    try:
        host = (urlparse(url).hostname or "").rstrip(".")
    except ValueError:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # a named allowlisted host is never an IP literal
    except ValueError:
        return True


def _is_allowed_file_host(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    """R2-P1: True iff ``url`` is https and its hostname is an EXACT match for one
    of the operator-configured tenant SharePoint/OneDrive hosts. Exact-match (not
    suffix) because ``*.sharepoint.com`` is customer-registrable — a suffix match
    would accept an attacker-owned tenant's upload/download session. Empty
    ``allowed_hosts`` ⇒ False (fail-closed: file transfer disabled until
    configured)."""
    if not allowed_hosts:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(host) and host in allowed_hosts


def _read_file_bounded(path: str, max_bytes: int) -> bytes | None:
    """R2-P2: read at most ``max_bytes`` from ``path`` through a single open
    descriptor, returning None if the file exceeds the cap (read ``max_bytes+1``
    and check) or can't be read. One fd + a hard bound closes the
    getsize()-then-read TOCTOU: the bytes returned are exactly what we hash and
    upload, and a file that grew past the cap is rejected rather than allocated
    unbounded."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    return data


# #73(c)/#82 — bound the per-lifetime feedback / handoff-token correlator maps
# so a long-running gateway can't grow them without limit (feedback is
# default-on; unclicked handoff links are never popped). Oldest entry is
# dropped past the cap (dict preserves insertion order).
_MAX_CORRELATOR_ENTRIES = 2048

# L2 (#105): cap the per-lifetime "seen this chat" set so a long-running
# gateway can't grow it without limit. Generous — it only holds chat-id
# strings and drives first-message detection; over-cap eviction just re-treats
# an old chat as "first" once (one extra greeting).
_MAX_SEEN_INBOUNDS = 8192

# M11 (#105): cap the durable conversation registry so it (and its on-disk
# save) can't grow without limit. Over-cap → LRU eviction skipping pinned +
# in-flight sessions (see ConversationRegistry.enforce_cap).
_MAX_REGISTRY_ENTRIES = 10000

# #77 — interactive-UI cards. Approve/deny, slash-confirm, and clarify prompts
# render as Adaptive Cards whose buttons are ``Action.Submit`` (documented CC-
# supported; both surfaces). A click returns an inbound ``message`` activity
# whose ``value`` is the button's ``data`` dict, tagged with ``hermes_kind`` so
# the route can route it back to the gateway's pending-approval resolvers
# (``tools.approval`` / ``tools.slash_confirm`` / ``tools.clarify_gateway``)
# instead of the agent loop.
_CARD_KIND_EXEC_APPROVAL = "exec_approval"
_CARD_KIND_SLASH_CONFIRM = "slash_confirm"
_CARD_KIND_CLARIFY = "clarify"
_CARD_ACTION_KINDS = frozenset(
    {_CARD_KIND_EXEC_APPROVAL, _CARD_KIND_SLASH_CONFIRM, _CARD_KIND_CLARIFY}
)

_DEFAULT_PORT = 3978

# Slice 19s-bis: Hermes' stream consumer appends a "cursor" character
# (default " ▉" from ``gateway/config.py::DEFAULT_STREAMING_CURSOR``)
# to intermediate streaming chunks so the user sees an animated
# in-progress indicator. BF's "Request streamed content should contain
# the previously streamed content" rule requires each chunk to start
# with the prior chunk's text — and a trailing cursor on chunk N puts
# a glyph at a position that chunk N+1 fills with real text, breaking
# the prefix match. Microsoft rejects with 403 ContentStreamNotAllowed.
#
# We strip the cursor before POSTing. Listed defensively rather than
# imported so the plugin stays importable in pytest contexts that
# don't pull in the Hermes harness.
_STREAMING_CURSORS_TO_STRIP: tuple[str, ...] = (" ▉", "▉")


def _bound_map(m: dict[str, Any], cap: int | None = None) -> None:
    """Drop oldest entries until ``m`` is within ``cap`` (in place). Dicts
    preserve insertion order, so the first key is the oldest. Keeps the
    per-lifetime feedback / handoff correlator maps from growing without
    limit on a long-running gateway. ``cap`` defaults to
    ``_MAX_CORRELATOR_ENTRIES`` (read at call time so it stays tunable)."""
    limit = _MAX_CORRELATOR_ENTRIES if cap is None else cap
    while len(m) > limit:
        m.pop(next(iter(m)))


def _strip_streaming_cursor(text: str) -> str:
    """Remove a trailing cursor glyph that Hermes' stream consumer
    appends to intermediate chunks. Idempotent + no-op when text
    doesn't end with one."""
    for cursor in _STREAMING_CURSORS_TO_STRIP:
        if cursor and text.endswith(cursor):
            return text[: -len(cursor)]
    return text


def _strip_one_mention(text: str, mention_text: str) -> str:
    """Remove every occurrence of ``mention_text`` plus the horizontal
    whitespace immediately around it.

    The seam collapses to a single space only when the mention sat between
    two non-space characters (so words don't fuse); at a line or string
    boundary it is removed entirely (no stray leading/trailing space).
    Whitespace *elsewhere* — interior runs and newlines — is never touched,
    so a legitimate multi-line body is not reflowed.
    """

    def _replace(match: re.Match[str]) -> str:
        start, end = match.span()
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        if before and after and not before.isspace() and not after.isspace():
            return " "
        return ""

    return re.compile(r"[ \t]*" + re.escape(mention_text) + r"[ \t]*").sub(_replace, text)


def _strip_recipient_mention(text: str, entities: Any, recipient_id: str) -> str:
    """Remove the bot's own ``<at>…</at>`` recipient-mention markup from
    inbound group/channel text — BF ``RemoveRecipientMention`` style (#78).

    Entity-driven: only a ``mention`` entity whose ``mentioned.id`` equals
    the activity's ``recipient.id`` (the bot itself, the same identity used
    for bot-self detection elsewhere) is stripped, so user-to-user mentions
    in the same message are preserved. Matching is on ``id`` not ``name`` —
    the entity's ``mentioned.name`` is a display name that differs from
    ``recipient.name``. The mention and the horizontal whitespace right
    around it are removed (see ``_strip_one_mention``); interior whitespace
    runs and newlines elsewhere are preserved, and the outer ends are
    trimmed only when a mention was actually removed — so a message with no
    recipient mention is returned byte-for-byte unchanged. The activity's
    ``entities`` list and ``raw_message`` are left untouched.

    A verified no-op on surfaces that pre-strip (Copilot Chat: the mention
    entity carries no ``text`` field) or carry no entities at all.
    """
    if not text or not recipient_id or not isinstance(entities, list):
        return text
    removed = False
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("type") != "mention":
            continue
        mentioned = entity.get("mentioned")
        if not isinstance(mentioned, dict):
            continue
        if str(mentioned.get("id") or "") != recipient_id:
            continue
        mention_text = entity.get("text")
        if isinstance(mention_text, str) and mention_text:
            new_text = _strip_one_mention(text, mention_text)
            if new_text != text:
                text = new_text
                removed = True
    return text.strip() if removed else text


# Slice 19s — BF streaming-response protocol pacing.
#
# Microsoft's documented hard throttle is 1 req/s, but the official
# guidance ("Buffer the tokens from the model for 1.5 to two seconds
# to ensure a smooth streaming process") recommends 1.5-2 s. We aim
# for the recommended pacing; this is the minimum gap between
# ``edit_message`` POSTs against the same stream.
#
# Reference: https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux
_STREAMING_MIN_GAP_SEC = 1.5
_STREAMING_FORCE_DROP_AFTER_SEC = 130.0
_STREAMING_FINALIZE_MAX_FAILURES = 2
_COALESCED_REPLY_FLUSH_AFTER_SEC = _STREAMING_FORCE_DROP_AFTER_SEC

# #53 — Hermes' status/lifecycle callbacks (retry/fallback traces, a
# terminal-failure summary) arrive as a rapid burst of same-``status_key``
# calls. Copilot Chat renders each as its own bubble because BF
# ``groupChat`` cannot edit a bubble in place (the Telegram trick from
# issue #30045). We coalesce the burst into one bubble; this debounce is
# how long we wait for the burst to settle before flushing. A terminal
# failure produces no reply, so a couple of seconds of latency on the
# consolidated notice is invisible.
_STATUS_COALESCE_FLUSH_AFTER_SEC = 2.0

# Status keys that carry a single substantive, user-must-see notice rather
# than a burst of transient retry/fallback traces. The agent emits these via
# ``status_callback("warn", ...)`` for degraded side paths (auxiliary
# compression / memory-flush failures: "the user needs to know something
# important failed", run_agent.py:_emit_warning). They are never a retry
# storm, so collapsing-into-one-bubble buys nothing — and the coalesce path's
# debounce/active-turn suppression can silently drop a leading notice that
# arrives just before a reply opens. Route them through plain ``send`` so they
# post immediately (or are CEA-suppressed by send() while a turn is active),
# never buffered-then-dropped at flush time.
#
# NOTE: the compression / context-pressure warning shares the *same*
# ``"lifecycle"`` key as transient retry/fallback traces
# (conversation_compression.py + run_agent.py:_emit_status both call
# ``status_callback("lifecycle", ...)``), so it is intentionally NOT covered
# here — there is no key-level way to tell a substantive lifecycle notice from
# retry noise, and bypassing coalesce for all "lifecycle" would defeat #53's
# terminal-failure burst collapse. A leading "lifecycle" notice emitted just
# before a reply opens within the debounce window is therefore knowingly
# lossy on Copilot Chat; that is an accepted tradeoff of sharing the key.
_STATUS_PASS_THROUGH_KEYS = frozenset({"warn"})


# Slice 19q (round-5 walkthrough finding, 2026-05-06): the BF connector
# delivers a handful of activities that aren't user messages and
# shouldn't reach the Hermes agent loop:
#
# - Classic channel-control activities (``conversationUpdate``,
#   ``typing``, ``endOfConversation``).
# - ``agents``-channel synthetic events Microsoft sends as part of the
#   AI Teammate onboarding / lifecycle flow. These carry a
#   conversation_id but the conversation isn't a chat — calling
#   ``send_typing`` against it 404s on the BF connector with
#   ``ServiceError``. Two observed shapes:
#     * ``type=event``, often with ``name=agentLifecycle``.
#     * ``type=message``, ``from.id=system`` (synthetic email-template
#       render activities).
#
# Routing any of these to ``handle_message`` wastes an agent turn,
# emits an empty Adaptive Card reply, and triggers the typing-pulse
# 404 spam. Ack-and-bail at the route level instead.
_CHANNEL_CONTROL_TYPES: frozenset[str] = frozenset(
    {"conversationUpdate", "typing", "endOfConversation"}
)


def _lifecycle_registry_action(activity: dict[str, Any]) -> str | None:
    """Classify a BF lifecycle activity into a registry action (#79).

    Returns:

    - ``"upsert"`` — capture/refresh the conversation reference so
      proactive delivery (#33/#67) can reach a chat the operator
      installed the agent into but nobody has messaged yet. Fires for
      ``installationUpdate`` (add) and ``conversationUpdate`` carrying
      ``membersAdded``.
    - ``"evict"`` — drop the conversation reference. Fires for
      ``installationUpdate`` (remove): the tenant uninstalled the agent,
      so proactive POSTs into the conversation must stop immediately.
    - ``None`` — not a lifecycle activity we act on; the caller's normal
      ``_should_dispatch`` channel-control / dispatch handling applies.

    Lifecycle activities are channel-control, NOT user turns: the route
    captures/evicts then ack-and-bails without an agent turn, and does
    NOT add the conversation to ``_seen_inbounds_this_lifetime`` — a
    lifecycle activity has no user-message activity id to
    ``replyToActivity`` against, so ``send()`` must route via the
    proactive ``sendToConversation`` path.

    Eviction fires for ``installationUpdate`` remove (the canonical
    uninstall hook) AND for ``conversationUpdate`` ``membersRemoved``
    carrying the bot itself (``recipient.id``) — the symmetric counterpart
    of the ``membersAdded`` bot-add capture below. On some channels/surfaces
    a group-chat bot removal is signalled by ``conversationUpdate``
    ``membersRemoved`` (bot) rather than ``installationUpdate`` remove, so
    not evicting on it would leave a stale ref that keeps proactive POSTing
    into a chat the bot was kicked from. An ordinary *user* leaving a
    still-live group (``membersRemoved`` without the bot id) must NOT evict
    — only the bot's own removal does.

    Synthetic ``agents``-channel probes (``from.id`` ``system`` or
    ``no-reply@…``) are classified as ``None`` here so they never reach
    the durable registry — they share the ``_should_dispatch``
    synthetic-sender screen but would otherwise bypass it, since the
    lifecycle interception runs *before* ``_should_dispatch``.

    ``conversationUpdate`` ``membersAdded`` only upserts when the bot
    itself (``recipient.id``) is among the added members — i.e. the agent
    was added to the conversation, the documented "operator installed the
    agent into this chat" signal. An ordinary user joining a still-live
    group is left to ``_should_dispatch``'s ack-and-bail so it neither
    churns the registry nor clobbers the cached inbound.
    """
    activity_type = str(activity.get("type") or "")
    if activity_type not in ("installationUpdate", "conversationUpdate"):
        return None
    # Screen synthetic agents-channel probes before any registry action:
    # the lifecycle interception runs ahead of _should_dispatch, so the
    # synthetic-sender filter there is unreachable for these types.
    if str(activity.get("channelId") or "") == "agents":
        sender = activity.get("from")
        sender_id = str(sender.get("id") or "") if isinstance(sender, dict) else ""
        if sender_id == "system" or sender_id.startswith("no-reply@"):
            return None
    if activity_type == "installationUpdate":
        if str(activity.get("action") or "").lower() == "remove":
            return "evict"
        return "upsert"
    recipient = activity.get("recipient")
    bot_id = str(recipient.get("id") or "") if isinstance(recipient, dict) else ""
    # A missing recipient/bot id can't be matched against members, so
    # neither the add nor the remove branch can fire — fall through to None.
    if not bot_id:
        return None

    def _member_ids(key: str) -> set[str]:
        members = activity.get(key)
        if not isinstance(members, list):
            return set()
        return {str(m.get("id") or "") for m in members if isinstance(m, dict)}

    # Only act when the bot ITSELF was added/removed (install/uninstall
    # into-this-chat), not an ordinary user join/leave of a still-live
    # group. Removal is checked first so a single activity carrying the
    # bot in both lists (pathological) resolves to evict.
    if bot_id in _member_ids("membersRemoved"):
        return "evict"
    if bot_id in _member_ids("membersAdded"):
        return "upsert"
    return None


def _should_dispatch(activity: dict[str, Any]) -> bool:
    """Return ``True`` for activities the agent loop should reason about.

    ``False`` for BF channel-control + synthetic ``agents``-channel
    probes. Pure-function so the route stays small and tests can
    exercise the matrix without spinning up a TestClient.

    The ``agents``-channel synthetic-sender filter was extended after
    the §9d round-5 walkthrough caught the email-template render
    activity slipping through under the literal ``"system"`` filter
    (real ``from.id`` is ``no-reply@teams.mail.microsoft``).
    """
    activity_type = str(activity.get("type") or "message")
    if activity_type in _CHANNEL_CONTROL_TYPES:
        return False
    channel_id = str(activity.get("channelId") or "")
    if channel_id == "agents":
        if activity_type == "event":
            return False
        sender = activity.get("from")
        sender_id = ""
        if isinstance(sender, dict):
            sender_id = str(sender.get("id") or "")
        # ``system`` is the literal Microsoft uses for lifecycle
        # event senders. ``no-reply@…`` covers the email-template
        # render activities Teams ships when an unread Copilot
        # notification arrives. Both classes are synthetic and
        # waste agent turns; real Teams users never use either id.
        if sender_id == "system" or sender_id.startswith("no-reply@"):
            return False
    return True


def _conversations_activities_url(service_url: str, conv_id: str) -> str:
    """``{serviceUrl}/v3/conversations/{id}/activities`` with the id encoded.

    #103 / M4: conversation ids come from inbound activities (or a
    registry seeded by them); percent-encoding the id as a single path
    segment stops ``/``, ``?``, ``#``, or ``../`` inside it from
    shifting the request target on the trusted host while the bearer
    stays attached. BF accepts encoded segments (Teams ids like
    ``19:abc@thread.tacv2`` become ``19%3Aabc%40thread.tacv2``).
    """
    return (
        f"{service_url}/v3/conversations/"
        f"{quote_path_segment(str(conv_id))}/activities"
    )


def _import_bridge() -> Any:
    """Import the bridge module on demand. Returns the module object."""
    from hermes_a365 import activity_bridge

    return activity_bridge


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class Agent365Adapter(BasePlatformAdapter):
    """Hermes platform adapter for Microsoft Agent 365 surfaces."""

    # A365 BF connector times out around 15 s. Replies must fit under
    # that for interactive turns; #4 covers the proactive pattern for
    # longer reasoning.
    MAX_MESSAGE_LENGTH = 4000

    # Slice 19s — Microsoft's BF streaming requires an explicit
    # ``endStream()`` (i.e. a final activity with
    # ``streamType=final``); the surface treats the message as
    # still-streaming otherwise. The flag tells Hermes' stream
    # consumer to route the final ``edit_message(finalize=True)``
    # through even when content is unchanged.
    REQUIRES_EDIT_FINALIZE: bool = True

    def __init__(self, config: PlatformConfig, **_kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform("agent365"))

        extra = getattr(config, "extra", {}) or {}

        # Connection / runtime config
        # #103/M9 + review P2: an EXPLICITLY configured slug must be
        # path-safe. Fail closed (refuse to construct the adapter) on an
        # invalid one — falling back to "" would route this profile's
        # durable state to ~/.hermes/agents/default/, where under multiplex
        # two invalid profiles (or an invalid one + a real "default")
        # would share and clobber conversations.json / bridge.log /
        # bridge.pid. A genuinely absent slug still resolves to "default".
        _slug_raw = str(extra.get("slug") or os.getenv("AGENT_IDENTITY") or "")
        if _slug_raw:
            self.slug: str = validate_slug(_slug_raw)
        else:
            self.slug = ""
        self.host: str = str(extra.get("host") or "127.0.0.1")
        self.port: int = int(
            os.getenv("HERMES_BRIDGE_PORT") or extra.get("port") or _DEFAULT_PORT
        )

        # Tenant + blueprint identity (also pulled by load_bridge_config when
        # available, but env-first lookup keeps the plugin loadable when
        # generated config isn't on disk in the gateway's cwd).
        self.tenant_id: str = os.getenv("A365_TENANT_ID") or str(
            extra.get("tenant_id") or ""
        )
        self.blueprint_app_id: str = os.getenv("A365_APP_ID") or str(
            extra.get("app_id") or ""
        )
        self.blueprint_client_secret: str = os.getenv(
            "A365_BLUEPRINT_CLIENT_SECRET"
        ) or str(extra.get("blueprint_client_secret") or "")

        # #36: optional separate non-agentic Path B identity. Empty
        # defaults to the blueprint app (which fails AADSTS82001 on
        # outbound for #36's reasons). Operators following the #36
        # walk register a second Entra app + set these env vars.
        self.bf_app_id: str = os.getenv("A365_BF_APP_ID") or str(
            extra.get("bf_app_id") or ""
        )
        self.bf_client_secret: str = os.getenv("A365_BF_CLIENT_SECRET") or str(
            extra.get("bf_client_secret") or ""
        )

        self._generated_config_path: Path = Path(
            extra.get("generated_config_path")
            or os.getenv("A365_GENERATED_CONFIG_PATH")
            or (Path.cwd() / "a365.generated.config.json")
        )

        # Slice 19o — durable conversation registry keyed on
        # `conversation.id`. Persists to
        # `~/.hermes/agents/<slug>/conversations.json` so proactive
        # sends and longer conversations work across uvicorn restarts.
        self._conversations_path: Path = Path(
            extra.get("conversations_path")
            or os.getenv("A365_CONVERSATIONS_PATH")
            or (
                Path.home()
                / ".hermes"
                / "agents"
                / (self.slug or "default")
                / "conversations.json"
            )
        )
        self._conversations: ConversationRegistry = ConversationRegistry.load(
            self._conversations_path
        )

        # Slice 19x-d (#4): conversations registry prune threshold.
        # Default 30 days matches Hermes' SessionStore reset policy.
        # Operators wire `await adapter.prune_conversations()` from cron
        # to drop dead chats without restarting the gateway.
        raw_prune = extra.get("conversations_prune_max_age_days")
        try:
            self._conversations_prune_max_age_days: float = (
                float(raw_prune) if raw_prune is not None else 30.0
            )
        except (ValueError, TypeError):
            self._conversations_prune_max_age_days = 30.0

        # Lazily-built runtime objects (populated in connect()).
        self._http_client: Any = None
        self._jwks_cache: Any = None  # AAD-v2 / A365 path (slice 19f)
        self._bf_jwks_cache: Any = None  # Bot Framework inbound (#34)
        self._idempotency_cache: Any = None
        self._fmi_cache: Any = None  # Path A T1/T2 cache (slice 19e)
        self._user_cache: Any = None  # Path A per-user final token cache
        self._bf_token_cache: Any = None  # Path B outbound bearer cache (#33)
        self._bridge_cfg: Any = None
        self._app: Any = None
        self._uvicorn_server: Any = None
        self._uvicorn_task: asyncio.Task | None = None

        # Slice 19s — per-stream state for BF streaming-response protocol.
        # Keyed on the Hermes-side ``message_id`` (the activity id returned
        # by ``send()``). Values: ``{"bf_stream_id", "sequence", "last_emit_ts"}``.
        # Each ``edit_message`` call increments ``sequence``; the first call
        # captures the BF-side ``streamId`` from the 201 response. Entries
        # are dropped on ``finalize=True`` or terminal 403.
        self._streams: dict[str, dict[str, Any]] = {}

        # Slice 19s-bis — at most one active BF stream per conversation
        # (Microsoft's "one streaming sequence per user turn" rule from the
        # custom-engine-agents doc). Maps chat_id → message_id key into
        # ``self._streams``. ``send()`` consults this to decide whether to
        # start a stream or emit a non-streaming reply. Cleared on finalize
        # or terminal 403.
        self._active_stream_by_chat: dict[str, str] = {}

        # #54 branch-walk correction: Copilot Chat arrives as
        # ``groupChat`` and renders BF streaming activities silently,
        # even though BF accepts them. For non-personal conversations,
        # keep Hermes' stream-consumer out of fallback mode by buffering
        # streamed chunks under a synthetic message id and emitting one
        # normal ``send_reply`` only when ``edit_message(finalize=True)``
        # arrives. Maps synthetic message_id -> buffer state.
        self._coalesced_replies: dict[str, dict[str, Any]] = {}
        self._active_coalesced_reply_by_chat: dict[str, str] = {}
        self._coalesced_reply_tasks: dict[str, asyncio.Task] = {}

        # Slice 19x-e (#27) — per-lifetime set of chat_ids the gateway has
        # captured an inbound for since boot. Used as ``send()``'s gate
        # for routing through the proactive ``sendToConversation`` path
        # rather than ``replyToActivity``: the latter requires a fresh
        # ``activity_id`` from this lifetime; the former does not.
        #
        # NOT persisted — every gateway restart starts fresh, so a
        # send() to a chat the registry knows about but the gateway
        # hasn't heard from this lifetime correctly takes the proactive
        # path. Surfaced during the v0.5.0 soak (2026-05-13); see #27
        # for the gating finding the registry-raw-as-gate logic missed.
        self._seen_inbounds_this_lifetime: set[str] = set()

        # M11 (#105): bridge the base's whole-turn in-flight signal
        # (`_active_sessions`, keyed by session key) into the registry's
        # conversation-id space. Recorded at dispatch: session_key → the
        # conversation_id it belongs to. `_active_conversation_ids()`
        # intersects this with the live `_active_sessions` so the registry
        # cap/prune never evict a conversation whose turn is running — which,
        # since the base runs the turn in a background task that outlives
        # handle_message, includes turns suspended awaiting a human
        # approval/clarify. Self-cleans mappings whose session has ended.
        self._session_key_to_conv: dict[str, str] = {}

        # M11 (#105): serialize registry saves. _persist_conversations builds
        # its snapshot AND runs the off-loop write while holding this lock, so
        # two concurrent inbounds can't let an older snapshot's os.replace land
        # after a newer one's (which would silently stale/drop entries on disk).
        # The lock is async — the event loop keeps serving during a write.
        self._persist_lock = asyncio.Lock()

        # Slice 19s-bis follow-up — Hermes' stream consumer can call
        # ``edit_message`` more than once with the same ``message_id``
        # after a legitimate ``finalize=True`` succeeds (e.g. an
        # ``_already_sent``/``_final_response_sent`` ordering quirk
        # double-finalises the same stream). After we drop stream state
        # on the legitimate close, those follow-ups arrive with
        # ``is_first=True`` and POST malformed activities:
        # ``streamType=final`` without ``streamId`` → 400 BadSyntax, or
        # ``streamSequence>1`` without ``streamId`` → 400. Both leave a
        # stuck "thinking" bubble on the user's surface.
        #
        # We track recently-finalized message_ids so duplicate calls
        # no-op (return success). 5-minute TTL is plenty (a BF stream
        # can't outlive 2 minutes; 5 covers slow-clock skew).
        self._recently_finalized: dict[str, float] = {}
        self._recently_finalized_ttl_sec = 300.0

        # #53 — gateway status/lifecycle callbacks (retry/fallback traces,
        # terminal-failure summaries) are routed through
        # ``send_or_update_status`` when the adapter implements it (see
        # ``gateway/run.py`` ``_send_or_update_status_coro``). Copilot Chat
        # cannot edit a bubble in place (unlike Telegram, #30045), so for
        # non-personal chats we coalesce a burst of same-key status lines
        # into ONE bubble: append distinct lines under a synthetic key and
        # flush via a short debounce watchdog (mirrors the coalesced-reply
        # machinery above). Personal (Teams 1:1) status passes straight
        # through to ``send`` — Path A behaviour is preserved.
        self._coalesced_status: dict[str, dict[str, Any]] = {}
        self._coalesced_status_tasks: dict[str, asyncio.Task] = {}

        # #76c — outbound file transfer. Maps our generated ``consentId`` ->
        # {"path": <safe local path>, "name": <file name>} for a FileConsentCard
        # awaiting the user's Accept/Decline. Consumed (popped) by the
        # ``fileConsent/invoke`` handler; a consent the user never answers is
        # simply never uploaded. In-memory + per-lifetime (mirrors
        # ``_active_stream_by_chat``) — a gateway restart drops pending offers,
        # so a late Accept acks gracefully without an upload.
        self._pending_file_uploads: dict[str, dict[str, Any]] = {}

        # #73(c) — feedback-loop opt-in. Default on; operators disable via
        # A365_FEEDBACK_LOOP=0. Gates the channelData.feedbackLoop stamp so the
        # thumbs up/down affordance can be turned off without a code change.
        self._feedback_enabled = os.environ.get(
            "A365_FEEDBACK_LOOP", "1"
        ).strip().lower() not in ("0", "false", "no", "off", "")

        # #82 — "continue in Teams" deep-link affordance on degraded Copilot Chat
        # replies. Default OFF (a UX-changing surface that needs walk validation
        # and could read as noise); operators opt in with A365_HANDOFF_LINK=1.
        self._handoff_link_enabled = os.environ.get(
            "A365_HANDOFF_LINK", "0"
        ).strip().lower() in ("1", "true", "yes", "on")

        # R2/R3-P1 — EXACT tenant SharePoint/OneDrive hosts allowed as inbound file
        # downloadUrl / outbound uploadUrl destinations. Empty ⇒ fail-closed: file
        # transfer degrades to a text fallback until the operator pins the tenant
        # host(s) — a `*.sharepoint.com` suffix would accept an attacker-owned
        # tenant's session (customer-registrable zone).
        #
        # R3-P1: sourced from the PROFILE config (``extra.file_host_allowlist`` —
        # a list or a comma-separated string) first, so multiplexed profiles
        # (gateway.multiplex_profiles) each pin their OWN tenant rather than
        # sharing a process-env union. ``A365_FILE_HOST_ALLOWLIST`` is the
        # single-profile compatibility fallback.
        _fha = extra.get("file_host_allowlist")
        if _fha is None:
            _fha = os.environ.get("A365_FILE_HOST_ALLOWLIST", "")
        if isinstance(_fha, str):
            _fha_items: list[Any] = _fha.split(",")
        elif isinstance(_fha, (list, tuple)):
            _fha_items = list(_fha)
        else:
            # Misconfig (e.g. `file_host_allowlist: 5` / `true` / a mapping) — a
            # non-str/non-list is not a host list. Fail-closed (empty) rather than
            # crashing plugin load on ``list(<scalar>)``.
            _fha_items = []
        self._file_host_allowlist: tuple[str, ...] = tuple(
            str(h).strip().lower() for h in _fha_items if str(h).strip()
        )

        # #73(c) — message-id -> {reaction, feedback, ...} captured from
        # ``message/submitAction`` feedback invokes. Teams stores nothing, so we
        # keep the latest reaction per replied message (in-memory, per-lifetime).
        self._feedback_by_message_id: dict[str, dict[str, Any]] = {}

        # #82 — Copilot→Teams handoff. Maps a minted continuation token ->
        # {"conversation_id", "chat_type", ...} so a ``handoff/action`` invoke
        # (fired when the user clicks the "continue in Teams" deep link) can
        # resolve the originating CC session. In-memory + per-lifetime — a
        # restart drops tokens and the deep link simply lands in a fresh 1:1.
        self._handoff_tokens: dict[str, dict[str, Any]] = {}

        # #73(c)/#82 — per-instance invoke registry: the shared #18 table plus
        # adapter-bound children that need adapter state (feedback map / handoff
        # tokens / ConversationRegistry). Passed to ``dispatch_invoke`` so the
        # module-level ``INVOKE_REGISTRY`` stays plugin-free.
        self._invoke_registry: dict[str, invoke.InvokeHandler] = {
            **invoke.INVOKE_REGISTRY,
            "message/submitAction": self._handle_feedback_submit,
            "message/fetchTask": self._handle_message_fetch_task,
            "handoff/action": self._handle_handoff_action,
        }

    @property
    def name(self) -> str:
        return "Agent 365"

    # ── Configuration helpers ─────────────────────────────────────────────

    def _load_secret_from_generated_config(self) -> str:
        """Best-effort read of `agentBlueprintClientSecret` from the
        local generated config. Returns empty string on miss."""
        try:
            data = json.loads(self._generated_config_path.read_text())
        except (OSError, json.JSONDecodeError):
            return ""
        secret = data.get("agentBlueprintClientSecret") if isinstance(data, dict) else None
        return secret if isinstance(secret, str) else ""

    def _ensure_secret(self) -> str:
        if self.blueprint_client_secret:
            return self.blueprint_client_secret
        secret = self._load_secret_from_generated_config()
        if secret:
            self.blueprint_client_secret = secret
        return self.blueprint_client_secret

    def _make_bridge_config(self) -> Any:
        """Construct a `BridgeConfig` for the bridge helpers (token
        acquisition, JWT validation, send_reply)."""
        bridge = _import_bridge()
        secret = self._ensure_secret()
        if not (self.tenant_id and self.blueprint_app_id and secret):
            raise RuntimeError(
                "agent365 adapter is missing tenant_id / blueprint_app_id / "
                "blueprint_client_secret — check A365_TENANT_ID, A365_APP_ID, "
                "and A365_BLUEPRINT_CLIENT_SECRET (or generated config path)"
            )
        log_path = Path.home() / ".hermes" / "agents" / (self.slug or "default") / "bridge.log"
        pid_path = log_path.with_name("bridge.pid")
        return bridge.BridgeConfig(
            slug=self.slug or "default",
            tenant_id=self.tenant_id,
            blueprint_client_id=self.blueprint_app_id,
            blueprint_client_secret=secret,
            webhook_url="",  # unused — we dispatch via handle_message instead
            log_path=log_path,
            pid_path=pid_path,
            bf_app_id=self.bf_app_id,
            bf_client_secret=self.bf_client_secret,
        )

    # ── FastAPI app construction (separated for testability) ──────────────

    def build_app(self) -> Any:
        """Build the FastAPI app this adapter serves on `connect()`.

        Exposed on the instance so unit tests can drive routes via
        ``fastapi.testclient.TestClient(adapter.build_app())`` without
        binding a real socket.
        """
        bridge = _import_bridge()
        from fastapi import Body, FastAPI, Header, HTTPException
        from fastapi.responses import JSONResponse

        app = FastAPI(title=f"agent365 adapter — {self.slug or 'default'}")

        # Caches are bound here so build_app() is callable from tests
        # without having to also call connect(). Production connect()
        # builds them once before this method runs.
        if self._jwks_cache is None:
            self._jwks_cache = bridge._JwksCache()
        if self._bf_jwks_cache is None:
            self._bf_jwks_cache = bridge._JwksCache()
        if self._idempotency_cache is None:
            self._idempotency_cache = bridge._IdempotencyCache(
                ttl_seconds=bridge.DEFAULT_IDEMPOTENCY_TTL_SECONDS,
            )

        @app.get("/healthz")
        async def healthz() -> dict[str, Any]:
            return {
                "ok": True,
                "slug": self.slug,
                "blueprint_client_id": (
                    self.blueprint_app_id[:8] + "…" if self.blueprint_app_id else ""
                ),
            }

        @app.post("/api/messages")
        async def messages(
            activity: dict[str, Any] = Body(...),  # noqa: B008
            authorization: str | None = Header(default=None),
        ) -> Any:
            # Request-level inbound observability. Previously the plugin
            # logged nothing for non-dispatched POSTs (lifecycle, channel
            # control, synthetic probes, gate rejections), making it
            # impossible to tell from the log whether a given activity (e.g.
            # an installationUpdate) even reached the endpoint. Log every
            # inbound's shape-defining fields here, BEFORE any gate, so the
            # full picture is visible. No token/secret is logged.
            _in_conv = activity.get("conversation")
            _in_conv = _in_conv if isinstance(_in_conv, dict) else {}
            _in_from = activity.get("from")
            _in_from = _in_from if isinstance(_in_from, dict) else {}
            logger.info(
                "inbound activity type=%s action=%s channelId=%s from=%s "
                "convType=%s conv=%s membersAdded=%s membersRemoved=%s",
                activity.get("type"),
                activity.get("action"),
                activity.get("channelId"),
                _in_from.get("id"),
                _in_conv.get("conversationType"),
                _in_conv.get("id"),
                bool(activity.get("membersAdded")),
                bool(activity.get("membersRemoved")),
            )

            # Slice 19j — serviceUrl gate before anything else.
            service_url = activity.get("serviceUrl") or ""
            trusted_suffixes = bridge.DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES
            if not trusted_suffixes:
                logger.warning(
                    "inbound 403 reason=config-bug detail=empty-trusted-suffixes"
                )
                raise HTTPException(
                    status_code=403,
                    detail="trusted_service_url_suffixes is empty — refusing to "
                    "process inbound activity. This is a config bug.",
                )
            if not bridge._is_trusted_service_url(service_url, trusted_suffixes):
                logger.warning(
                    "inbound 403 reason=untrusted-service-url url=%r",
                    service_url,
                )
                raise HTTPException(
                    status_code=403,
                    detail=f"untrusted serviceUrl: {service_url!r}",
                )

            # Bearer presence check (shared by Path A and Path B).
            if not authorization or not authorization.lower().startswith("bearer "):
                logger.warning("inbound 401 reason=missing-bearer-token")
                raise HTTPException(status_code=401, detail="missing bearer token")
            token = authorization.split(None, 1)[1]

            # #34 — peek unverified `iss` to pick the right validator.
            # BF tokens (Direct Line / Teams via Bot Service / Test in
            # Web Chat) say ``https://api.botframework.com``; A365 /
            # MCP Platform tokens say
            # ``https://login.microsoftonline.com/<tid>/v2.0``.
            # Unverified peek is a routing hint only — both validator
            # branches do full signature checks, so a malformed token
            # gets rejected either way; default to the A365 path on
            # peek failure to preserve pre-#34 behaviour.
            claims: dict[str, Any] = {}
            iss = bridge.peek_unverified_iss(token)
            # L4 (#100, #106 review) — capture the JWT-validated path (which
            # validator passes: BF -> Path B, AAD-v2 -> Path A) so decoupled
            # agent-loop sends bind to it via the ConversationRef, never
            # re-deriving the path from the untrusted activity body.
            validated_path: str | None = "B" if iss == bridge.BF_ISSUER else "A"
            if iss == bridge.BF_ISSUER:
                # #36: when the operator has migrated the bot's
                # `--appid` to the non-agentic Path B identity, BF
                # signs inbound tokens with `aud = bf_app_id` rather
                # than the blueprint. Use bf_app_id when set; fall
                # back to blueprint to preserve pre-#36 behaviour
                # (bot's --appid still being the blueprint).
                bf_expected_aud = self.bf_app_id or self.blueprint_app_id
                logger.info(
                    "inbound path=B (iss=%s aud=%s…)",
                    iss,
                    bf_expected_aud[:8] if bf_expected_aud else "",
                )
                try:
                    claims = await bridge.validate_inbound_jwt_bf(
                        token=token,
                        expected_app_id=bf_expected_aud,
                        expected_service_url=service_url,
                        client=self._http_client,
                        cache=self._bf_jwks_cache,
                    )
                except bridge.JwtValidationError as e:
                    logger.warning(
                        "inbound 403 path=B reason=%s", e
                    )
                    raise HTTPException(status_code=403, detail=str(e)) from e
            else:
                logger.info("inbound path=A (iss=%r)", iss)
                try:
                    claims = await bridge.validate_inbound_jwt(
                        token=token,
                        tenant_id=self.tenant_id,
                        expected_app_id=self.blueprint_app_id,
                        azp_allowlist=bridge.DEFAULT_INBOUND_AZP_ALLOWLIST,
                        client=self._http_client,
                        cache=self._jwks_cache,
                    )
                except bridge.JwtValidationError as e:
                    logger.warning(
                        "inbound 403 path=A reason=%s", e
                    )
                    raise HTTPException(status_code=403, detail=str(e)) from e

            # Slice 19w-a (#18) — invoke activities are a synchronous
            # request/response wire: the reply is the HTTP response of THIS POST
            # (HTTP body = the invokeResponse body, HTTP status = the invoke
            # status). They must NOT fall through to handle_message (fire-and-
            # forget, no sync return channel) — the pre-#18 bug (Microsoft logs
            # 200, user sees no response).
            #
            # Intercepted BEFORE the idempotency dedupe (#96): an invoke is
            # synchronous and today's names (task/fetch) are local + idempotent,
            # so a Bot Framework retry must re-render its taskInfo, not receive
            # the {status:duplicate} marker (which is not a valid invokeResponse
            # body). Invokes never enter the agent loop, so bypassing the message
            # dedupe cannot double-fire it; when a side-effectful invoke name
            # lands, 19w-g adds per-name response replay. A handler crash still
            # returns a graceful {status:500} invokeResponse, not an HTTP 500.
            if str(activity.get("type") or "") == "invoke":
                invoke_name = str(activity.get("name") or "")
                # #76c — fileConsent/invoke is the user's Accept/Decline on an
                # outbound FileConsentCard. It drives an upload side-effect (PUT
                # bytes to a OneDrive session) + a FileInfoCard ack, so it is NOT
                # a #18 typed-invoke child (task/fetch/search) and never enters
                # the agent loop. Handled here, ahead of the message dedupe, so a
                # BF retry re-acks idempotently (the pending entry is popped on
                # first handling — see ``_handle_file_consent``).
                if invoke_name == _FILE_CONSENT_INVOKE:
                    try:
                        resp = await self._handle_file_consent(
                            activity, validated_path=validated_path
                        )
                    except Exception as e:
                        logger.error("agent365 fileConsent handler crashed: %s", e)
                        resp = invoke.InvokeResponse(status=200, body={})
                    logger.info(
                        "inbound invoke name=%s status=%s", invoke_name, resp.status
                    )
                    return JSONResponse(resp.body, status_code=resp.status)
                # Context assembly (_inbound_path_tag / build_invoke_context) is
                # inside the try too: a wire-shape surprise on the live walk must
                # degrade to a graceful {status:500} invokeResponse, never an
                # unhandled HTTP 500 that Microsoft reads as an outage.
                try:
                    path_tag = bridge._inbound_path_tag(activity)
                    ctx = invoke.build_invoke_context(
                        activity, claims=claims, path_tag=path_tag
                    )
                    resp = await invoke.dispatch_invoke(
                        ctx, registry=self._invoke_registry
                    )
                except Exception as e:
                    logger.error(
                        "agent365 invoke handler failed: name=%s %s", invoke_name, e
                    )
                    resp = invoke.InvokeResponse(
                        status=500, body={"error": "invoke handler error"}
                    )
                logger.info(
                    "inbound invoke name=%s status=%s", invoke_name, resp.status
                )
                # BF wire: the HTTP body is the invokeResponse *body*, and the
                # HTTP status is its status — NOT a {status, body} wrapper (an
                # SDK abstraction the transport unwraps). v0.8.0 walk: Teams
                # rejected the wrapper with "Unable to reach app"; the taskInfo
                # must be the top-level body.
                return JSONResponse(resp.body, status_code=resp.status)

            # Slice 19i — dedupe (conversationId, activityId).
            delivery_id = bridge._activity_delivery_id(activity)
            if delivery_id is not None and self._idempotency_cache.is_duplicate(
                delivery_id
            ):
                return JSONResponse({"status": "duplicate"})

            # #77 — a card Action.Submit arrives as a ``message`` with ``value``
            # (our ``hermes_kind`` tag) and no user text. It is an approval/
            # clarify control signal, not a user turn: route it to the gateway
            # resolver and ack, never dispatch it to the agent loop. After the
            # dedupe so a BF retry is dropped (a re-resolve is harmless but
            # wasteful).
            card_action = self._extract_card_action(activity)
            if card_action is not None:
                return await self._handle_card_action(activity, card_action)

            # #79 — BF lifecycle activities (install add/remove,
            # membersAdded) are channel-control, not user turns, but they
            # carry the conversation reference we need for proactive
            # delivery. Capture it on add (so #33/#67 proactive can reach a
            # chat the operator installed the agent into but nobody has
            # messaged), and evict on uninstall (so we stop POSTing into a
            # conversation the tenant removed us from, rather than waiting
            # out the 30-day prune). We deliberately do NOT add to
            # ``_seen_inbounds_this_lifetime``: a lifecycle activity has no
            # user-message activity id, so ``send()`` must route via the
            # proactive ``sendToConversation`` path, never replyToActivity.
            # Out of the agent loop either way — ack-and-bail.
            lifecycle_action = _lifecycle_registry_action(activity)
            if lifecycle_action is not None:
                ref = ConversationRef.from_activity(activity)
                if ref is not None:
                    # L4 (#100, #106 review follow-up): stamp the JWT-validated
                    # path on lifecycle-captured refs too, so a later proactive
                    # mint off this ref binds to the validated path rather than
                    # re-deriving it from the untrusted body. The main dispatch
                    # branch below already stamps it; the lifecycle capture path
                    # (install-then-proactive-without-a-user-message) was missed.
                    ref.validated_path = validated_path
                    if lifecycle_action == "evict":
                        # L3 (#105): tear down live stream/coalesced state +
                        # cancel watchdogs first, so no debounce task fires a
                        # doomed POST after the tenant uninstalled us. Runs
                        # regardless of whether the registry held the ref.
                        self._teardown_chat_state(ref.conversation_id)
                        self._seen_inbounds_this_lifetime.discard(
                            ref.conversation_id
                        )
                        if self._conversations.evict(ref.conversation_id):
                            await self._persist_conversations()
                    else:  # "upsert" — capture-if-missing only.
                        # A lifecycle activity has no replyToActivity-able
                        # id and no agentic ids, so it must NEVER overwrite a
                        # richer captured user-message ref: doing so would
                        # corrupt the cached reply target (send() would
                        # replyToActivity against a non-message id) and
                        # downgrade the proactive path (Path A -> B/unknown).
                        # Only create a new entry, or fill one that has no
                        # usable raw. A subsequent real user message refreshes
                        # the entry via the normal dispatch path below.
                        existing = self._conversations.get(ref.conversation_id)
                        if existing is None or not existing.raw:
                            self._conversations.upsert(ref)
                            # M11 (#105): lifecycle capture is a growth path too
                            # — bound it, or repeated installs grow the registry
                            # past the cap without ever hitting the dispatch
                            # branch.
                            self._enforce_registry_cap(ref.conversation_id)
                            await self._persist_conversations()
                logger.info(
                    "inbound lifecycle type=%s action=%s conv=%s",
                    str(activity.get("type") or ""),
                    lifecycle_action,
                    ref.conversation_id if ref is not None else "?(no conv.id)",
                )
                return JSONResponse({"status": "acked", "lifecycle": lifecycle_action})

            # Slice 19q — channel-control + synthetic agents-channel
            # probes ack-and-bail before the registry upsert. They're
            # transient or aren't user messages, so persisting them in
            # the registry would just churn ``last_inbound_activity_id``.
            if not _should_dispatch(activity):
                return JSONResponse({"status": "acked"})

            # Slice 19o — upsert into the durable registry. ``send()``,
            # ``send_typing()``, and ``send_image()`` all look up by
            # ``conversation.id`` here.
            ref = ConversationRef.from_activity(activity)
            if ref is not None:
                # L4 (#100): stamp the validated path so later decoupled mints
                # off this ref bind to it, not the body.
                ref.validated_path = validated_path
                self._conversations.upsert(ref)
                # Slice 19x-e (#27): record that this gateway lifetime
                # has captured an inbound for this chat. Drives the
                # send() gate that picks replyToActivity vs
                # sendToConversation. Per-lifetime, not persisted.
                seen = self._seen_inbounds_this_lifetime
                seen.add(ref.conversation_id)
                # L2 (#105): bound the set, but NEVER evict the chat we just
                # received — the send() gate above routes reply-vs-proactive
                # off its membership, so evicting it would misroute THIS turn's
                # response to sendToConversation. Trim arbitrary OTHER entries
                # down to the cap (set.pop() can't hit a discarded key).
                if len(seen) > _MAX_SEEN_INBOUNDS:
                    seen.discard(ref.conversation_id)
                    # ``and seen`` guards the degenerate cap<=0 case (would
                    # otherwise pop() an empty set → KeyError).
                    while len(seen) >= _MAX_SEEN_INBOUNDS and seen:
                        seen.pop()
                    seen.add(ref.conversation_id)
                # M11 (#105): bound the registry on the hot growth path.
                self._enforce_registry_cap(ref.conversation_id)
                await self._persist_conversations()

            # Build event + dispatch through Hermes' loop.
            # #76 — download any inbound attachments (images/files) into the
            # media cache so the agent's auto-vision / document path sees them.
            media = await self._extract_inbound_media(
                activity, validated_path=validated_path
            )
            event = self._activity_to_event(activity, media=media)
            await self.handle_message(event)
            # M11 (#105): `handle_message` spawns the real turn as a base
            # background task and returns — the turn (incl. any approval/clarify
            # suspend) outlives this call. Record session_key → conversation_id
            # so `_active_conversation_ids()` can see this conversation as
            # in-flight (via the base's live `_active_sessions`) for the WHOLE
            # turn and keep the registry cap/prune from evicting its reply
            # target underneath it.
            if ref is not None:
                sk = self._session_key_for(event)
                if sk is not None:
                    self._session_key_to_conv[sk] = ref.conversation_id
            return JSONResponse({"status": "dispatched"})

        return app

    # ── #76 inbound file/media ────────────────────────────────────────────

    def _media_cache_dir(self) -> Path:
        """#76: platform media cache — ``{HERMES_HOME}/platforms/agent365/media``.
        Absolute paths under here go into ``MessageEvent.media_urls``; the gateway
        reads them for auto-vision / documents (matches the whatsapp_cloud
        adapter's per-platform cache convention)."""
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(home) / "platforms" / "agent365" / "media"

    async def _reply_bearer(
        self, activity: dict[str, Any], validated_path: str | None
    ) -> str | None:
        """Mint the reply bearer, reused to download an inbound image's
        ``contentUrl`` (Teams serves inline-image content with the bot's
        Connector token). Returns None on any failure. The token audience for
        attachment download is walk-validated at #89."""
        if self._http_client is None or self._bridge_cfg is None:
            return None
        try:
            bridge = _import_bridge()
            token, _p = await bridge.acquire_reply_token(
                client=self._http_client,
                cfg=self._bridge_cfg,
                activity=activity,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                bf_cache=self._bf_token_cache,
                validated_path=validated_path,
            )
            return token
        except Exception as e:
            logger.warning("agent365 inbound-media token mint failed: %s", e)
            return None

    async def _download_inbound_media(
        self, url: str, *, headers: dict[str, str] | None, cache_name: str
    ) -> str | None:
        """Download one inbound attachment into the media cache; return its local
        path or None. Best-effort (a failed download is logged + skipped so the
        text still dispatches) and size-capped. ``cache_name`` must be
        caller-sanitised — it is joined onto the cache dir."""
        if self._http_client is None:
            return None
        cap = _MAX_INBOUND_MEDIA_BYTES
        try:
            # R2-P1: stream + bound. Never buffer resp.content (an allowed endpoint
            # could return an arbitrarily large body → memory exhaustion). Reject an
            # oversized Content-Length up front, then read incrementally and abort
            # once we exceed the cap — without consuming the rest of the body.
            # Review-F1: follow_redirects=False + explicit 2xx so a 3xx can't
            # re-target the (bearer-bearing) request past the validated URL.
            async with self._http_client.stream(
                "GET", url, headers=headers or {}, timeout=30.0, follow_redirects=False
            ) as resp:
                status_code = int(getattr(resp, "status_code", 0) or 0)
                if status_code < 200 or status_code >= 300:
                    logger.warning(
                        "agent365 inbound media non-2xx (%s) for %.60s", status_code, url
                    )
                    return None
                clen = resp.headers.get("Content-Length") if resp.headers else None
                if clen is not None:
                    try:
                        if int(clen) > cap:
                            logger.warning(
                                "agent365 inbound media Content-Length %s over cap; "
                                "dropped", clen
                            )
                            return None
                    except ValueError:
                        pass  # unparseable — fall through to the streamed bound
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > cap:
                        logger.warning(
                            "agent365 inbound media over %d-byte cap while streaming; "
                            "aborted", cap
                        )
                        return None
                data = bytes(buf)
        except Exception as e:
            logger.warning("agent365 inbound media download failed (%.60s): %s", url, e)
            return None
        try:
            cache_dir = self._media_cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            out = cache_dir / cache_name
            out.write_bytes(data)
        except OSError as e:
            logger.warning("agent365 inbound media cache write failed: %s", e)
            return None
        return str(out)

    async def _extract_inbound_media(
        self, activity: dict[str, Any], *, validated_path: str | None
    ) -> tuple[list[str], list[str], MessageType]:
        """#76(a/b): download Teams inbound attachments into the media cache so
        the agent's auto-vision (images) / document path sees them. Returns
        ``(media_urls, media_types, message_type)``. Best-effort; a failed
        attachment is skipped. No-op when there are no attachments (the common
        text turn, and every Copilot Chat turn — files are unsupported on CC)."""
        attachments = activity.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return [], [], MessageType.TEXT
        media_urls: list[str] = []
        media_types: list[str] = []
        saw_image = saw_file = False
        # Collision-free, path-traversal-safe base from the (validated) activity
        # id — never interpolate the user-supplied file name into the path.
        base = re.sub(r"[^A-Za-z0-9._-]", "_", str(activity.get("id") or "att"))[:64]
        # Review-F1: the connector allowlist gates where the reply bearer may be
        # sent (image contentUrls). Prefer the operator-configured suffixes; fall
        # back to the bridge default so validation is never silently disabled.
        bridge = _import_bridge()
        connector_suffixes = (
            self._bridge_cfg.trusted_service_url_suffixes
            if self._bridge_cfg is not None
            else bridge.DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES
        )
        image_bearer: str | None = None  # minted once, lazily, for image contentUrls
        for i, att in enumerate(attachments):
            if not isinstance(att, dict):
                continue
            ctype = str(att.get("contentType") or "")
            if ctype.startswith("image/"):
                url = att.get("contentUrl")
                if not isinstance(url, str) or not url or url.startswith("data:"):
                    continue  # inline data: URIs carry bytes directly — deferred
                # Review-F1: never mint/send the reply bearer to a body-supplied
                # host that isn't the Bot Framework connector (SSRF / bearer exfil).
                if not _is_safe_fetch_url(url, connector_suffixes):
                    logger.warning(
                        "agent365 inbound image contentUrl off connector allowlist; "
                        "skipped (%.60s)", url
                    )
                    continue
                if image_bearer is None:
                    image_bearer = await self._reply_bearer(activity, validated_path)
                if image_bearer is None:
                    continue
                ext = mimetypes.guess_extension(ctype.split(";", 1)[0]) or ".bin"
                path = await self._download_inbound_media(
                    url,
                    headers={"Authorization": f"Bearer {image_bearer}"},
                    cache_name=f"{base}_{i}{ext}",
                )
                if path is not None:
                    media_urls.append(path)
                    media_types.append(ctype)
                    saw_image = True
            elif ctype == _TEAMS_FILE_DOWNLOAD_INFO:
                content = att.get("content")
                content = content if isinstance(content, dict) else {}
                url = content.get("downloadUrl")
                if not isinstance(url, str) or not url:
                    continue
                # downloadUrl is a pre-authenticated SharePoint/OneDrive link.
                # R2-P1: pin it to the configured tenant hosts (exact match) before
                # the GET — a `*.sharepoint.com` suffix would accept another
                # customer's tenant. Empty allowlist ⇒ fail-closed.
                if not _is_allowed_file_host(url, self._file_host_allowlist):
                    logger.warning(
                        "agent365 inbound file downloadUrl not on the configured "
                        "tenant host allowlist; skipped (%.60s)", url
                    )
                    continue
                file_type = str(content.get("fileType") or "").lower()
                ext = f".{file_type}" if file_type and file_type.isalnum() else ".bin"
                path = await self._download_inbound_media(
                    url, headers=None, cache_name=f"{base}_{i}{ext}"
                )
                if path is not None:
                    media_urls.append(path)
                    media_types.append(_TEAMS_FILE_DOWNLOAD_INFO)
                    saw_file = True
        message_type = (
            MessageType.PHOTO
            if saw_image
            else MessageType.DOCUMENT
            if saw_file
            else MessageType.TEXT
        )
        return media_urls, media_types, message_type

    def _activity_to_event(
        self,
        activity: dict[str, Any],
        media: tuple[list[str], list[str], MessageType] | None = None,
    ) -> MessageEvent:
        conv = activity.get("conversation") or {}
        sender = activity.get("from") or {}
        recipient = activity.get("recipient")
        recipient_id = (
            str(recipient.get("id") or "") if isinstance(recipient, dict) else ""
        )
        text = _strip_recipient_mention(
            str(activity.get("text") or ""),
            activity.get("entities"),
            recipient_id,
        )
        chat_id = str(conv.get("id") or "")
        # BF conversation.conversationType: "personal" / "groupChat" / "channel"
        conv_type = str(conv.get("conversationType") or "personal")
        chat_type = "dm" if conv_type == "personal" else (
            "group" if conv_type == "groupChat" else "channel"
        )
        source = SessionSource(
            platform=self.platform,
            chat_id=chat_id,
            chat_name=chat_id,  # 19o replaces with the resolved display name
            chat_type=chat_type,
            user_id=str(sender.get("id") or ""),
            user_name=str(sender.get("name") or ""),
            message_id=str(activity.get("id") or ""),
        )
        # #76 — media downloaded by the async _extract_inbound_media step; the
        # message_type reflects the attachment kind (PHOTO/DOCUMENT) or TEXT.
        media_urls, media_types, media_message_type = media or ([], [], MessageType.TEXT)
        return MessageEvent(
            text=text,
            message_type=media_message_type,
            source=source,
            raw_message=activity,
            message_id=str(activity.get("id") or ""),
            timestamp=datetime.now(),
            media_urls=media_urls,
            media_types=media_types,
        )

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Build the bridge runtime + start uvicorn on `self.port`.

        ``is_reconnect`` is part of the ``BasePlatformAdapter.connect``
        contract — the gateway's reconnection watcher forwards it
        (``gateway/run.py`` calls ``adapter.connect(is_reconnect=...)``).
        The a365 adapter rebuilds its runtime the same way on a fresh
        connect or a reconnect (``close()`` tears down the prior uvicorn
        task + http client, so a reconnect starts clean), so the flag is
        accepted for contract compatibility. Without this parameter the
        gateway raises ``unexpected keyword argument 'is_reconnect'`` and
        the platform never connects.
        """
        bridge = _import_bridge()
        try:
            import httpx
            import uvicorn
        except ImportError as e:
            logger.error("agent365 adapter missing extras: %s", e)
            self._set_fatal_error("missing_extras", str(e), retryable=False)
            return False

        try:
            self._bridge_cfg = self._make_bridge_config()
        except RuntimeError as e:
            logger.error("agent365 adapter config error: %s", e)
            self._set_fatal_error("config_error", str(e), retryable=False)
            return False

        if self._http_client is None:
            self._http_client = httpx.AsyncClient()
        if self._fmi_cache is None:
            self._fmi_cache = bridge._FmiCache()
        if self._user_cache is None:
            self._user_cache = bridge._UserTokenCache()
        if self._bf_token_cache is None:
            self._bf_token_cache = bridge._BfTokenCache()

        if self._app is None:
            self._app = self.build_app()

        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="info",
            access_log=False,
            lifespan="on",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._uvicorn_task = asyncio.create_task(self._uvicorn_server.serve())

        # Wait for uvicorn to flip its ``started`` flag before we
        # report ready — otherwise the gateway's status check could
        # race the bind.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if getattr(self._uvicorn_server, "started", False):
                break
            if self._uvicorn_task.done():
                exc = self._uvicorn_task.exception()
                logger.error("agent365 uvicorn died during startup: %s", exc)
                self._set_fatal_error(
                    "uvicorn_startup_failed",
                    str(exc) if exc else "unknown",
                    retryable=True,
                )
                return False
            await asyncio.sleep(0.05)
        else:
            logger.error("agent365 uvicorn did not start within 10s")
            self._set_fatal_error(
                "uvicorn_startup_timeout",
                "uvicorn did not flip started=True within 10s",
                retryable=True,
            )
            return False

        self._mark_connected()
        logger.info(
            "agent365 adapter listening on http://%s:%s/api/messages",
            self.host,
            self.port,
        )
        return True

    async def disconnect(self) -> None:
        import contextlib

        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._uvicorn_task is not None:
            try:
                await asyncio.wait_for(self._uvicorn_task, timeout=10.0)
            except (TimeoutError, asyncio.CancelledError) as e:
                logger.warning("agent365 uvicorn shutdown noise: %s", e)
            except Exception as e:
                logger.warning("agent365 uvicorn shutdown noise: %s", e)
            self._uvicorn_task = None
            self._uvicorn_server = None
        if self._http_client is not None:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()
            self._http_client = None
        for task in list(self._coalesced_reply_tasks.values()):
            task.cancel()
        self._coalesced_reply_tasks.clear()
        self._coalesced_replies.clear()
        self._active_coalesced_reply_by_chat.clear()
        for task in list(self._coalesced_status_tasks.values()):
            task.cancel()
        self._coalesced_status_tasks.clear()
        self._coalesced_status.clear()
        # #73(c)/#82 — drop per-lifetime correlator maps on disconnect.
        self._feedback_by_message_id.clear()
        self._handoff_tokens.clear()
        # Review-F3 — drop pending file-consent offers on disconnect too.
        self._pending_file_uploads.clear()
        self._mark_disconnected()

    # ── Outbound ──────────────────────────────────────────────────────────

    def _session_key_for(self, event: Any) -> str | None:
        """Compute ``event``'s base session key exactly as
        ``BasePlatformAdapter.handle_message`` does, so this conversation can
        be found in the base's ``_active_sessions`` (which is session-key
        keyed). Returns ``None`` if it can't be computed — the caller then
        falls back to recency-only protection (#105/M11)."""
        try:
            extra = getattr(self.config, "extra", {}) or {}
            return build_session_key(
                event.source,
                group_sessions_per_user=extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=extra.get(
                    "thread_sessions_per_user", False
                ),
            )
        except Exception:
            return None

    def _active_conversation_ids(self) -> set[str]:
        """Conversation ids whose Hermes turn is currently in flight — the set
        the registry cap/prune must not evict (#105/M11).

        The base's ``_active_sessions`` guard spans the whole background turn,
        including a turn suspended awaiting a human approval/clarify, but is
        keyed by session key. Bridge it to the registry's conversation-id space
        via ``_session_key_to_conv`` (recorded at dispatch), self-cleaning any
        mapping whose session has since ended."""
        active_sks = set(getattr(self, "_active_sessions", {}) or {})
        for sk in [k for k in self._session_key_to_conv if k not in active_sks]:
            self._session_key_to_conv.pop(sk, None)
        return set(self._session_key_to_conv.values())

    def _enforce_registry_cap(self, current_conversation_id: str) -> None:
        """M11 (#105): bound the durable registry after a growth-capable
        upsert. LRU-evict down to ``_MAX_REGISTRY_ENTRIES``, never dropping a
        pinned entry, a conversation whose Hermes turn is in flight, or the
        conversation just upserted (``current_conversation_id`` — this turn's
        reply target). Shared by the dispatch AND lifecycle-capture upserts so
        every growth path is bounded."""
        self._conversations.enforce_cap(
            _MAX_REGISTRY_ENTRIES,
            active_conversation_ids=(
                self._active_conversation_ids() | {current_conversation_id}
            ),
        )

    async def _persist_conversations(self) -> None:
        """Best-effort save of the registry, OFF the event loop (#105/M11).

        The registry snapshot (``to_payload``) is built on the loop thread —
        so ``_by_id`` is never mutated mid-iteration — then the blocking
        serialize + fsync + os.replace runs in the default executor. This
        keeps a large save (~0.6s at 20k entries in the pre-M11 shape) from
        stalling inbound processing. Failures are logged, never raised.

        The locked write is ``asyncio.shield``-ed: cancelling the caller (e.g.
        on shutdown) must NOT release ``_persist_lock`` while the executor
        thread is still writing — otherwise a newer save could acquire the
        lock and ``os.replace`` first, then this older worker overwrites it
        (#105/M11). Shield lets the inner locked write run to completion,
        holding the lock, even when this coroutine is cancelled."""
        await asyncio.shield(self._locked_persist())

    async def _locked_persist(self) -> None:
        """The serialized snapshot+write half of :meth:`_persist_conversations`
        — held under ``_persist_lock`` for its whole duration so saves land in
        order. Run only via the shielded wrapper above."""
        async with self._persist_lock:
            payload = self._conversations.to_payload()
            path = self._conversations_path
            registry_cls = type(self._conversations)
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, registry_cls.write_payload, path, payload
                )
            except OSError as e:
                logger.warning(
                    "agent365 conversations: save failed for %s: %s",
                    path,
                    e,
                )

    async def prune_conversations(self) -> int:
        """Slice 19x-d (#4): drop stale ConversationRegistry entries.

        Mirrors Hermes' ``SessionStore.prune_old_entries`` shape —
        skips entries that are operator-pinned, have a Hermes turn in flight
        (``_active_conversation_ids``), or have no ``last_used_at`` stamp.
        Threshold is ``extra.conversations_prune_max_age_days`` (default 30).

        Operators wire this from cron (no built-in periodic loop;
        keeping it one-shot avoids adding a maintenance-task pattern
        the gateway doesn't otherwise use). Saves to disk if anything
        dropped.

        Returns the number of entries removed.
        """
        # #105: pass the in-flight set in the registry's conversation-id space
        # (base `_active_sessions` is keyed by prefixed session keys, which the
        # registry's bare-conversation_id comparison never matches — a
        # long-standing no-op the M11 work corrected).
        active_keys = self._active_conversation_ids()
        dropped = self._conversations.prune_old_entries(
            max_age_days=self._conversations_prune_max_age_days,
            active_session_keys=active_keys,
        )
        if dropped > 0:
            await self._persist_conversations()
            logger.info(
                "agent365 prune_conversations: dropped %d stale entry(ies); "
                "%d remain.",
                dropped,
                len(self._conversations),
            )
        return dropped

    def _cached_inbound_for(self, chat_id: str) -> dict[str, Any] | None:
        """Return the most recent inbound activity for ``chat_id``,
        sourced from the registry's ``raw`` field. Slice 19o's
        registry is the only authoritative source; legacy callers
        should not reach into ``_chat_contexts`` (gone)."""
        ref = self._conversations.get(chat_id)
        if ref is None or not ref.raw:
            return None
        return ref.raw

    def _validated_path_for(self, chat_id: str) -> str | None:
        """L4 (#100): the JWT-validated inbound path ("A"/"B") captured on the
        ConversationRef, used to bind decoupled outbound mints instead of
        re-deriving the path from the untrusted body. None for legacy/lifecycle
        refs → the mint site passes it through and ``acquire_reply_token`` falls
        back to body-derived routing."""
        ref = self._conversations.get(chat_id)
        return ref.validated_path if ref is not None else None

    def _validated_path_for_inbound(self, inbound: dict[str, Any]) -> str | None:
        """L4 (#100): validated path for a cached inbound the mint site holds as a
        raw dict (rather than a chat_id) — keyed by its conversation id."""
        conv = inbound.get("conversation")
        conv = conv if isinstance(conv, dict) else {}
        chat_id = conv.get("id")
        return self._validated_path_for(str(chat_id)) if chat_id else None

    def _build_proactive_target_spec(self, chat_id: str) -> dict[str, Any] | None:
        """Slice 19x-a (#4): pure-function read over the registry.

        Returns the minimal target spec needed to construct an outbound
        Activity + mint the outbound token chain for a chat the gateway
        hasn't necessarily seen this lifetime. Returns ``None`` when the
        registry has no entry for ``chat_id``.

        Shape::

            {
                "service_url": str,
                "conversation_id": str,
                "channel_id": str,           # default "msteams" if missing
                "chat_type": str,             # personal / groupChat / channel
                "tenant_id": str,
                "agentic_app_id": str,        # empty when not a Path A inbound
                "agentic_user_id": str,       # empty when not a Path A inbound
                "from": dict,                 # outbound sender (= inbound recipient)
                "recipient": dict,            # outbound recipient (= inbound sender)
                "path": "A" | "B" | "unknown",  # convenience tag for callers
            }

        Path-tagging rule (refined #33):

        - **Path A** when the cached inbound's ``recipient`` carries
          both ``agenticAppId`` and ``agenticUserId`` (the Microsoft
          A365 agentic-user routing signal).
        - **Path B** when those fields are absent AND the cached
          ``serviceUrl`` has a host suffix matching a classic Bot
          Framework destination (``.botframework.com`` /
          ``.trafficmanager.net``). Slice 20e (#33) shipped the BF
          S2S outbound token mint so these inbounds can now reply via
          ``acquire_reply_token``'s Path B branch.
        - **unknown** otherwise — callers raise rather than guess.

        Pure: no network, no token minting, no state mutation. Safe
        to call from sync contexts.
        """
        ref = self._conversations.get(chat_id)
        if ref is None or not ref.raw:
            return None

        raw = ref.raw
        recipient_inbound = raw.get("recipient") if isinstance(raw.get("recipient"), dict) else {}
        sender_inbound = raw.get("from") if isinstance(raw.get("from"), dict) else {}
        conversation = raw.get("conversation") if isinstance(raw.get("conversation"), dict) else {}

        agentic_app_id = str(recipient_inbound.get("agenticAppId") or "")
        agentic_user_id = str(recipient_inbound.get("agenticUserId") or "")
        tenant_id = (
            str(recipient_inbound.get("tenantId") or "")
            or str(conversation.get("tenantId") or "")
            or (ref.tenant_id or "")
        )
        bridge = _import_bridge()
        path_tag = bridge._inbound_path_tag(
            {
                "recipient": recipient_inbound,
                "conversation": conversation,
                "serviceUrl": ref.service_url,
            }
        )

        return {
            "service_url": ref.service_url,
            "conversation_id": ref.conversation_id,
            "channel_id": str(raw.get("channelId") or "msteams"),
            "chat_type": ref.chat_type,
            "tenant_id": tenant_id,
            "agentic_app_id": agentic_app_id,
            "agentic_user_id": agentic_user_id,
            # In a reply, the inbound's recipient becomes the outbound
            # sender (the agentic user identity) and vice-versa.
            "from": dict(recipient_inbound),
            "recipient": dict(sender_inbound),
            "path": path_tag,
            # L4 (#100): the JWT-validated path captured at inbound time (None for
            # legacy/lifecycle refs). Callers pass this to acquire_reply_token so
            # the mint binds to it rather than the body-derived `path` above.
            "validated_path": ref.validated_path,
        }

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Render `content` as a reply Activity and POST via serviceUrl.

        Looks up the most recent inbound activity for ``chat_id`` in the
        durable registry (slice 19o). Routing decision (slice 19x-e, #27):

        - **This gateway lifetime captured an inbound for ``chat_id``** →
          use ``replyToActivity`` against the cached inbound's
          ``activity_id``. This is the steady-state Hermes reply flow.
        - **Otherwise** (gateway restarted since last inbound, or
          cron-driven send for a chat the registry knows about but
          this lifetime hasn't seen) → fall through to
          ``_send_proactive`` which posts a non-reply Activity via the
          ``sendToConversation`` BF endpoint. Avoids stale-activity-id
          rejections from BF channels.

        Path A proactive is live-walked. Path B proactive code shipped
        in #33 and uses the BF S2S branch of ``acquire_reply_token``;
        its end-to-end ``sendToConversation`` round trip is tracked
        separately for live validation.

        Slice 19s-bis: in personal chats with no active stream for the
        conversation, ``send()`` emits a BF streaming-start activity
        (typing + streaminfo + streamSequence:1) and captures the
        returned ``streamId``. Subsequent ``edit_message`` calls
        continue that same stream rather than creating a separate one.
        This satisfies Microsoft's "one streaming sequence per user
        turn" rule (custom-engine-agents doc) and gives a single
        growing bubble per Hermes segment.

        #54 branch-walk correction: Copilot Chat arrives as
        ``groupChat`` and does not visibly render BF streaming
        activities. For non-personal chats, a stream-consumer first
        chunk (``reply_to`` present) starts a local coalescing buffer
        instead of POSTing immediately. ``edit_message(finalize=True)``
        later emits one normal ``send_reply`` with the final text.

        Suppress one-shot non-streaming sends while a stream is active;
        Copilot Chat renders interleaved progress/fallback activities
        as separate bubbles. A new streaming first chunk must first
        finalize the prior stream successfully before opening another.

        Fallback to a non-streaming ``message`` activity when the
        streaming-start POST itself fails.
        """
        # Slice 19x-e (#27): the gate is "did this lifetime capture an
        # inbound for chat_id", not "is the registry populated". The
        # registry's raw persists across restarts (slice 19o), so the
        # earlier ``_cached_inbound_for is None`` check never fired in
        # production — every send took the cached-inbound path with a
        # potentially stale activity_id.
        if chat_id not in self._seen_inbounds_this_lifetime:
            return await self._send_proactive(chat_id, content)

        inbound = self._cached_inbound_for(chat_id)
        if not inbound:
            # Defensive fallback: lifetime set says we saw an inbound,
            # but the registry doesn't have raw. Should be unreachable
            # under normal flow (capture writes both atomically); treat
            # like a fresh-lifetime call and route via proactive.
            return await self._send_proactive(chat_id, content)

        # Slice 19x-d (#4): bump the registry's last_used_at so prune
        # honours actively-driven chats even when no fresh inbound has
        # arrived recently (e.g. operator-driven outbound only).
        self._conversations.mark_used(chat_id)

        if self._http_client is None or self._bridge_cfg is None:
            msg = "agent365 send: adapter not connected"
            logger.error(msg)
            return SendResult(success=False, error=msg)

        # Slice 19s-bis: try streaming-start only when in a streaming
        # context. ``reply_to`` is the signal — Hermes' stream consumer's
        # first-chunk-send passes ``reply_to=event_message_id`` (the
        # inbound activity id, see ``stream_consumer.py:1233``).
        # Commentary (interim_assistant_messages), tool-progress, and
        # ``base.py:_send_with_retry`` all default to ``reply_to=None`` —
        # those are one-shot messages, not streams. Starting a BF stream
        # for them creates a "typing" activity that never closes,
        # leaving the user's surface stuck in the streaming indicator
        # until Microsoft's 2-min cap fires.
        conv = inbound.get("conversation") or {}
        chat_type = str(conv.get("conversationType") or "")

        # Slice 19s-bis follow-up — Hermes' stream consumer occasionally
        # starts a fresh segment (segment break, interim_assistant_messages,
        # commentary handoff) without first calling
        # ``edit_message(finalize=True)`` on the previous stream's
        # message_id. The old stream stays open in our state and as a
        # typing indicator on the user's surface until BF's 2-min cap
        # fires.
        #
        # #54 / CEA ordering rule: do not interleave non-streaming
        # progress/fallback messages into an active stream. Copilot Chat
        # renders those as additional bubbles. Only a new streaming
        # first-chunk (reply_to != None) may force-finalize the old
        # stream, and the next activity is allowed only if finalization
        # succeeded.
        if chat_id in self._active_stream_by_chat:
            stale_msg_id = self._active_stream_by_chat[chat_id]
            if reply_to is None:
                logger.info(
                    "agent365 send suppressed while stream active: "
                    "chat_id=%s active_message_id=%s",
                    chat_id,
                    stale_msg_id,
                )
                return SendResult(success=True, message_id=str(stale_msg_id))
            finalized = await self._auto_finalize_stale_stream(
                chat_id=chat_id, message_id=stale_msg_id, inbound=inbound,
            )
            if not finalized and chat_id in self._active_stream_by_chat:
                return SendResult(
                    success=False,
                    error="active stream still open; suppressed next send",
                )

        if chat_id in self._active_coalesced_reply_by_chat:
            active_msg_id = self._active_coalesced_reply_by_chat[chat_id]
            if reply_to is None:
                logger.info(
                    "agent365 send suppressed while coalesced reply active: "
                    "chat_id=%s active_message_id=%s",
                    chat_id,
                    active_msg_id,
                )
                return SendResult(success=True, message_id=active_msg_id)
            return self._buffer_coalesced_reply(
                chat_id=chat_id,
                content=content,
                message_id=active_msg_id,
                inbound=inbound,
            )

        if (
            chat_type == "personal"
            and reply_to is not None
            and chat_id not in self._active_stream_by_chat
        ):
            stream_result = await self._send_stream_start(
                chat_id=chat_id, content=content, inbound=inbound
            )
            if stream_result is not None:
                return stream_result
            # Stream start failed (logged inside _send_stream_start);
            # fall through to non-streaming reply.

        if chat_type != "personal" and reply_to is not None:
            message_id = self._coalesced_reply_message_id(chat_id, reply_to)
            return self._buffer_coalesced_reply(
                chat_id=chat_id,
                content=content,
                message_id=message_id,
                inbound=inbound,
            )

        return await self._send_reply_activity(
            inbound=inbound,
            content=content,
            log_context="send",
            # #73(b): the agent surfaces sources under metadata["citations"];
            # render_reply_activity converts them to Teams citation entities.
            citations=(metadata or {}).get("citations"),
        )

    async def _send_reply_activity(
        self,
        *,
        inbound: dict[str, Any],
        content: str,
        log_context: str,
        citations: Any = None,
    ) -> SendResult:
        bridge = _import_bridge()
        webhook_response: dict[str, Any] = {"text": content}
        # #73(b) citations (metadata-driven) + #73(c) feedback loop (env-gated).
        if citations:
            webhook_response["citations"] = citations
        if self._feedback_enabled:
            webhook_response["feedback"] = True
        reply = bridge.render_reply_activity(inbound, webhook_response)
        try:
            await bridge.send_reply(
                inbound=inbound,
                reply=reply,
                cfg=self._bridge_cfg,
                client=self._http_client,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                validated_path=self._validated_path_for_inbound(inbound),  # L4 (#100)
            )
        except Exception as e:
            logger.error("agent365 %s send_reply failed: %s", log_context, e)
            return SendResult(success=False, error=str(e))
        return SendResult(success=True, message_id=str(inbound.get("id") or ""))

    @staticmethod
    def _coalesced_reply_message_id(chat_id: str, reply_to: Any) -> str:
        return f"coalesced:{chat_id}:{reply_to}"

    def _buffer_coalesced_reply(
        self,
        *,
        chat_id: str,
        content: str,
        message_id: str,
        inbound: dict[str, Any],
    ) -> SendResult:
        loop = asyncio.get_event_loop()
        now = loop.time()
        state = self._coalesced_replies.get(message_id)
        if state is None:
            state = {
                "chat_id": chat_id,
                "content": "",
                "inbound": inbound,
                "opened_ts": now,
                "last_update_ts": now,
            }
            self._coalesced_replies[message_id] = state
        else:
            state["inbound"] = inbound
            state["last_update_ts"] = now
        state["content"] = _strip_streaming_cursor(content)
        self._active_coalesced_reply_by_chat[chat_id] = message_id
        self._ensure_coalesced_reply_task(message_id)
        return SendResult(success=True, message_id=message_id)

    async def _edit_coalesced_reply(
        self,
        *,
        chat_id: str,
        message_id: str,
        content: str,
        finalize: bool,
        inbound: dict[str, Any],
        loop_now: float,
    ) -> SendResult:
        active_msg_id = self._active_coalesced_reply_by_chat.get(chat_id)
        if active_msg_id and active_msg_id != message_id:
            logger.info(
                "agent365 edit_message continuing coalesced reply: "
                "chat_id=%s requested_message_id=%s active_message_id=%s "
                "finalize=%s",
                chat_id,
                message_id,
                active_msg_id,
                finalize,
            )
            message_id = active_msg_id

        if message_id not in self._coalesced_replies:
            self._buffer_coalesced_reply(
                chat_id=chat_id,
                content=content,
                message_id=message_id,
                inbound=inbound,
            )
        else:
            self._coalesced_replies[message_id]["content"] = (
                _strip_streaming_cursor(content)
            )
            self._coalesced_replies[message_id]["inbound"] = inbound
            self._coalesced_replies[message_id]["last_update_ts"] = loop_now
            self._ensure_coalesced_reply_task(message_id)

        if not finalize:
            return SendResult(success=True, message_id=message_id)

        if self._http_client is None or self._bridge_cfg is None:
            return SendResult(
                success=False,
                error="agent365 edit_message: adapter not connected",
            )

        state = self._coalesced_replies.get(message_id) or {}
        final_content = str(state.get("content") or "")
        # #82 — the coalesced path is the "degraded-from-stream" surface for
        # every non-personal chat (Copilot Chat AND genuine Teams groups are
        # wire-indistinguishable here). Offer a "continue in Teams" link
        # (policy-gated, off by default).
        final_content = self._maybe_append_handoff_link(chat_id, final_content)
        result = await self._send_reply_activity(
            inbound=inbound,
            content=final_content,
            log_context="coalesced edit_message",
        )
        if result.success:
            self._drop_coalesced_reply_state(chat_id, message_id)
            self._recently_finalized[message_id] = loop_now
            return SendResult(success=True, message_id=result.message_id)
        return result

    def _ensure_coalesced_reply_task(self, message_id: str) -> None:
        task = self._coalesced_reply_tasks.get(message_id)
        if task is not None and not task.done():
            return
        self._coalesced_reply_tasks[message_id] = asyncio.create_task(
            self._watch_coalesced_reply(message_id)
        )

    async def _watch_coalesced_reply(self, message_id: str) -> None:
        try:
            while True:
                state = self._coalesced_replies.get(message_id)
                if state is None:
                    return
                loop_now = asyncio.get_event_loop().time()
                last_update_ts = state.get("last_update_ts")
                if not isinstance(last_update_ts, (int, float)):
                    last_update_ts = state.get("opened_ts", loop_now)
                    if not isinstance(last_update_ts, (int, float)):
                        last_update_ts = loop_now
                    state["last_update_ts"] = last_update_ts
                age = loop_now - float(last_update_ts)
                remaining = _COALESCED_REPLY_FLUSH_AFTER_SEC - age
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                await self._flush_stale_coalesced_reply(
                    message_id, cancel_task=False
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "agent365 coalesced reply watchdog failed: "
                "message_id=%s error=%s",
                message_id,
                e,
            )

    async def _flush_stale_coalesced_reply(
        self,
        message_id: str,
        *,
        cancel_task: bool = True,
    ) -> bool:
        state = self._coalesced_replies.get(message_id)
        if state is None:
            return True
        chat_id = str(state.get("chat_id") or "")
        inbound = state.get("inbound")
        content = str(state.get("content") or "")
        if not chat_id or not isinstance(inbound, dict):
            logger.warning(
                "agent365 dropping stale coalesced reply with incomplete state: "
                "message_id=%s chat_id=%s",
                message_id,
                chat_id,
            )
            self._drop_coalesced_reply_state(
                chat_id, message_id, cancel_task=cancel_task
            )
            return False

        if self._http_client is None or self._bridge_cfg is None:
            logger.warning(
                "agent365 dropping stale coalesced reply while disconnected: "
                "chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            self._drop_coalesced_reply_state(
                chat_id, message_id, cancel_task=cancel_task
            )
            return False

        result = await self._send_reply_activity(
            inbound=inbound,
            content=content,
            log_context="stale coalesced reply",
        )
        if result.success:
            logger.warning(
                "agent365 auto-flushed stale coalesced reply: "
                "chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            self._drop_coalesced_reply_state(
                chat_id, message_id, cancel_task=cancel_task
            )
            self._recently_finalized[message_id] = asyncio.get_event_loop().time()
            return True

        logger.warning(
            "agent365 dropping stale coalesced reply after flush failure: "
            "chat_id=%s message_id=%s error=%s",
            chat_id,
            message_id,
            result.error,
        )
        self._drop_coalesced_reply_state(
            chat_id, message_id, cancel_task=cancel_task
        )
        return False

    def _drop_coalesced_reply_state(
        self,
        chat_id: str,
        message_id: str,
        *,
        cancel_task: bool = True,
    ) -> None:
        self._coalesced_replies.pop(message_id, None)
        if self._active_coalesced_reply_by_chat.get(chat_id) == message_id:
            self._active_coalesced_reply_by_chat.pop(chat_id, None)
        task = self._coalesced_reply_tasks.pop(message_id, None)
        current = asyncio.current_task()
        if cancel_task and task is not None and task is not current:
            task.cancel()

    # ── Status coalescing (Copilot Chat, #53) ────────────────────────────
    @staticmethod
    def _coalesced_status_key(chat_id: str, status_key: str) -> str:
        return f"status:{chat_id}:{status_key}"

    def _buffer_coalesced_status(
        self,
        *,
        chat_id: str,
        status_key: str,
        content: str,
        inbound: dict[str, Any],
    ) -> SendResult:
        """Append a status line to the per-(chat_id, status_key) buffer and
        (re)arm the debounce watchdog. Distinct lines accumulate so the
        flushed bubble preserves the whole trace; an exact repeat of the
        last line is dropped (retry bookkeeping can fire the same callback
        twice)."""
        key = self._coalesced_status_key(chat_id, status_key)
        now = asyncio.get_event_loop().time()
        line = _strip_streaming_cursor(content).strip()
        state = self._coalesced_status.get(key)
        if state is None:
            state = {
                "chat_id": chat_id,
                "lines": [],
                "inbound": inbound,
                "opened_ts": now,
                "last_update_ts": now,
            }
            self._coalesced_status[key] = state
        else:
            state["inbound"] = inbound
            state["last_update_ts"] = now
        lines = state["lines"]
        if line and (not lines or lines[-1] != line):
            lines.append(line)
        self._ensure_coalesced_status_task(key)
        return SendResult(success=True, message_id=key)

    def _ensure_coalesced_status_task(self, key: str) -> None:
        task = self._coalesced_status_tasks.get(key)
        if task is not None and not task.done():
            return
        self._coalesced_status_tasks[key] = asyncio.create_task(
            self._watch_coalesced_status(key)
        )

    async def _watch_coalesced_status(self, key: str) -> None:
        try:
            while True:
                state = self._coalesced_status.get(key)
                if state is None:
                    return
                loop_now = asyncio.get_event_loop().time()
                last_update_ts = state.get("last_update_ts")
                if not isinstance(last_update_ts, (int, float)):
                    last_update_ts = state.get("opened_ts", loop_now)
                    if not isinstance(last_update_ts, (int, float)):
                        last_update_ts = loop_now
                    state["last_update_ts"] = last_update_ts
                age = loop_now - float(last_update_ts)
                remaining = _STATUS_COALESCE_FLUSH_AFTER_SEC - age
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                await self._flush_coalesced_status(key)
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "agent365 coalesced status watchdog failed: key=%s error=%s",
                key,
                e,
            )

    async def _flush_coalesced_status(self, key: str) -> bool:
        """Emit the accumulated status lines as ONE reply bubble, then drop
        the buffer + watchdog. Returns False (and still drops state) on
        empty/incomplete buffers or a send failure — a stale status notice
        must never wedge the buffer.

        Two windows are guarded explicitly:

        - A turn may have *opened* while the burst was settling (a status
          buffered before any reply/stream existed). Re-check active-turn
          state and suppress the bubble rather than interleaving it into
          the active turn — the entry guard only covers calls that arrive
          *after* the turn opened, so the flush must mirror it.
        - A new same-key line may be *appended* during the ``await`` below
          (the watchdog task is suspended here, so ``_ensure_…`` would not
          re-arm). Status lines accumulate, so a dropped line is lost for
          good; re-arm a fresh watchdog for the trailing remainder instead.
        """
        state = self._coalesced_status.get(key)
        if state is None:
            return True
        chat_id = str(state.get("chat_id") or "")
        inbound = state.get("inbound")
        lines = state.get("lines") or []
        content = "\n".join(str(line) for line in lines).strip()
        if not content or not isinstance(inbound, dict):
            self._drop_coalesced_status_state(key)
            return False
        if self._http_client is None or self._bridge_cfg is None:
            logger.warning(
                "agent365 dropping coalesced status while disconnected: "
                "chat_id=%s key=%s",
                chat_id,
                key,
            )
            self._drop_coalesced_status_state(key)
            return False
        # A reply/stream that opened while this burst settled must suppress
        # the status — never interleave a status bubble into an active turn
        # (mirrors the entry guard in send_or_update_status).
        if (
            chat_id in self._active_stream_by_chat
            or chat_id in self._active_coalesced_reply_by_chat
        ):
            logger.info(
                "agent365 coalesced status suppressed (turn opened during "
                "debounce): chat_id=%s key=%s",
                chat_id,
                key,
            )
            self._drop_coalesced_status_state(key)
            return False
        sent_count = len(lines)
        result = await self._send_reply_activity(
            inbound=inbound,
            content=content,
            log_context="coalesced status",
        )
        if not result.success:
            logger.warning(
                "agent365 coalesced status flush failed: "
                "chat_id=%s key=%s error=%s",
                chat_id,
                key,
                result.error,
            )
        # If new lines were appended during the send (append-during-flush
        # race), the watchdog task we are running in is suspended, so
        # _ensure_coalesced_status_task could not re-arm. Trim the already
        # sent lines and re-arm a fresh watchdog for the remainder so the
        # trailing line is not silently lost.
        live = self._coalesced_status.get(key)
        if live is state and len(state.get("lines") or []) > sent_count:
            del state["lines"][:sent_count]
            state["last_update_ts"] = asyncio.get_event_loop().time()
            self._rearm_coalesced_status_task(key)
            return result.success
        self._drop_coalesced_status_state(key)
        return result.success

    def _rearm_coalesced_status_task(self, key: str) -> None:
        """Force a fresh watchdog for ``key``. Unlike
        ``_ensure_coalesced_status_task`` this does not early-return when the
        current task is still running — it is called from inside the flush
        (the watchdog task is about to return), so the stale entry must be
        replaced rather than reused."""
        self._coalesced_status_tasks[key] = asyncio.create_task(
            self._watch_coalesced_status(key)
        )

    def _drop_coalesced_status_state(self, key: str) -> None:
        self._coalesced_status.pop(key, None)
        task = self._coalesced_status_tasks.pop(key, None)
        current = asyncio.current_task()
        if task is not None and task is not current:
            task.cancel()

    async def _send_proactive(
        self, chat_id: str, content: str
    ) -> SendResult:
        """Slice 19x-b (#4): cron-driven outbound for a chat the gateway
        hasn't seen an inbound for this lifetime.

        Reads the target spec from ``_build_proactive_target_spec``;
        falls cleanly on three conditions:

        - No registry entry → ``no registry entry`` error.
        - Target tagged ``path == "unknown"`` → cannot safely choose
          the Path A user-FIC chain or the Path B BF S2S chain.
        - Adapter not connected (HTTP client / bridge cfg unset) →
          ``adapter not connected`` error.

        Happy path: dispatches token minting through
        ``acquire_reply_token`` against a synthetic activity-shaped
        dict. Path A reads ``recipient`` + ``conversation`` to extract
        the agentic ids; Path B classifies the BF ``serviceUrl`` and
        mints a Bot Framework S2S bearer. Both paths then POST a
        non-reply Activity to
        ``<serviceUrl>/v3/conversations/<conv_id>/activities`` (the
        ``sendToConversation`` BF endpoint — no ``replyToId``, no
        ``/activities/<id>`` suffix). Returns the new activity id from
        the server response when available.
        """
        target = self._build_proactive_target_spec(chat_id)
        if target is None:
            msg = (
                f"no registry entry for chat_id={chat_id!r} — "
                "cannot reach a chat the bridge has never seen"
            )
            logger.error("agent365 proactive send: %s", msg)
            return SendResult(success=False, error=msg)

        # Slice 19x-d (#4): bump last_used_at — proactive sends are
        # exactly the case where outbound traffic should keep the
        # registry entry warm.
        self._conversations.mark_used(chat_id)

        if target["path"] == "unknown":
            msg = (
                "agent365 proactive send: cannot classify target as "
                "Path A or Path B (no agentic identifiers + serviceUrl "
                f"not on the BF host-suffix allowlist). serviceUrl="
                f"{target['service_url']!r}. This usually means the "
                "registry entry pre-dates #33 path-tag refinement; "
                "re-walk an inbound through /api/messages to refresh."
            )
            logger.error(msg)
            return SendResult(success=False, error=msg)

        if self._http_client is None or self._bridge_cfg is None:
            msg = "agent365 proactive send: adapter not connected"
            logger.error(msg)
            return SendResult(success=False, error=msg)

        # Synthetic activity-shape for the dispatcher. For Path A the
        # dispatcher reads recipient.agenticAppId/agenticUserId/tenantId;
        # for Path B it reads serviceUrl host suffix. Include both so
        # the dispatcher can route without re-walking the registry.
        token_input = {
            "recipient": dict(target["from"]),  # outbound sender = agentic identity
            "conversation": {
                "id": target["conversation_id"],
                "tenantId": target["tenant_id"],
            },
            "serviceUrl": target["service_url"],
        }

        bridge = _import_bridge()
        try:
            token, _path = await bridge.acquire_reply_token(
                client=self._http_client,
                cfg=self._bridge_cfg,
                activity=token_input,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                bf_cache=self._bf_token_cache,
                validated_path=target.get("validated_path"),  # L4 (#100)
            )
        except Exception as e:
            logger.error("agent365 proactive token mint failed: %s", e)
            return SendResult(success=False, error=f"token: {e}")

        activity = {
            "type": "message",
            "from": dict(target["from"]),
            "recipient": dict(target["recipient"]),
            "conversation": {"id": target["conversation_id"]},
            "text": content,
            # #73(a): proactive sends are AI-generated content too.
            "entities": [dict(bridge.AI_GENERATED_CONTENT_ENTITY)],
        }
        service_url = target["service_url"].rstrip("/")
        url = _conversations_activities_url(service_url, target["conversation_id"])

        try:
            resp = await self._http_client.post(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
        except Exception as e:
            logger.error("agent365 proactive POST failed: %s", e)
            return SendResult(success=False, error=f"post: {e}")

        # httpx raises on .raise_for_status; do the same shape but avoid
        # hard-coupling to httpx specifics (the tests use MagicMock).
        status = getattr(resp, "status_code", None)
        if status is not None and not (200 <= int(status) < 300):
            msg = f"proactive POST returned status={status}"
            logger.error("agent365 proactive: %s", msg)
            return SendResult(success=False, error=msg)

        new_id = ""
        try:
            body = resp.json() if callable(getattr(resp, "json", None)) else None
            if isinstance(body, dict):
                new_id = str(body.get("id") or "")
        except Exception:
            # Server may return empty body — that's fine.
            pass

        return SendResult(success=True, message_id=new_id)

    async def _send_stream_start(
        self,
        *,
        chat_id: str,
        content: str,
        inbound: dict[str, Any],
    ) -> SendResult | None:
        """Slice 19s-bis: open a new BF stream from ``send()``.

        Returns the captured ``bf_stream_id`` as ``SendResult.message_id``
        so subsequent ``edit_message`` calls — which Hermes drives with
        whatever ``message_id`` we return — find the stream state by the
        same key in ``self._streams``.

        Returns ``None`` on stream-start failure so ``send()`` can fall
        back to a non-streaming activity rather than dropping the reply.
        """
        bridge = _import_bridge()
        conv = inbound.get("conversation") or {}
        service_url = str(inbound.get("serviceUrl") or "").rstrip("/")
        conv_id = conv.get("id")
        if not service_url or not conv_id:
            return None

        activity = {
            "type": "typing",
            # Strip the streaming cursor — see _strip_streaming_cursor
            # docstring for why this matters for BF's prefix-match rule.
            "text": _strip_streaming_cursor(content),
            "from": inbound.get("recipient") or {},
            "recipient": inbound.get("from") or {},
            "conversation": conv,
            "entities": [
                {
                    "type": "streaminfo",
                    "streamType": "streaming",
                    "streamSequence": 1,
                }
            ],
        }
        url = _conversations_activities_url(service_url, conv_id)
        try:
            token, _path = await bridge.acquire_reply_token(
                client=self._http_client,
                cfg=self._bridge_cfg,
                activity=inbound,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                bf_cache=self._bf_token_cache,
                validated_path=self._validated_path_for_inbound(inbound),  # L4 (#100)
            )
            resp = await self._http_client.post(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
        except Exception as e:
            logger.warning(
                "agent365 stream-start POST failed; falling back to "
                "non-streaming send: %s",
                e,
            )
            return None

        if resp.status_code != 201:
            logger.warning(
                "agent365 stream-start expected 201, got %s; "
                "falling back to non-streaming send",
                resp.status_code,
            )
            return None

        try:
            bf_stream_id = (resp.json() or {}).get("id")
        except Exception:
            bf_stream_id = None
        if not bf_stream_id:
            logger.warning(
                "agent365 stream-start 201 without id; falling back"
            )
            return None
        bf_stream_id = str(bf_stream_id)

        loop = asyncio.get_event_loop()
        now = loop.time()
        clean_content = _strip_streaming_cursor(content)
        self._streams[bf_stream_id] = {
            "bf_stream_id": bf_stream_id,
            "sequence": 1,
            "last_emit_ts": now,
            "opened_ts": now,
            "finalize_failures": 0,
            # Track last-sent content so auto-finalize-stale-stream has
            # something non-empty to POST as the close (BF rejects
            # empty-text final activities with 400 BadSyntax).
            "last_content": clean_content,
        }
        self._active_stream_by_chat[chat_id] = bf_stream_id
        return SendResult(success=True, message_id=bf_stream_id)

    async def _post_activity(
        self, *, inbound: dict[str, Any], activity: dict[str, Any]
    ) -> None:
        """POST a fresh activity (not a reply) to the inbound's
        serviceUrl. Used by ``send_typing``. Reuses ``send_reply``'s
        outbound user-FIC token chain via the bridge module."""
        bridge = _import_bridge()
        if self._http_client is None or self._bridge_cfg is None:
            raise RuntimeError("agent365: adapter not connected")
        service_url = str(inbound.get("serviceUrl") or "").rstrip("/")
        conv_id = (inbound.get("conversation") or {}).get("id")
        if not service_url or not conv_id:
            raise RuntimeError(
                "agent365 _post_activity: serviceUrl / conversation.id missing"
            )
        url = _conversations_activities_url(service_url, conv_id)
        token, _path = await bridge.acquire_reply_token(
            client=self._http_client,
            cfg=self._bridge_cfg,
            activity=inbound,
            fmi_cache=self._fmi_cache,
            user_cache=self._user_cache,
            bf_cache=self._bf_token_cache,
            validated_path=self._validated_path_for_inbound(inbound),  # L4 (#100)
        )
        resp = await self._http_client.post(
            url,
            json=activity,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"agent365 _post_activity: {resp.status_code} {resp.text[:200]}"
            )

    async def send_or_update_status(
        self,
        chat_id: str,
        status_key: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Render a gateway status/lifecycle callback, collapsing Copilot
        Chat bursts into a single bubble (#53).

        The gateway routes every status callback (retry/fallback traces,
        context-pressure notices, terminal-failure summaries) through this
        method when the adapter implements it
        (``gateway/run.py:_send_or_update_status_coro``); adapters without
        it fall back to plain ``send``. Telegram (issue #30045) edits one
        bubble in place per ``status_key``. Copilot Chat arrives as
        ``groupChat`` and does not visibly render a BF edit (#54
        branch-walk finding), so the edit-in-place trick is unavailable —
        a terminal-failure flush would otherwise render as N raw bubbles.

        Behaviour:

        - **Personal (Teams 1:1) / unknown chat type** → pass straight
          through to ``send`` (identical to the gateway's no-method
          fallback; Path A status behaviour is preserved — #53 discipline
          says do not filter personal status).
        - **Copilot Chat / non-personal** →
            - while a stream or coalesced reply is already active for the
              chat, suppress the status — interleaving a status bubble into
              an active turn is the same CEA-ordering problem ``send``
              already guards against (Copilot Chat would render it as an
              extra bubble);
            - otherwise coalesce: append the line under
              ``(chat_id, status_key)`` and flush one consolidated bubble
              once the burst settles (``_watch_coalesced_status``).
        """
        inbound = self._cached_inbound_for(chat_id)
        conv = (inbound or {}).get("conversation") or {}
        chat_type = str(conv.get("conversationType") or "")

        # Personal / unknown → preserve the gateway's plain-send fallback
        # exactly (this is also the path for chats we have no inbound for).
        #
        # Also fall back to plain send() when no inbound was captured *this
        # lifetime* (slice 19x-e / #27): the coalesce flush always uses
        # replyToActivity against the cached inbound's activity_id, but
        # ``_cached_inbound_for`` reads the persistent registry which survives
        # gateway restarts, so the cached activity_id can be stale. send()'s
        # own gate (``chat_id not in _seen_inbounds_this_lifetime`` → robust
        # ``_send_proactive`` via sendToConversation) is the replyToActivity
        # precondition; a resumed-turn status (built from persisted origin,
        # never through the webhook that populates the lifetime set) would
        # otherwise coalesce into a stale-activity-id reply BF can reject.
        if (
            inbound is None
            or chat_type == "personal"
            or not chat_type
            or chat_id not in self._seen_inbounds_this_lifetime
        ):
            return await self.send(chat_id, content, metadata=metadata)

        # Substantive single notices (``"warn"``) must never be buffered and
        # then silently dropped at flush time when a reply opens during the
        # debounce window. Route them through plain ``send`` so they post
        # immediately when no turn is active, or are CEA-suppressed by send()
        # when one is — same correctness as a transient trace, no silent loss
        # (#53 leading-notice finding). See ``_STATUS_PASS_THROUGH_KEYS``.
        if status_key in _STATUS_PASS_THROUGH_KEYS:
            return await self.send(chat_id, content, metadata=metadata)

        # Non-personal (Copilot Chat). Never interleave a status bubble into
        # an active turn — mirror send()'s reply_to=None suppression.
        if (
            chat_id in self._active_stream_by_chat
            or chat_id in self._active_coalesced_reply_by_chat
        ):
            active = (
                self._active_coalesced_reply_by_chat.get(chat_id)
                or self._active_stream_by_chat.get(chat_id)
                or ""
            )
            logger.info(
                "agent365 status suppressed while turn active: "
                "chat_id=%s status_key=%s active=%s",
                chat_id,
                status_key,
                active,
            )
            return SendResult(success=True, message_id=str(active))

        if not (content or "").strip():
            return SendResult(success=True, message_id="")

        return self._buffer_coalesced_status(
            chat_id=chat_id,
            status_key=status_key,
            content=content,
            inbound=inbound,
        )

    async def send_typing(
        self, chat_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Send a BF ``typing`` activity to the conversation. Renders
        as the trailing-dots indicator on Teams 1:1 chats."""
        inbound = self._cached_inbound_for(chat_id)
        if not inbound:
            # No-op: the gateway pulses typing periodically; without
            # a cached inbound we have nowhere to post.
            return None
        # Slice 19x-d (#4): bump last_used_at on typing too.
        self._conversations.mark_used(chat_id)
        typing_activity = {
            "type": "typing",
            "from": inbound.get("recipient") or {},
            "recipient": inbound.get("from") or {},
            "conversation": inbound.get("conversation") or {},
        }
        try:
            await self._post_activity(inbound=inbound, activity=typing_activity)
        except Exception as e:
            # Typing failures are best-effort — never raise into the
            # gateway's pulse loop.
            logger.warning("agent365 send_typing failed for %s: %s", chat_id, e)
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Render an Adaptive Card with an Image element + optional
        caption, route through send()'s outbound POST path."""
        bridge = _import_bridge()
        inbound = self._cached_inbound_for(chat_id)
        if not inbound:
            msg = f"agent365 send_image: no cached inbound for {chat_id!r}"
            logger.error(msg)
            return SendResult(success=False, error=msg)
        # Slice 19x-d (#4): bump last_used_at on image outbound.
        self._conversations.mark_used(chat_id)
        if self._http_client is None or self._bridge_cfg is None:
            msg = "agent365 send_image: adapter not connected"
            logger.error(msg)
            return SendResult(success=False, error=msg)
        if chat_id in self._active_stream_by_chat:
            msg = "agent365 send_image: active stream still open"
            logger.warning("%s for %s", msg, chat_id)
            return SendResult(success=False, error=msg)

        body: list[dict[str, Any]] = [{"type": "Image", "url": image_url}]
        if caption:
            body.append({"type": "TextBlock", "text": caption, "wrap": True})
        card = {
            "type": "AdaptiveCard",
            "version": "1.6",
            "body": body,
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        }
        reply = bridge.render_reply_activity(inbound, {"text": "", "card": card})
        try:
            await bridge.send_reply(
                inbound=inbound,
                reply=reply,
                cfg=self._bridge_cfg,
                client=self._http_client,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                validated_path=self._validated_path_for_inbound(inbound),  # L4 (#100)
            )
        except Exception as e:
            logger.error("agent365 send_image failed: %s", e)
            return SendResult(success=False, error=str(e))
        return SendResult(success=True, message_id=str(inbound.get("id") or ""))

    # ── #76c outbound file transfer (FileConsentCard → OneDrive) ───────────

    @staticmethod
    def _reply_with_attachment(
        inbound: dict[str, Any],
        attachment: dict[str, Any],
        *,
        text: str = "",
    ) -> dict[str, Any]:
        """Build a BF ``message`` reply carrying a single non-Adaptive
        attachment (Teams file consent/info cards). Mirrors
        ``bridge.render_reply_activity``'s conversation/recipient/from triple but
        deliberately OMITS the AI-generated-content entity — a file-transfer card
        is a platform affordance, not model output, so #73(a)'s label doesn't
        apply."""
        reply: dict[str, Any] = {
            "type": "message",
            "from": inbound.get("recipient", {}),
            "recipient": inbound.get("from", {}),
            "conversation": inbound.get("conversation", {}),
            "replyToId": inbound.get("id"),
            "attachments": [attachment],
        }
        if text:
            reply["text"] = text
        return reply

    async def _file_text_fallback(
        self,
        chat_id: str,
        file_name: str,
        caption: str,
        reply_to: str | None,
        metadata: dict[str, Any] | None,
    ) -> SendResult:
        """Degrade a file delivery to a plain-text notice (📎 name) via ``send``.
        Used when file consent isn't available: non-personal chats, or no pinned
        tenant host (R3-P1) — so the agent still communicates the file rather than
        offering a consent flow that can't complete."""
        text = f"📎 {file_name}"
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(
            chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata
        )

    async def _send_file_consent(
        self,
        chat_id: str,
        file_path: str,
        caption: str,
        file_name: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """#76c: offer a local file to a Teams 1:1 chat via a FileConsentCard.
        The bytes are NOT sent now — on Accept the user's ``fileConsent/invoke``
        (see ``_handle_file_consent``) carries the OneDrive upload session we PUT
        to. Personal scope only: a non-personal chat (Copilot Chat / group /
        channel) has no file-consent affordance, so it degrades to the base text
        fallback so the agent still communicates the file."""
        inbound = self._cached_inbound_for(chat_id)
        if not inbound:
            msg = f"agent365 send_file: no cached inbound for {chat_id!r}"
            logger.error(msg)
            return SendResult(success=False, error=msg)
        conv = inbound.get("conversation") or {}
        if str(conv.get("conversationType") or "") != "personal":
            logger.info(
                "agent365 send_file: non-personal chat %s → text fallback", chat_id
            )
            return await self._file_text_fallback(
                chat_id, file_name, caption, reply_to, metadata
            )
        # R3-P1: if no tenant host is pinned, file transfer is disabled — degrade
        # to the text fallback BEFORE offering a FileConsentCard that can never
        # complete (the accept-time upload would be refused by the empty
        # allowlist, leaving the user with a dead-end consent flow).
        if not self._file_host_allowlist:
            logger.warning(
                "agent365 send_file: A365_FILE_HOST_ALLOWLIST / "
                "extra.file_host_allowlist unset → text fallback (file transfer "
                "disabled; pin the tenant SharePoint/OneDrive host to enable)"
            )
            return await self._file_text_fallback(
                chat_id, file_name, caption, reply_to, metadata
            )
        if self._http_client is None or self._bridge_cfg is None:
            msg = "agent365 send_file: adapter not connected"
            logger.error(msg)
            return SendResult(success=False, error=msg)

        # Path-safety + existence + size via the gateway's shared validator
        # (resolves symlinks, blocks credential/system paths).
        safe_path = self.validate_media_delivery_path(file_path)
        if safe_path is None:
            msg = f"agent365 send_file: unsafe or missing path for {file_name!r}"
            logger.warning(msg)
            return SendResult(success=False, error=msg)
        # R2-P2: read the offered bytes ONCE through a bounded descriptor and bind
        # the consent to their SHA-256, so the accept-time upload can prove it is
        # sending exactly the content the user consented to. A same-size
        # replacement or a post-offer grow is caught by the digest / bound, not a
        # bare getsize() (which the TOCTOU exploited).
        offered = _read_file_bounded(safe_path, _MAX_OUTBOUND_FILE_BYTES)
        if offered is None:
            msg = f"agent365 send_file: unreadable or over-cap file for {file_name!r}"
            logger.warning(msg)
            return SendResult(success=False, error=msg)
        size = len(offered)
        if size <= 0:
            msg = f"agent365 send_file: empty file for {file_name!r}"
            logger.warning(msg)
            return SendResult(success=False, error=msg)
        digest = hashlib.sha256(offered).hexdigest()

        self._conversations.mark_used(chat_id)
        consent_id = uuid.uuid4().hex
        card = {
            "contentType": _FILE_CONSENT_CONTENT_TYPE,
            "name": file_name,
            "content": {
                "description": caption or file_name,
                "sizeInBytes": size,
                "acceptContext": {"consentId": consent_id},
                "declineContext": {"consentId": consent_id},
            },
        }
        reply = self._reply_with_attachment(inbound, card)
        try:
            await _import_bridge().send_reply(
                inbound=inbound,
                reply=reply,
                cfg=self._bridge_cfg,
                client=self._http_client,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                bf_cache=self._bf_token_cache,
                validated_path=self._validated_path_for_inbound(inbound),  # L4 (#100)
            )
        except Exception as e:
            logger.error("agent365 send_file consent card failed: %s", e)
            return SendResult(success=False, error=str(e))
        # Record the pending upload only AFTER the card is accepted by BF — a
        # failed send leaves no consent the user can act on.
        #
        # Security model (Review-F2 / R2-P1): the primary boundary is the
        # unguessable single-use ``consent_id`` (uuid4, minted here, sent ONLY in
        # this card to this conversation, popped once) presented by a
        # JWT-validated platform caller. The stored conversation/user/serviceUrl
        # are body-derived and are checked at accept as *consistency*
        # defence-in-depth, NOT as authenticated identity (BF service tokens carry
        # no end-user claim — see ``_handle_file_consent``). ``sha256`` binds the
        # accept to the exact offered content.
        self._pending_file_uploads[consent_id] = {
            "path": safe_path,
            "name": file_name,
            "size": size,
            "sha256": digest,
            "conversation_id": str(conv.get("id") or ""),
            "user_id": str((inbound.get("from") or {}).get("id") or ""),
            "service_url": str(inbound.get("serviceUrl") or ""),
            "validated_path": self._validated_path_for_inbound(inbound),
            "created_at": time.time(),
        }
        _bound_map(self._pending_file_uploads)
        return SendResult(success=True, message_id=consent_id)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """#76c: deliver a local file to a Teams 1:1 chat as a downloadable
        attachment via the FileConsentCard → OneDrive flow. Non-personal chats
        degrade to the base text fallback."""
        name = file_name or os.path.basename(file_path) or "file"
        return await self._send_file_consent(
            chat_id, file_path, caption or "", name,
            reply_to=reply_to, metadata=metadata,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """#76c: deliver a local image file to a Teams 1:1 chat as a downloadable
        attachment (FileConsentCard → OneDrive). Unlike ``send_image`` (URL →
        inline Adaptive Card), this hands over the actual bytes. Non-personal →
        text fallback."""
        name = os.path.basename(image_path) or "image"
        return await self._send_file_consent(
            chat_id, image_path, caption or "", name,
            reply_to=reply_to, metadata=metadata,
        )

    async def _handle_file_consent(
        self, activity: dict[str, Any], *, validated_path: str | None
    ) -> invoke.InvokeResponse:
        """#76c: handle a ``fileConsent/invoke`` — the user's Accept/Decline on a
        FileConsentCard we sent. On Accept: PUT the file bytes to the OneDrive
        upload session, then confirm with a FileInfoCard. On Decline / unknown
        consent: ack without uploading. ALWAYS returns a 200 InvokeResponse —
        Teams renders a non-200 as an upload-failure banner, and the outcome here
        (uploaded, declined, or lost-to-restart) is not a protocol error.

        The pending entry is popped BEFORE the upload, so a BF retry of the same
        invoke acks idempotently rather than uploading the file twice."""
        value = activity.get("value")
        value = value if isinstance(value, dict) else {}
        action = str(value.get("action") or "")
        context = value.get("context")
        context = context if isinstance(context, dict) else {}
        consent_id = str(context.get("consentId") or "")
        pending = self._pending_file_uploads.pop(consent_id, None)

        if action != "accept":
            logger.info(
                "agent365 fileConsent action=%s consent=%s", action, consent_id[:8]
            )
            return invoke.InvokeResponse(status=200, body={})
        if pending is None:
            # Consent we don't know about — restart dropped the in-memory map, a
            # replay, or a spoofed id. Ack without uploading.
            logger.warning(
                "agent365 fileConsent accept for unknown consent=%s", consent_id[:8]
            )
            return invoke.InvokeResponse(status=200, body={})

        # Review-F2 / R2-P1 — capability model, not authenticated-user check.
        # The real boundary is: (1) this handler is only reached AFTER inbound-JWT
        # validation (a legitimate platform caller), and (2) ``consent_id`` is an
        # unguessable uuid4 we minted, sent only in this card, and popped once. The
        # conversation / user / serviceUrl comparisons below are body-derived (BF
        # service tokens carry no end-user claim) and serve as CONSISTENCY
        # defence-in-depth — a valid consentId replayed onto a different
        # conversation still won't upload. They are NOT claimed as authenticated
        # identity.
        act_conv = str((activity.get("conversation") or {}).get("id") or "")
        act_user = str((activity.get("from") or {}).get("id") or "")
        act_surl = str(activity.get("serviceUrl") or "")
        if (
            act_conv != pending.get("conversation_id")
            or act_user != pending.get("user_id")
            or act_surl != pending.get("service_url")
        ):
            logger.warning(
                "agent365 fileConsent accept context mismatch consent=%s",
                consent_id[:8],
            )
            return invoke.InvokeResponse(status=200, body={})

        # Review-F3: a consent the user answers long after the offer is stale.
        if time.time() - float(pending.get("created_at") or 0.0) > _PENDING_UPLOAD_TTL_SEC:
            logger.warning("agent365 fileConsent accept expired consent=%s", consent_id[:8])
            return invoke.InvokeResponse(status=200, body={})

        upload_info = value.get("uploadInfo")
        upload_info = upload_info if isinstance(upload_info, dict) else {}
        upload_url = str(upload_info.get("uploadUrl") or "")
        if not upload_url or self._http_client is None:
            logger.warning("agent365 fileConsent accept missing uploadUrl/client")
            return invoke.InvokeResponse(status=200, body={})
        # R2-P1: only POST bytes to an EXACT configured tenant SharePoint/OneDrive
        # host — a `*.sharepoint.com` suffix would accept an attacker-owned tenant's
        # upload session (customer-registrable zone). Empty allowlist ⇒ refuse.
        if not _is_allowed_file_host(upload_url, self._file_host_allowlist):
            logger.warning(
                "agent365 fileConsent uploadUrl not on the configured tenant host "
                "allowlist; refused (%.60s)", upload_url
            )
            return invoke.InvokeResponse(status=200, body={})

        # Re-validate the path (may have been removed since the offer).
        safe_path = self.validate_media_delivery_path(str(pending.get("path") or ""))
        if safe_path is None:
            logger.warning("agent365 fileConsent accept: file no longer available")
            return invoke.InvokeResponse(status=200, body={})
        # R2-P2: read the current bytes ONCE through a bounded descriptor, then
        # verify size + SHA-256 against the offer. A file that grew past the cap,
        # shrank, or was swapped for same-size different content since the offer is
        # rejected — the bytes we upload are provably the ones the user consented
        # to, closing the getsize()-then-read TOCTOU.
        data = _read_file_bounded(safe_path, _MAX_OUTBOUND_FILE_BYTES)
        if data is None:
            logger.warning("agent365 fileConsent: file unreadable or over-cap at accept")
            return invoke.InvokeResponse(status=200, body={})
        size = len(data)
        if size <= 0 or size != int(pending.get("size") or -1):
            logger.warning("agent365 fileConsent: size changed since offer")
            return invoke.InvokeResponse(status=200, body={})
        if hashlib.sha256(data).hexdigest() != pending.get("sha256"):
            logger.warning("agent365 fileConsent: content changed since offer (digest)")
            return invoke.InvokeResponse(status=200, body={})
        try:
            put_resp = await self._http_client.put(
                upload_url,
                content=data,
                headers={
                    "Content-Length": str(size),
                    "Content-Range": f"bytes 0-{size - 1}/{size}",
                },
                timeout=60.0,
                follow_redirects=False,  # Review-F2: no redirect off the validated host
            )
            status_code = int(getattr(put_resp, "status_code", 0) or 0)
            if status_code < 200 or status_code >= 300:
                logger.error("agent365 fileConsent upload HTTP %s", status_code)
                return invoke.InvokeResponse(status=200, body={})
        except Exception as e:
            logger.error("agent365 fileConsent upload failed: %s", e)
            return invoke.InvokeResponse(status=200, body={})

        # Confirm with a FileInfoCard so the file renders inline in the chat.
        # Best-effort — the upload already succeeded, so a failed card is logged,
        # not surfaced as an error.
        info_card = {
            "contentType": _FILE_INFO_CONTENT_TYPE,
            "contentUrl": upload_info.get("contentUrl"),
            "name": upload_info.get("name") or pending.get("name"),
            "content": {
                "uniqueId": upload_info.get("uniqueId"),
                "fileType": upload_info.get("fileType"),
            },
        }
        try:
            reply = self._reply_with_attachment(activity, info_card)
            await _import_bridge().send_reply(
                inbound=activity,
                reply=reply,
                cfg=self._bridge_cfg,
                client=self._http_client,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                bf_cache=self._bf_token_cache,
                validated_path=validated_path,
            )
        except Exception as e:
            logger.warning("agent365 fileConsent info card failed: %s", e)

        logger.info(
            "agent365 fileConsent uploaded %d bytes consent=%s", size, consent_id[:8]
        )
        return invoke.InvokeResponse(status=200, body={})

    # ── #73(c) feedback loop + #82 handoff (invoke children) ──────────────

    async def _handle_feedback_submit(
        self, ctx: invoke.InvokeContext
    ) -> invoke.InvokeResponse:
        """#73(c): ``message/submitAction`` — the user pressed thumbs up/down on
        a reply (or submitted the feedback form). Teams stores nothing, so we
        record the reaction keyed by the replied message id (best-effort,
        in-memory). Always a 200 — a non-200 shows the user an error toast."""
        value = ctx.value
        action_name = str(value.get("actionName") or "")
        if action_name != "feedback":
            # Some other submitAction — ack without recording.
            return invoke.InvokeResponse(status=200, body={})
        action_value = value.get("actionValue")
        action_value = action_value if isinstance(action_value, dict) else {}
        # The reply the reaction targets: Teams sets replyToId on the invoke.
        msg_id = str(ctx.activity.get("replyToId") or ctx.activity.get("id") or "")
        if msg_id:
            self._feedback_by_message_id[msg_id] = {
                "reaction": str(action_value.get("reaction") or ""),
                "feedback": action_value.get("feedback"),
                "conversation_id": str(ctx.conv.get("id") or ""),
                "user_oid": ctx.user_oid,
            }
            _bound_map(self._feedback_by_message_id)
        logger.info(
            "agent365 feedback reaction=%s msg=%s",
            str(action_value.get("reaction") or "?"),
            msg_id[:16],
        )
        return invoke.InvokeResponse(status=200, body={})

    async def _handle_message_fetch_task(
        self, ctx: invoke.InvokeContext
    ) -> invoke.InvokeResponse:
        """#73(c): ``message/fetchTask`` — Teams requests a custom feedback form.
        We use the built-in ``feedbackLoop type:"default"`` (no custom form), so
        there is nothing to render; ack with a benign empty task so Teams closes
        cleanly rather than erroring."""
        return invoke.InvokeResponse(status=200, body={})

    def _handoff_deep_link(self, token: str) -> str:
        """#82: the Copilot→Teams continuation deep link. Clicking it opens a
        Teams 1:1 with this bot and fires a ``handoff/action`` invoke carrying
        ``token``. ``28:<botId>`` must be the **Teams-routable messaging bot id**
        — the Path B / Bot Framework app that owns the Teams channel — NOT the
        Path A CEA blueprint. For the standard split identity these differ, and
        the #89 walk (2026-07-16) caught the blueprint variant opening the wrong
        bot in Teams. Fall back to the blueprint only when no BF app is set (the
        single-identity deployment where they coincide)."""
        bot_id = self.bf_app_id or self.blueprint_app_id or ""
        return (
            "https://teams.microsoft.com/l/chat/0/0"
            f"?users=28:{bot_id}&continuation={token}"
        )

    def _maybe_append_handoff_link(self, chat_id: str, content: str) -> str:
        """#82: append a "continue in Teams" deep link to a degraded non-personal
        reply, when enabled (A365_HANDOFF_LINK). No-op for personal chats, when
        disabled, or when the conversation can't be resolved — the original
        content is returned unchanged. NB: Copilot Chat and a genuine Teams group
        are indistinguishable from the stored ref (the discriminator is the
        per-turn path tag, absent here), so an enabled link also appears on real
        Teams-group degraded replies — acceptable as the feature is opt-in and
        walk-gated (#89)."""
        if not self._handoff_link_enabled:
            return content
        ref = self._conversations.get(chat_id)
        if ref is None or ref.chat_type == "personal":
            return content
        link = self._mint_handoff_link(chat_id, reason="cc_degraded")
        if not link:
            return content
        return f"{content}\n\n[Continue in Teams]({link})"

    def _mint_handoff_link(self, chat_id: str, *, reason: str) -> str | None:
        """#82: mint a continuation token bound to ``chat_id`` and return the
        deep link, or None if we can't resolve the conversation. Used to append
        a "continue in Teams" affordance to a degraded Copilot Chat reply."""
        ref = self._conversations.get(chat_id)
        if ref is None:
            return None
        token = uuid.uuid4().hex
        self._handoff_tokens[token] = {
            "conversation_id": chat_id,
            "chat_type": ref.chat_type,
            "tenant_id": ref.tenant_id,
            "reason": reason,
        }
        _bound_map(self._handoff_tokens)
        return self._handoff_deep_link(token)

    async def _handle_handoff_action(
        self, ctx: invoke.InvokeContext
    ) -> invoke.InvokeResponse:
        """#82 (foundation only): ``handoff/action`` — the user clicked a
        continuation deep link, so Teams opened a 1:1 and handed us the token.

        Scope of THIS handler: validate + consume the continuation token (reject
        unknown/spoofed) and ack so Teams completes the handoff. It does **not**
        yet bridge the Copilot session into the Teams conversation — the agent
        lands in a fresh Teams turn. Actual session-context import needs a
        Hermes-core conversation-import hook that does not exist yet; #82 stays
        open, tracking that dependency, until it does. Always 200 (Teams reads a
        non-200 as a failed handoff)."""
        token = str(ctx.value.get("continuation") or "")
        origin = self._handoff_tokens.pop(token, None) if token else None
        if origin is None:
            logger.warning("agent365 handoff/action unknown token=%s", token[:8])
            return invoke.InvokeResponse(status=200, body={})
        # Record the CC-origin → Teams-conversation linkage so a later
        # Hermes-core session-import hook can bridge them.
        origin["resumed_conversation_id"] = str(ctx.conv.get("id") or "")
        logger.info(
            "agent365 handoff resumed: origin=%s teams=%s",
            origin.get("conversation_id"),
            origin["resumed_conversation_id"],
        )
        return invoke.InvokeResponse(status=200, body={})

    # ── #77 interactive-UI cards (approval / confirm / clarify) ────────────

    @staticmethod
    def _action_submit_card(text: str, actions: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        """Build an Adaptive Card with a prompt TextBlock + one ``Action.Submit``
        button per (title, data) pair. The ``data`` dict is echoed verbatim in
        the inbound ``message.value`` when the button is pressed."""
        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [{"type": "TextBlock", "text": text, "wrap": True}],
            "actions": [
                {"type": "Action.Submit", "title": title, "data": data}
                for (title, data) in actions
            ],
        }

    async def _send_card(
        self, chat_id: str, card: dict[str, Any], *, log_context: str
    ) -> SendResult:
        """Send an Adaptive Card as a reply (no AI-content label / feedback loop —
        these are system-interaction cards, not agent output). Returns
        ``success=False`` when there's no cached inbound or the adapter isn't
        connected, so the gateway falls back to its text prompt."""
        inbound = self._cached_inbound_for(chat_id)
        if not inbound:
            msg = f"agent365 {log_context}: no cached inbound for {chat_id!r}"
            logger.warning(msg)
            return SendResult(success=False, error=msg)
        if self._http_client is None or self._bridge_cfg is None:
            msg = f"agent365 {log_context}: adapter not connected"
            logger.warning(msg)
            return SendResult(success=False, error=msg)
        self._conversations.mark_used(chat_id)
        attachment = {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card,
        }
        reply = self._reply_with_attachment(inbound, attachment)
        try:
            await _import_bridge().send_reply(
                inbound=inbound,
                reply=reply,
                cfg=self._bridge_cfg,
                client=self._http_client,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                bf_cache=self._bf_token_cache,
                validated_path=self._validated_path_for_inbound(inbound),
            )
        except Exception as e:
            logger.error("agent365 %s card send failed: %s", log_context, e)
            return SendResult(success=False, error=str(e))
        return SendResult(success=True, message_id=str(inbound.get("id") or ""))

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """#77: dangerous-command Approve/Deny as an Adaptive Card. ``command`` is
        already credential-redacted gateway-side. On click the route resolves via
        ``tools.approval.resolve_gateway_approval(session_key, choice)``."""
        text = f"**Approval required** — {description}\n\n`{command}`"
        base = {"hermes_kind": _CARD_KIND_EXEC_APPROVAL, "session_key": session_key}
        actions = [
            ("Approve once", {**base, "choice": "once"}),
            ("Approve for session", {**base, "choice": "session"}),
            ("Always allow", {**base, "choice": "always"}),
            ("Deny", {**base, "choice": "deny"}),
        ]
        return await self._send_card(
            chat_id, self._action_submit_card(text, actions), log_context="exec_approval"
        )

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """#77: Approve Once / Always / Cancel for an expensive command. On click
        the route resolves via
        ``tools.slash_confirm.resolve(session_key, confirm_id, choice)`` and posts
        any follow-up text the resolver returns."""
        text = f"**{title}**\n\n{message}"
        base = {
            "hermes_kind": _CARD_KIND_SLASH_CONFIRM,
            "session_key": session_key,
            "confirm_id": confirm_id,
        }
        actions = [
            ("Approve once", {**base, "choice": "once"}),
            ("Always", {**base, "choice": "always"}),
            ("Cancel", {**base, "choice": "cancel"}),
        ]
        return await self._send_card(
            chat_id, self._action_submit_card(text, actions), log_context="slash_confirm"
        )

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: list | None,
        clarify_id: str,
        session_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """#77: multiple-choice clarification as choice buttons + a
        "Something else" free-text escape. Open-ended (no choices) sends the
        question as text and arms the gateway's text-intercept. On a choice click
        the route resolves via
        ``tools.clarify_gateway.resolve_gateway_clarify(clarify_id, choice_text)``;
        "Something else" arms ``mark_awaiting_text(clarify_id)``."""
        if not choices:
            inbound = self._cached_inbound_for(chat_id)
            if not inbound:
                msg = f"agent365 clarify: no cached inbound for {chat_id!r}"
                logger.warning(msg)
                return SendResult(success=False, error=msg)
            result = await self._send_reply_activity(
                inbound=inbound, content=question, log_context="clarify"
            )
            if result.success:
                # Arm the gateway text-intercept for the user's typed answer.
                try:
                    self._gw_mark_awaiting_text(clarify_id)
                except Exception as e:  # gateway tools absent (tests) / import race
                    logger.warning("agent365 clarify mark_awaiting_text failed: %s", e)
            return result
        actions: list[tuple[str, dict[str, Any]]] = [
            (
                str(choice),
                {
                    "hermes_kind": _CARD_KIND_CLARIFY,
                    "clarify_id": clarify_id,
                    "choice_text": str(choice),
                },
            )
            for choice in choices
        ]
        actions.append(
            (
                "Something else",
                {
                    "hermes_kind": _CARD_KIND_CLARIFY,
                    "clarify_id": clarify_id,
                    "choice": "other",
                },
            )
        )
        return await self._send_card(
            chat_id, self._action_submit_card(question, actions), log_context="clarify"
        )

    # ── #77 card-action routing (Action.Submit → gateway resolvers) ────────

    @staticmethod
    def _extract_card_action(activity: dict[str, Any]) -> dict[str, Any] | None:
        """Return the ``value`` dict of an inbound card ``Action.Submit`` if it is
        one of ours (tagged ``hermes_kind``), else None. Card submits arrive as a
        ``message`` activity carrying ``value`` and (usually) no ``text``."""
        if str(activity.get("type") or "") != "message":
            return None
        value = activity.get("value")
        if not isinstance(value, dict):
            return None
        if value.get("hermes_kind") in _CARD_ACTION_KINDS:
            return value
        return None

    # Gateway resolver seams — lazily import the gateway ``tools`` package (only
    # importable inside the gateway process at runtime; monkeypatched in tests).

    @staticmethod
    def _gw_resolve_approval(session_key: str, choice: str) -> int:
        from tools.approval import resolve_gateway_approval

        return resolve_gateway_approval(session_key, choice)

    @staticmethod
    async def _gw_resolve_slash_confirm(
        session_key: str, confirm_id: str, choice: str
    ) -> str | None:
        from tools.slash_confirm import resolve as _resolve

        return await _resolve(session_key, confirm_id, choice)

    @staticmethod
    def _gw_resolve_clarify(clarify_id: str, text: str) -> bool:
        from tools.clarify_gateway import resolve_gateway_clarify

        return resolve_gateway_clarify(clarify_id, text)

    @staticmethod
    def _gw_mark_awaiting_text(clarify_id: str) -> None:
        from tools.clarify_gateway import mark_awaiting_text

        mark_awaiting_text(clarify_id)

    async def _handle_card_action(
        self, activity: dict[str, Any], value: dict[str, Any]
    ) -> Any:
        """Route a card ``Action.Submit`` back to the gateway's pending-approval
        resolver. Never dispatches to the agent loop; always acks 200 (the button
        press is a control signal, not a user turn). Resolver errors degrade to a
        logged ack so a wire surprise on the walk isn't an HTTP 500."""
        from fastapi.responses import JSONResponse

        kind = str(value.get("hermes_kind") or "")
        try:
            if kind == _CARD_KIND_EXEC_APPROVAL:
                self._gw_resolve_approval(
                    str(value.get("session_key") or ""), str(value.get("choice") or "")
                )
            elif kind == _CARD_KIND_SLASH_CONFIRM:
                follow = await self._gw_resolve_slash_confirm(
                    str(value.get("session_key") or ""),
                    str(value.get("confirm_id") or ""),
                    str(value.get("choice") or ""),
                )
                if follow:
                    # The resolver returns a follow-up (command result / cancelled
                    # notice) the adapter must post as a new message.
                    await self._send_reply_activity(
                        inbound=activity, content=str(follow), log_context="slash_confirm"
                    )
            elif kind == _CARD_KIND_CLARIFY:
                clarify_id = str(value.get("clarify_id") or "")
                if str(value.get("choice") or "") == "other":
                    self._gw_mark_awaiting_text(clarify_id)
                else:
                    self._gw_resolve_clarify(
                        clarify_id, str(value.get("choice_text") or "")
                    )
        except Exception as e:
            logger.error("agent365 card action (%s) resolve failed: %s", kind, e)
        return JSONResponse({"status": "card_action", "kind": kind})

    # ── Slice 19s — BF streaming response protocol ────────────────────────

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Emit a Bot Framework streaming activity for this stream.

        Each call POSTs a *new* activity to the conversation's
        ``/activities`` endpoint — BF streaming activities are new
        POSTs, not PUTs against the original message. The first call
        for a given ``message_id`` starts a new stream
        (``streamSequence: 1``, no ``streamId``); the 201 response
        carries the BF-side ``streamId`` we use on every subsequent
        call. ``finalize=True`` swaps the activity ``type`` from
        ``typing`` to ``message``, sets ``streamType=final``, and
        omits ``streamSequence`` per the Microsoft spec.

        Non-personal chat types are coalesced into a single normal
        ``send_reply`` on ``finalize=True`` because Copilot Chat
        accepts but does not visibly render BF streaming activities.

        Returns ``SendResult(success=False, ...)`` and falls back to
        ``send()`` on:
        - missing cached inbound (proactive sends with no prior turn),
        - terminal 403 ``ContentStreamNotAllowed`` (2-min timeout,
          stop-button cancel, oversize message),
        - non-2xx HTTP responses.

        Soft path (``202 ContentStreamSequenceOrderPreConditionFailed``):
        out-of-order requests get dropped server-side; we log and
        return success since the most recent sequence wins anyway.

        References:
        - https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux
        - https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/overview-custom-engine-agent
        """
        inbound = self._cached_inbound_for(chat_id)
        if not inbound:
            return SendResult(
                success=False,
                error=f"agent365 edit_message: no cached inbound for chat_id={chat_id!r}",
            )

        # Slice 19x-d (#4): streaming edits are clear outbound traffic;
        # mark the conversation as recently used so prune respects it.
        self._conversations.mark_used(chat_id)

        # Slice 19s-bis follow-up — drop a recently-finalized message_id
        # follow-up call as a successful no-op. See ``_recently_finalized``
        # docstring for the Hermes stream-consumer quirk this guards.
        loop_now = asyncio.get_event_loop().time()
        self._prune_recently_finalized(loop_now)
        if message_id in self._recently_finalized:
            return SendResult(success=True, message_id="")

        # DM-only: BF streaming-ux doc:
        # "Streaming bot message is available only for one-on-one chats."
        conv = inbound.get("conversation") or {}
        if str(conv.get("conversationType") or "") != "personal":
            return await self._edit_coalesced_reply(
                chat_id=chat_id,
                message_id=message_id,
                content=content,
                finalize=finalize,
                inbound=inbound,
                loop_now=loop_now,
            )

        if self._http_client is None or self._bridge_cfg is None:
            return SendResult(
                success=False,
                error="agent365 edit_message: adapter not connected",
            )

        active_msg_id = self._active_stream_by_chat.get(chat_id)
        if active_msg_id and active_msg_id not in self._streams:
            self._active_stream_by_chat.pop(chat_id, None)
            active_msg_id = None
        if active_msg_id and active_msg_id != message_id:
            logger.info(
                "agent365 edit_message continuing active stream: "
                "chat_id=%s requested_message_id=%s active_message_id=%s "
                "finalize=%s",
                chat_id,
                message_id,
                active_msg_id,
                finalize,
            )
            message_id = active_msg_id

        state = self._streams.get(message_id)
        is_first = state is None
        if state is None:
            state = {
                "bf_stream_id": None,
                "sequence": 0,
                "last_emit_ts": 0.0,
                "opened_ts": loop_now,
                "finalize_failures": 0,
            }
            self._streams[message_id] = state

        # Throttle — Microsoft recommends 1.5-2 s pacing even though the
        # hard limit is 1 req/s. Adapter-side rather than relying on the
        # stream consumer's per-tick edit interval.
        loop = asyncio.get_event_loop()
        now = loop.time()
        elapsed = now - state["last_emit_ts"]
        if elapsed < _STREAMING_MIN_GAP_SEC and state["last_emit_ts"] > 0.0:
            await asyncio.sleep(_STREAMING_MIN_GAP_SEC - elapsed)

        state["sequence"] += 1
        entity: dict[str, Any] = {"type": "streaminfo"}
        if state["bf_stream_id"]:
            entity["streamId"] = state["bf_stream_id"]
        if finalize:
            entity["streamType"] = "final"
            # streamSequence MUST NOT be set on the final activity per
            # Microsoft's REST API spec.
        else:
            entity["streamType"] = "streaming"
            entity["streamSequence"] = state["sequence"]

        clean_content = _strip_streaming_cursor(content)
        # Track last-sent content for auto-finalize-stale-stream (slice
        # 19s-bis follow-up). BF requires non-empty text on the final
        # activity; if we end up auto-closing this stream because
        # Hermes never called finalize, we'll reuse this.
        state["last_content"] = clean_content
        activity: dict[str, Any] = {
            "type": "message" if finalize else "typing",
            # Strip the streaming cursor — Hermes appends one for visual
            # feedback, but BF's prefix-match rule rejects activities
            # whose text doesn't start with the previously streamed
            # chunk. See _strip_streaming_cursor.
            "text": clean_content,
            "from": inbound.get("recipient") or {},
            "recipient": inbound.get("from") or {},
            "conversation": conv,
            "entities": [entity],
        }

        bridge = _import_bridge()
        if finalize:
            # #73(a): the finalized, user-visible message is AI-generated
            # content — append the label alongside the streaminfo entity.
            # Intermediate (typing) chunks are NOT labelled.
            activity["entities"].append(dict(bridge.AI_GENERATED_CONTENT_ENTITY))
            # #73(c): feedback loop only on the FINAL streamed message
            # (streaming-UX rule — feedback/label are final-only).
            if self._feedback_enabled:
                activity["channelData"] = dict(bridge.FEEDBACK_LOOP_CHANNEL_DATA)
        service_url = str(inbound.get("serviceUrl") or "").rstrip("/")
        conv_id = conv.get("id")
        if not service_url or not conv_id:
            return SendResult(
                success=False,
                error="agent365 edit_message: serviceUrl or conversation.id missing",
            )
        url = _conversations_activities_url(service_url, conv_id)

        try:
            token, _path = await bridge.acquire_reply_token(
                client=self._http_client,
                cfg=self._bridge_cfg,
                activity=inbound,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                bf_cache=self._bf_token_cache,
                validated_path=self._validated_path_for_inbound(inbound),  # L4 (#100)
            )
            resp = await self._http_client.post(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
        except Exception as e:
            logger.warning("agent365 edit_message POST failed: %s", e)
            return SendResult(success=False, error=str(e))

        state["last_emit_ts"] = loop.time()

        # First request → 201 with {"id": "<streamId>"}. Capture for
        # subsequent calls and as the message_id returned to Hermes.
        if is_first and resp.status_code == 201:
            try:
                stream_id = (resp.json() or {}).get("id")
            except Exception:
                stream_id = None
            if not stream_id:
                logger.warning(
                    "agent365 edit_message: first streaming POST 201 "
                    "without id: %s",
                    resp.text[:200],
                )
                self._drop_stream_state(chat_id, message_id)
                return SendResult(
                    success=False,
                    error="streaming start returned no id",
                )
            state["bf_stream_id"] = str(stream_id)
            if finalize:
                # First + finalize is degenerate but legal — drop state.
                self._drop_stream_state(chat_id, message_id)
                self._recently_finalized[message_id] = loop_now
            else:
                self._active_stream_by_chat[chat_id] = message_id
            return SendResult(success=True, message_id=state["bf_stream_id"])

        if 200 <= resp.status_code < 300:
            # 202 is the happy path for subsequent calls. The body may
            # contain a ContentStreamSequenceOrderPreConditionFailed
            # signal (out-of-order); we log it and keep going since the
            # most-recent sequence wins server-side anyway.
            err_code = self._maybe_extract_error_code(resp)
            if err_code == "ContentStreamSequenceOrderPreConditionFailed":
                logger.debug(
                    "agent365 edit_message: stream sequence %d arrived "
                    "out-of-order; server-side dedup retains the latest",
                    state["sequence"],
                )
            if finalize:
                self._drop_stream_state(chat_id, message_id)
                self._recently_finalized[message_id] = loop_now
            return SendResult(success=True, message_id=state.get("bf_stream_id") or "")

        if resp.status_code == 403:
            # Terminal — fall back. Drop stream state so the next call
            # starts cleanly. Map common Microsoft messages to short
            # error tags Hermes can surface to operators.
            err_msg = self._extract_error_message(resp)
            self._drop_stream_state(chat_id, message_id)
            short = err_msg
            low = err_msg.lower()
            if "exceeded streaming time" in low:
                short = "streaming timeout"
            elif "canceled by user" in low:
                short = "streaming canceled by user"
            elif "message size too large" in low:
                short = "streaming message too large"
            elif "already completed" in low:
                short = "streaming already completed"
            return SendResult(success=False, error=short)

        if resp.status_code == 429:
            return SendResult(success=False, error="streaming rate limited")

        # Other non-2xx codes — surface for diagnosis.
        return SendResult(
            success=False,
            error=f"agent365 edit_message HTTP {resp.status_code}: "
                  f"{resp.text[:200] if hasattr(resp, 'text') else ''}",
        )

    async def _auto_finalize_stale_stream(
        self,
        *,
        chat_id: str,
        message_id: str,
        inbound: dict[str, Any],
    ) -> bool:
        """Emit a synthetic ``streamType=final`` POST to close a stream
        that Hermes' consumer abandoned without calling
        ``edit_message(finalize=True)``. Best-effort: any failure here
        leaves the stream in its prior state; the next ``send()`` /
        finalize will retry or BF will eventually time out at 2 min.

        Slice 19s-bis follow-up — observed when Hermes segments at
        ``interim_assistant_messages`` boundaries: the consumer flips to
        a new ``_message_id`` without firing finalize=True for the old
        one, leaving stream A as a stuck typing indicator while stream B
        opens beside it.
        """
        state = self._streams.get(message_id)
        if state is None or not state.get("bf_stream_id"):
            # Nothing to close, or never received a stream id from
            # Microsoft (stream-start failed). Just drop the slot.
            self._drop_stream_state(chat_id, message_id)
            return True

        bf_stream_id = state["bf_stream_id"]
        conv = inbound.get("conversation") or {}
        service_url = str(inbound.get("serviceUrl") or "").rstrip("/")
        conv_id = conv.get("id")
        if not service_url or not conv_id:
            self._drop_stream_state(chat_id, message_id)
            return True

        # BF rejects final activities with empty text (400 BadSyntax).
        # Use the last content we streamed for this stream; fall back to
        # a single space if nothing was tracked. The text content is
        # what becomes the visible bubble's final state, so this is
        # also what the user reads.
        final_text = state.get("last_content") or " "
        activity = {
            "type": "message",
            "text": final_text,
            "from": inbound.get("recipient") or {},
            "recipient": inbound.get("from") or {},
            "conversation": conv,
            "entities": [
                {
                    "type": "streaminfo",
                    "streamId": bf_stream_id,
                    "streamType": "final",
                }
            ],
        }
        url = _conversations_activities_url(service_url, conv_id)
        try:
            bridge = _import_bridge()
            token, _path = await bridge.acquire_reply_token(
                client=self._http_client,
                cfg=self._bridge_cfg,
                activity=inbound,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
                bf_cache=self._bf_token_cache,
                validated_path=self._validated_path_for_inbound(inbound),  # L4 (#100)
            )
            resp = await self._http_client.post(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
        except Exception as e:
            logger.warning(
                "agent365 auto-finalize stale stream %s failed: %s",
                bf_stream_id, e,
            )
            return self._record_stale_finalize_failure(
                chat_id=chat_id,
                message_id=message_id,
                state=state,
                reason=str(e),
            )

        if 200 <= resp.status_code < 300:
            logger.info(
                "agent365 auto-finalize stale stream %s: status=%s",
                bf_stream_id, resp.status_code,
            )
            self._drop_stream_state(chat_id, message_id)
            loop_now = asyncio.get_event_loop().time()
            self._recently_finalized[message_id] = loop_now
            return True

        logger.warning(
            "agent365 auto-finalize stale stream %s returned HTTP %s: %s",
            bf_stream_id,
            resp.status_code,
            resp.text[:200] if hasattr(resp, "text") else "",
        )
        return self._record_stale_finalize_failure(
            chat_id=chat_id,
            message_id=message_id,
            state=state,
            reason=f"HTTP {resp.status_code}",
        )

    def _record_stale_finalize_failure(
        self,
        *,
        chat_id: str,
        message_id: str,
        state: dict[str, Any],
        reason: str,
    ) -> bool:
        """Return True when the failed stale stream was force-dropped.

        One failed close blocks the replacement stream to avoid
        knowingly interleaving activities. Repeated failure or an
        already-expired stream id is treated as dead BF state; force-drop
        it so the chat cannot wedge forever.
        """
        loop_now = asyncio.get_event_loop().time()
        failures = int(state.get("finalize_failures") or 0) + 1
        state["finalize_failures"] = failures
        opened_ts = state.get("opened_ts")
        if not isinstance(opened_ts, (int, float)):
            opened_ts = loop_now
            state["opened_ts"] = opened_ts
        age = loop_now - float(opened_ts)
        if (
            failures >= _STREAMING_FINALIZE_MAX_FAILURES
            or age >= _STREAMING_FORCE_DROP_AFTER_SEC
        ):
            logger.warning(
                "agent365 force-dropping stale stream after failed finalize: "
                "chat_id=%s message_id=%s failures=%s age=%.1fs reason=%s",
                chat_id,
                message_id,
                failures,
                age,
                reason,
            )
            self._drop_stream_state(chat_id, message_id)
            self._recently_finalized[message_id] = loop_now
            return True
        return False

    def _prune_recently_finalized(self, now: float) -> None:
        """Drop ``_recently_finalized`` entries older than the TTL."""
        cutoff = now - self._recently_finalized_ttl_sec
        stale = [k for k, ts in self._recently_finalized.items() if ts < cutoff]
        for k in stale:
            self._recently_finalized.pop(k, None)

    def _drop_stream_state(self, chat_id: str, message_id: str) -> None:
        """Slice 19s-bis: clear both ``self._streams[message_id]`` and the
        chat's active-stream slot. Called on finalize success and terminal
        errors so the next ``send()`` for the same conversation starts a
        fresh stream cleanly."""
        self._streams.pop(message_id, None)
        # Only clear the chat-level slot if it points at the same id we're
        # cleaning up — protects against tool-progress streams clobbering
        # a content stream's slot (or vice versa).
        if self._active_stream_by_chat.get(chat_id) == message_id:
            self._active_stream_by_chat.pop(chat_id, None)

    def _teardown_chat_state(self, chat_id: str) -> None:
        """L3 (#105): drop every live stream / coalesced-reply / coalesced-status
        slot for ``chat_id`` and cancel their debounce watchdog tasks.

        Called on lifecycle **evict** (uninstall). Without this, the registry
        ref is dropped but the coalesced-reply / status watchdogs keep running
        and later fire a doomed POST into a conversation the tenant already
        removed the agent from. Pops are no-ops when a slot is absent, so it's
        safe to call unconditionally."""
        # Streaming slot.
        sid = self._active_stream_by_chat.pop(chat_id, None)
        if sid is not None:
            self._streams.pop(sid, None)
        # Coalesced-reply slot + its watchdog.
        mid = self._active_coalesced_reply_by_chat.pop(chat_id, None)
        if mid is not None:
            self._coalesced_replies.pop(mid, None)
            task = self._coalesced_reply_tasks.pop(mid, None)
            if task is not None:
                task.cancel()
        # Coalesced-status slots (keyed ``status:{chat}:{status_key}``) + their
        # watchdogs. Match on the stored chat_id, not a key prefix, since a
        # Teams chat id itself contains ':'.
        status_keys = [
            k
            for k, st in self._coalesced_status.items()
            if isinstance(st, dict) and st.get("chat_id") == chat_id
        ]
        for k in status_keys:
            self._coalesced_status.pop(k, None)
            task = self._coalesced_status_tasks.pop(k, None)
            if task is not None:
                task.cancel()

    @staticmethod
    def _extract_error_message(resp: Any) -> str:
        """Best-effort extraction of Microsoft's error message text."""
        try:
            body = resp.json() or {}
            err = body.get("error") or {}
            if isinstance(err, dict):
                msg = err.get("message")
                if isinstance(msg, str):
                    return msg
            return str(body)[:200]
        except Exception:
            try:
                return resp.text[:200]
            except Exception:
                return ""

    @staticmethod
    def _maybe_extract_error_code(resp: Any) -> str | None:
        """Return Microsoft's error code from a 2xx response body, if any.

        202 responses can carry a ``ContentStreamSequenceOrderPreConditionFailed``
        soft-error code in the body. Pure 2xx ``{}`` returns ``None``.
        """
        try:
            body = resp.json() or {}
        except Exception:
            return None
        err = body.get("error") or {}
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, str):
                return code
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Return chat metadata sourced from the durable registry."""
        ref = self._conversations.get(chat_id)
        if ref is None:
            return {"name": chat_id, "type": "personal", "chat_id": chat_id}
        chat_type = (
            "personal"
            if ref.chat_type == "personal"
            else ("group" if ref.chat_type == "groupChat" else "channel")
        )
        return {
            "name": ref.chat_name or chat_id,
            "type": chat_type,
            "chat_id": chat_id,
        }


# ---------------------------------------------------------------------------
# Plugin entry points
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """Probe for the bridge runtime extras (FastAPI, httpx, pyjwt[crypto], uvicorn)."""
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        import jwt  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config: Any) -> bool:
    """Plugin loader pre-flight check. We accept any config that has
    `A365_TENANT_ID` + `A365_APP_ID` available either via env or
    ``extra`` — and, if a slug is explicitly configured, that it is
    path-safe."""
    extra = getattr(config, "extra", {}) or {}
    tenant = os.getenv("A365_TENANT_ID") or extra.get("tenant_id")
    app = os.getenv("A365_APP_ID") or extra.get("app_id")
    if not (tenant and app):
        return False
    # Review P2: reject an explicitly configured non-path-safe slug at
    # pre-flight (mirrors __init__'s fail-closed guard) rather than let the
    # adapter route to the shared "default" profile state.
    slug_raw = str(extra.get("slug") or os.getenv("AGENT_IDENTITY") or "")
    if slug_raw:
        try:
            validate_slug(slug_raw)
        except ValueError:
            logger.warning(
                "agent365 configured slug %r is not path-safe; refusing to load",
                slug_raw,
            )
            return False
    return True


def is_connected(config: Any) -> bool:
    """Plugin-loader liveness probe.

    Signature is ``Callable[[Any], bool]`` per
    ``gateway/platform_registry.py:64`` — the registry passes the
    ``PlatformConfig`` so the probe can inspect operator config without
    holding an adapter instance. We treat "configured well enough to
    connect" as the connection signal here, mirroring IRC's pattern;
    actual liveness is observable via ``GET /healthz`` once
    ``connect()`` has run.
    """
    return validate_config(config)


# Well-known Entra app id of the operator-side "Agent 365 CLI" custom
# client app (Microsoft's convention; created by setup_blueprint and
# carried across walkthroughs). Used to reseed ``~/a365.config.json``
# when sweep-collateral leaves it with an empty clientAppId.
_AGENT365_CLI_APP_ID = "58bfafcb-cfd6-4b3f-ba3b-a9e5848ac061"


# Slice 19r-bis (#25): the GA `a365` CLI reads its generated config from
# the XDG-standard location ``~/.config/a365/a365.generated.config.json``
# and does NOT honour our ``A365_GENERATED_CONFIG_PATH`` env var. When
# the operator chooses a non-XDG path, we ensure the CLI can still find
# the config by maintaining a symlink at the XDG location. Surfaced
# during the 2026-05-12 walkthrough as a setup-wizard gap.


def _xdg_generated_config_path(home: Path | None = None) -> Path:
    """Return the GA CLI's expected XDG path for the generated config."""
    base = home if home is not None else Path.home()
    return base / ".config" / "a365" / "a365.generated.config.json"


def _ensure_xdg_generated_config_symlink(
    target: Path,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    """Ensure the GA CLI can find the generated config at the XDG path.

    Returns a status dict::

        {"status": "noop|created|repaired|skipped_real_file|error",
         "xdg_path": str,
         "target": str,
         "message": str}

    Idempotent. Never clobbers an operator-owned real file at the XDG
    path — if a non-symlink exists there, returns ``skipped_real_file``
    with a clear message.
    """
    xdg_path = _xdg_generated_config_path(home)
    target_abs = target.resolve() if target.exists() else target.absolute()
    out: dict[str, Any] = {
        "status": "error",
        "xdg_path": str(xdg_path),
        "target": str(target_abs),
        "message": "",
    }

    # If the operator already keeps the generated config at the XDG
    # path directly, nothing to do.
    if target_abs == xdg_path.absolute():
        out["status"] = "noop"
        out["message"] = (
            f"Generated config already at XDG path {xdg_path}; no symlink needed."
        )
        return out

    # Ensure parent dir exists.
    try:
        xdg_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        out["message"] = f"Couldn't create {xdg_path.parent}: {e}"
        return out

    if xdg_path.is_symlink():
        try:
            current = xdg_path.readlink()
        except OSError as e:
            out["message"] = f"Couldn't read existing symlink {xdg_path}: {e}"
            return out
        current_abs = current if current.is_absolute() else xdg_path.parent / current
        if current_abs.resolve() == target_abs:
            out["status"] = "noop"
            out["message"] = (
                f"XDG symlink already points at {target_abs}; no change."
            )
            return out
        # Wrong target — repair.
        try:
            xdg_path.unlink()
            xdg_path.symlink_to(target_abs)
        except OSError as e:
            out["message"] = f"Couldn't repair symlink {xdg_path}: {e}"
            return out
        out["status"] = "repaired"
        out["message"] = (
            f"Repaired XDG symlink {xdg_path} → {target_abs} "
            f"(was pointing at {current_abs})."
        )
        return out

    if xdg_path.exists():
        # Non-symlink file or directory — don't clobber.
        out["status"] = "skipped_real_file"
        out["message"] = (
            f"{xdg_path} is a real file/dir, not a symlink — leaving alone. "
            "Manually remove or back it up if you want the wizard to link "
            f"to {target_abs}."
        )
        return out

    # Doesn't exist — create.
    try:
        xdg_path.symlink_to(target_abs)
    except OSError as e:
        out["message"] = f"Couldn't create symlink {xdg_path}: {e}"
        return out
    out["status"] = "created"
    out["message"] = (
        f"Created XDG symlink {xdg_path} → {target_abs} so the GA `a365` CLI can find it."
    )
    return out


def _detect_drift(
    *,
    home: Path | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan operator config files for drift that accumulates across
    walkthroughs. Returns a list of dicts shaped::

        {"key": str, "message": str, "fixer": Callable[[], None] | None}

    Each ``key`` is stable for tests + ordering. Empty list means no
    drift detected. Fix functions, where present, are safe to call in
    any order — they each touch a single file with a narrow write.

    Read-only — never mutates anything just by being called.

    Args:
        home: Override for ``Path.home()``. Defaults to the real home
            dir. Tests pass a tmp_path to isolate the filesystem reads.
        config: Override for the parsed ``~/.hermes/config.yaml``.
            Defaults to ``hermes_cli.config.load_config()``. Tests
            pass a synthetic dict to exercise stanza-shape branches.
    """
    import json as _json
    from pathlib import Path as _Path

    drift: list[dict[str, Any]] = []
    home_dir = home if home is not None else _Path.home()
    operator_env = home_dir / ".hermes" / ".env"
    agents_dir = home_dir / ".hermes" / "agents"
    a365_config = home_dir / "a365.config.json"

    # Helpers — kept inline so this function has no module-level deps
    # that could fail at gateway-load time.
    def _read_env(path: _Path) -> dict[str, str]:
        if not path.is_file():
            return {}
        out: dict[str, str] = {}
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
        return out

    def _load_yaml_config() -> dict[str, Any] | None:
        if config is not None:
            return config
        try:
            from hermes_cli.config import load_config
            cfg_ = load_config()
            return cfg_ if isinstance(cfg_, dict) else None
        except Exception:
            return None

    def _load_json(path: _Path) -> dict[str, Any] | None:
        try:
            with open(path) as f:
                obj = _json.load(f)
            return obj if isinstance(obj, dict) else None
        except (OSError, _json.JSONDecodeError):
            return None

    env_vars = _read_env(operator_env)
    cfg = _load_yaml_config() or {}
    stanza = (
        cfg.get("gateway", {}).get("platforms", {}).get("agent365", {})
        if isinstance(cfg.get("gateway"), dict)
        else {}
    )
    extra = stanza.get("extra", {}) if isinstance(stanza, dict) else {}

    # 1. Stale A365_APP_ID — operator .env vs the latest generated
    #    config's agentBlueprintId. Indicates a prior register's
    #    output never propagated into the bootstrap env, so the
    #    bridge would authenticate against the wrong app.
    generated_path_hint = (
        env_vars.get("A365_GENERATED_CONFIG_PATH")
        or extra.get("generated_config_path")
        or str(home_dir / "a365.generated.config.json")
    )
    generated = _load_json(_Path(generated_path_hint)) or {}
    env_app = env_vars.get("A365_APP_ID", "")
    gen_app = str(generated.get("agentBlueprintId") or "")
    if env_app and gen_app and env_app != gen_app:
        drift.append(
            {
                "key": "app_id_stale",
                "message": (
                    f"A365_APP_ID in ~/.hermes/.env is {env_app[:8]}… but "
                    f"{generated_path_hint} carries {gen_app[:8]}… — "
                    "operator .env is stale (a prior register's output didn't propagate)."
                ),
                # interactive_setup's regular flow re-reads the
                # generated config + saves, so no auto-fixer needed.
                "fixer": None,
            }
        )

    # 2. Slug mismatch — config.yaml stanza references a slug that
    #    isn't present under ~/.hermes/agents/. Indicates the platform
    #    block survived a tenant change.
    stanza_slug = extra.get("slug")
    agent_slugs = (
        sorted(d.name for d in agents_dir.iterdir() if d.is_dir())
        if agents_dir.is_dir()
        else []
    )
    if stanza_slug and agent_slugs and stanza_slug not in agent_slugs:
        drift.append(
            {
                "key": "slug_orphan",
                "message": (
                    f"config.yaml stanza slug={stanza_slug!r} but ~/.hermes/agents/ has "
                    f"{agent_slugs!r}. Platform block points at a non-existent per-agent dir."
                ),
                "fixer": None,
            }
        )

    # 3. ~/a365.config.json sweep collateral — missing or empty
    #    tenantId / clientAppId. Causes `update-endpoint --apply`
    #    to exit early with config-validation errors.
    cfg_json = _load_json(a365_config) if a365_config.is_file() else None
    needs_reseed = False
    detected_tenant = ""
    if not a365_config.is_file():
        # Missing file is fine if the operator works from a different
        # cwd; only warn when the platform stanza explicitly references
        # a different path that DOES exist.
        pass
    elif cfg_json is not None and (not cfg_json.get("tenantId") or not cfg_json.get("clientAppId")):
        needs_reseed = True
        # Detect tenant from az for the reseed hint.
        import subprocess as _subprocess
        try:
            r = _subprocess.run(
                ["az", "account", "show", "--query", "tenantId", "-o", "tsv"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if r.returncode == 0:
                detected_tenant = r.stdout.strip()
        except (OSError, _subprocess.TimeoutExpired):
            pass

    if needs_reseed:
        target_tenant = detected_tenant or env_vars.get("A365_TENANT_ID", "")

        def _reseed() -> None:
            cur = _load_json(a365_config) or {}
            if not cur.get("tenantId") and target_tenant:
                cur["tenantId"] = target_tenant
            if not cur.get("clientAppId"):
                cur["clientAppId"] = _AGENT365_CLI_APP_ID
            with open(a365_config, "w") as f:
                _json.dump(cur, f, indent=2)
                f.write("\n")

        drift.append(
            {
                "key": "a365_config_empty",
                "message": (
                    f"{a365_config} exists with empty tenantId/clientAppId — "
                    "`hermes a365 activity-bridge update-endpoint --apply` will fail."
                ),
                "fixer": _reseed if (target_tenant and a365_config.is_file()) else None,
            }
        )

    # Slice 19r-bis (#25). 5. XDG symlink for the generated config.
    #    The GA `a365` CLI reads ~/.config/a365/a365.generated.config.json
    #    and does NOT honour A365_GENERATED_CONFIG_PATH. If our generated
    #    config lives elsewhere, the XDG path must symlink to it or
    #    `a365 publish` fails with "agentBlueprintId missing".
    target_path = _Path(generated_path_hint)
    xdg_path = _xdg_generated_config_path(home_dir)
    if (
        target_path.is_file()
        and target_path.resolve() != xdg_path.absolute()
    ):
        def _fix_xdg() -> None:
            _ensure_xdg_generated_config_symlink(target_path, home=home_dir)

        if not xdg_path.exists() and not xdg_path.is_symlink():
            drift.append(
                {
                    "key": "xdg_symlink_missing",
                    "message": (
                        f"GA `a365` CLI expects {xdg_path} but it's missing; "
                        f"your generated config lives at {target_path}. "
                        "`a365 publish` will fail with 'agentBlueprintId missing'."
                    ),
                    "fixer": _fix_xdg,
                }
            )
        elif xdg_path.is_symlink():
            try:
                current = xdg_path.readlink()
                current_abs = (
                    current if current.is_absolute() else xdg_path.parent / current
                )
                if current_abs.resolve() != target_path.resolve():
                    drift.append(
                        {
                            "key": "xdg_symlink_wrong_target",
                            "message": (
                                f"{xdg_path} symlinks to {current_abs} but your generated "
                                f"config lives at {target_path}. The GA CLI may read stale data."
                            ),
                            "fixer": _fix_xdg,
                        }
                    )
            except OSError:
                pass
        # If xdg_path is a real (non-symlink) file, we don't flag drift
        # — the operator may have deliberately seeded it. Surface in
        # doctor instead if needed.

    # 4. generated_config_path in config.yaml stanza is unreachable
    #    or has an empty blueprint id. Indicates the stanza was
    #    written before the file was emitted (or pointed at a
    #    superseded path).
    stanza_gpath = extra.get("generated_config_path")
    if stanza_gpath:
        gp = _Path(stanza_gpath)
        if not gp.is_file():
            drift.append(
                {
                    "key": "generated_config_missing",
                    "message": (
                        f"config.yaml generated_config_path={stanza_gpath} doesn't exist — "
                        "stanza points at a superseded path or register never wrote it."
                    ),
                    "fixer": None,
                }
            )
        else:
            gen_at_stanza = _load_json(gp) or {}
            if not gen_at_stanza.get("agentBlueprintId"):
                drift.append(
                    {
                        "key": "generated_config_blank",
                        "message": (
                            f"{stanza_gpath} exists but agentBlueprintId is empty — "
                            "register apply must have failed or this is a stale empty seed."
                        ),
                        "fixer": None,
                    }
                )

    return drift


def _gate_env_secret_write(
    env_path: Path,
    *,
    prompt_yes_no: Any,
    print_warning: Any,
) -> bool:
    """CS-002 (#110): may ``env_path`` receive the blueprint client secret?

    The parent hermes_cli ``save_env_value`` deliberately *preserves* a
    pre-existing ``.env``'s mode (it only hardens files it creates), so
    by the time the wizard writes ``A365_BLUEPRINT_CLIENT_SECRET`` an
    already-permissive file (e.g. 0644) would silently stay permissive.

    Gate: a group/world-readable ``.env`` is offered a chmod-600 repair;
    declining that requires an explicit second confirmation that names
    the current mode and the risk. Anything else — stat failure, both
    prompts declined — refuses the write (fail-closed). A missing file
    passes: ``save_env_value`` hardens fresh files on create.

    Prompt/print hooks are injected so tests don't need the hermes_cli
    harness. Returns True when the secret write may proceed.
    """
    try:
        mode = stat.S_IMODE(env_path.stat().st_mode)
    except FileNotFoundError:
        return True
    except OSError as e:
        print_warning(
            f"Could not check permissions on {env_path} ({e}); refusing to "
            "write the secret there."
        )
        return False
    if not (mode & 0o077):
        return True
    print_warning(
        f"{env_path} is group/world-readable (mode {mode:03o}) — the "
        "blueprint client secret would be readable by other local users."
    )
    if prompt_yes_no(f"chmod 600 {env_path} before saving the secret?", True):
        try:
            os.chmod(env_path, 0o600)
        except OSError as e:
            print_warning(f"chmod failed ({e}); refusing to write the secret.")
            return False
        return True
    return bool(
        prompt_yes_no(
            f"Save the secret into {env_path} at mode {mode:03o} ANYWAY? "
            "(any local reader of the file can recover it)",
            False,
        )
    )


def interactive_setup() -> None:
    """``hermes gateway setup --platform agent365`` wizard.

    Assumes ``hermes a365 register --apply --m365 --aiteammate`` has
    already been run (the blueprint Entra app + permissions are set
    up). This wizard wires the platform side: bootstraps env vars in
    ``~/.hermes/.env``, ensures ``agent365`` is in ``plugins.enabled``
    in ``~/.hermes/config.yaml``, and writes the
    ``gateway.platforms.agent365`` block.

    Idempotent — re-running detects existing values and prompts
    update-vs-keep. Slice 19r-b adds a drift-detection pass that runs
    first: if any drift is found, the wizard surfaces it as warnings,
    runs auto-fixers for items that have them, and falls through to
    the regular reconfigure flow without asking again.

    Lazy-imports the ``hermes_cli.setup`` / ``hermes_cli.plugins_cmd``
    helpers so the plugin module stays importable in non-CLI contexts
    (gateway runtime, ``pytest`` without the harness).
    """
    import json
    import subprocess
    from pathlib import Path

    from hermes_cli.setup import (
        get_env_path,
        get_env_value,
        print_header,
        print_info,
        print_success,
        print_warning,
        prompt,
        prompt_choice,
        prompt_yes_no,
        save_env_value,
    )

    print_header("Microsoft Agent 365")

    # Slice 19r-b: drift detection pass.
    drift = _detect_drift()
    drift_force_reconfigure = False
    if drift:
        print_warning(f"Found {len(drift)} configuration drift item(s):")
        for item in drift:
            print_info(f"  • [{item['key']}] {item['message']}")
        print()
        if prompt_yes_no(
            "Fix drift now (auto-fixers + reconfigure)?",
            True,
        ):
            applied = 0
            for item in drift:
                fixer = item.get("fixer")
                if callable(fixer):
                    try:
                        fixer()
                        print_success(f"  ✓ auto-fixed [{item['key']}]")
                        applied += 1
                    except Exception as e:
                        print_warning(f"  ✗ auto-fixer for [{item['key']}] failed: {e}")
            if applied:
                print_info(f"Auto-fixed {applied} item(s). Continuing to wizard to fix the rest…")
            drift_force_reconfigure = True
            print()
        else:
            print_info("Skipping drift fixes. Re-run the wizard if you change your mind.")

    existing_tenant = get_env_value("A365_TENANT_ID")
    existing_app = get_env_value("A365_APP_ID")
    if existing_tenant and existing_app and not drift_force_reconfigure:
        print_info(
            f"agent365: already configured (tenant={existing_tenant[:8]}…, "
            f"app={existing_app[:8]}…)"
        )
        if not prompt_yes_no("Reconfigure agent365?", False):
            return

    print_info(
        "Wires Agent 365 into Hermes. Assumes `hermes a365 register --apply` has "
        "already created the blueprint + minted the client secret."
    )
    print_info(
        "Tunnel exposing localhost:3978 to public HTTPS is operator-territory "
        "(cloudflared / devtunnels / etc.); set up before `hermes gateway run`."
    )
    print()

    # 1. Generated config — required, drives detected defaults below.
    default_generated = str(Path.home() / "a365.generated.config.json")
    generated_path = prompt(
        "Path to a365.generated.config.json (emitted by `hermes a365 register`)",
        default=get_env_value("A365_GENERATED_CONFIG_PATH") or default_generated,
    )
    if not generated_path or not Path(generated_path).is_file():
        print_warning(
            f"{generated_path or '(blank)'} not found — "
            "run `hermes a365 register --apply` first, then re-run this wizard."
        )
        return
    save_env_value("A365_GENERATED_CONFIG_PATH", generated_path)

    # Slice 19r-bis (#25): ensure the GA `a365` CLI can find the
    # generated config at its XDG-standard location.
    xdg_result = _ensure_xdg_generated_config_symlink(Path(generated_path))
    if xdg_result["status"] == "created":
        print_success(xdg_result["message"])
    elif xdg_result["status"] == "repaired":
        print_info(xdg_result["message"])
    elif xdg_result["status"] == "skipped_real_file":
        print_warning(xdg_result["message"])
    elif xdg_result["status"] == "error":
        print_warning(
            f"Couldn't ensure XDG symlink: {xdg_result['message']}. "
            f"`a365 publish` may fail unless you manually `ln -s "
            f"{generated_path} {xdg_result['xdg_path']}`."
        )
    # status == "noop" is silent — no action needed.

    try:
        with open(generated_path) as f:
            gen = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print_warning(f"Couldn't parse {generated_path}: {e}")
        return

    detected_app = str(gen.get("agentBlueprintId") or "")
    detected_secret = str(gen.get("agentBlueprintClientSecret") or "")
    detected_endpoint = str(gen.get("messagingEndpoint") or "")

    # 2. Tenant id — prefer az context.
    detected_tenant = ""
    try:
        result = subprocess.run(
            ["az", "account", "show", "--query", "tenantId", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            detected_tenant = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    tenant = prompt(
        "Tenant id (GUID)",
        default=existing_tenant or detected_tenant,
    )
    if not tenant:
        print_warning("Tenant id required — skipping.")
        return
    save_env_value("A365_TENANT_ID", tenant)

    # 3. Blueprint app id — drift-check against detected.
    app = prompt(
        "Blueprint Entra app id (GUID)",
        default=existing_app or detected_app,
    )
    if not app:
        print_warning("App id required — skipping.")
        return
    save_env_value("A365_APP_ID", app)
    if existing_app and detected_app and existing_app != detected_app and app == detected_app:
        print_info(
            f"⚠️  Refreshed A365_APP_ID: {existing_app[:8]}… → {detected_app[:8]}… "
            "(now matches the latest register output; previous value was stale)."
        )

    # 4. Slug — slice 19r-a-bis (#22):
    #    - 1 dir: default to it (existing behaviour).
    #    - >1 dirs without an existing AGENT_IDENTITY: present a
    #      prompt_choice to avoid silently dropping the slug if the
    #      operator hits Enter on a freeform prompt.
    #    - 0 dirs: prompt freeform but re-prompt on blank (up to 3
    #      tries) to avoid silently writing an empty stanza.
    agents_dir = Path.home() / ".hermes" / "agents"
    slug_options = (
        sorted(d.name for d in agents_dir.iterdir() if d.is_dir())
        if agents_dir.is_dir()
        else []
    )
    existing_slug = get_env_value("AGENT_IDENTITY")
    if slug_options:
        print_info(f"Existing per-agent dirs: {', '.join(slug_options)}")

    slug = ""
    if len(slug_options) > 1 and not existing_slug:
        default_idx = 0
        try:
            idx = prompt_choice(
                "Agent slug (per-agent dir under ~/.hermes/agents/)",
                slug_options,
                default=default_idx,
            )
            slug = slug_options[idx]
        except (IndexError, ValueError):
            slug = slug_options[0]
    elif len(slug_options) == 0 and not existing_slug:
        for _attempt in range(3):
            slug = prompt(
                "Agent slug (per-agent dir under ~/.hermes/agents/) — required",
                default="",
            )
            if slug:
                break
            print_warning(
                "Slug is required; an empty value would leave the gateway "
                "platform stanza without a slug, breaking conversation lookup."
            )
        if not slug:
            print_warning(
                "Skipping slug after 3 blank attempts. Re-run the wizard "
                "after creating a per-agent dir or setting AGENT_IDENTITY."
            )
            return
    else:
        slug = prompt(
            "Agent slug (per-agent dir under ~/.hermes/agents/)",
            default=existing_slug or (slug_options[0] if len(slug_options) == 1 else ""),
        )

    if slug:
        try:
            validate_slug(slug)
        except ValueError as e:
            # #103 / M9: AGENT_IDENTITY feeds every agents-dir path join.
            print_warning(
                f"Slug {slug!r} is not path-safe ({e}); not saving it. "
                "Re-run the wizard with a plain slug (letters/digits/hyphens)."
            )
            return
        save_env_value("AGENT_IDENTITY", slug)

    # 5. Bridge port.
    port_raw = prompt(
        "Bridge port",
        default=get_env_value("HERMES_BRIDGE_PORT") or "3978",
    )
    port = 3978
    if port_raw:
        try:
            port = int(port_raw)
            save_env_value("HERMES_BRIDGE_PORT", str(port))
        except ValueError:
            print_warning(f"Invalid port {port_raw!r} — keeping 3978")

    # 6. Blueprint client secret bootstrap.
    print()
    print_info("🔑 Blueprint client secret")
    if detected_secret:
        if prompt_yes_no(
            f"Use secret from {generated_path}? "
            "(writes plaintext to ~/.hermes/.env — keychain-only is slice #19)",
            True,
        ):
            # #110 / CS-002: never add the secret to a permissive .env.
            if _gate_env_secret_write(
                get_env_path(),
                prompt_yes_no=prompt_yes_no,
                print_warning=print_warning,
            ):
                save_env_value("A365_BLUEPRINT_CLIENT_SECRET", detected_secret)
                print_success("Secret bootstrap saved to ~/.hermes/.env")
            else:
                print_info(
                    "Secret NOT saved. Fix the .env permissions (chmod 600) "
                    "and re-run the wizard, or export "
                    "A365_BLUEPRINT_CLIENT_SECRET in the gateway shell."
                )
        else:
            print_info(
                "Skipped. Export A365_BLUEPRINT_CLIENT_SECRET in the gateway "
                "shell manually before `hermes gateway run`."
            )
    else:
        print_warning(
            "agentBlueprintClientSecret is null in generated config — "
            "likely Microsoft#408 on this CLI release. Re-run "
            "`hermes a365 register --apply --auto-recover-secret`."
        )
        manual_secret = prompt(
            "Or paste the 40-char client secret now (skipped if blank)",
            password=True,
        )
        if manual_secret:
            # #110 / CS-002: same gate on the manual-paste branch.
            if _gate_env_secret_write(
                get_env_path(),
                prompt_yes_no=prompt_yes_no,
                print_warning=print_warning,
            ):
                save_env_value("A365_BLUEPRINT_CLIENT_SECRET", manual_secret)
            else:
                print_info(
                    "Secret NOT saved. Fix the .env permissions (chmod 600) "
                    "and re-run the wizard, or export "
                    "A365_BLUEPRINT_CLIENT_SECRET in the gateway shell."
                )

    # 7. Allow-all toggle.
    print()
    print_info("🔒 Access control")
    print_info(
        "Testing: A365_ALLOW_ALL_USERS=true accepts any signed-in tenant user."
    )
    print_info(
        "Production: set A365_ALLOWED_USERS=<csv-of-emails-or-oids> instead."
    )
    allow_all = prompt_yes_no(
        "Allow all users (testing only)?",
        get_env_value("A365_ALLOW_ALL_USERS") == "true",
    )
    if allow_all:
        save_env_value("A365_ALLOW_ALL_USERS", "true")
        save_env_value("A365_ALLOWED_USERS", "")
        print_warning(
            "⚠️  Open access — any signed-in tenant user can DM the bot."
        )
    else:
        save_env_value("A365_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed users (comma-separated emails/oids; blank = deny everyone)",
            default=get_env_value("A365_ALLOWED_USERS") or "",
        )
        save_env_value(
            "A365_ALLOWED_USERS",
            allowed.replace(" ", "") if allowed else "",
        )

    # 8. Patch ~/.hermes/config.yaml — plugins.enabled + platform stanza.
    #
    # We import the private helpers from ``hermes_cli.plugins_cmd`` because
    # ``hermes plugins enable agent365`` exits non-zero for entry-point-
    # discovered plugins (``_plugin_exists`` in v0.13.0 only checks bundled
    # + user dirs; entry-point check is the same gap as the
    # ``hermes plugins list`` filter). Going under the CLI here keeps the
    # wizard one-shot; once upstream Hermes folds entry-point discovery into
    # ``_plugin_exists``, we can switch to invoking the CLI cleanly.
    from hermes_cli.config import load_config, save_config
    from hermes_cli.plugins_cmd import _get_enabled_set, _save_enabled_set

    enabled = _get_enabled_set()
    if "agent365" not in enabled:
        enabled.add("agent365")
        _save_enabled_set(enabled)
        print_success("Added agent365 to plugins.enabled in ~/.hermes/config.yaml")

    # Slice 19r-a-bis (#22): only call save_config when the stanza
    # actually changes. hermes_cli.config.save_config expands every
    # implicit-default key on round-trip (~270-line diff per run);
    # skipping the write when nothing meaningful changed keeps
    # ~/.hermes/config.yaml git-reviewable.
    import copy as _copy

    config = load_config()
    pre_snapshot = _copy.deepcopy(config)
    gateway = config.setdefault("gateway", {})
    platforms = gateway.setdefault("platforms", {})
    block = platforms.setdefault("agent365", {})
    block["enabled"] = True
    extra = block.setdefault("extra", {})
    if slug:
        extra["slug"] = slug
    extra["port"] = port
    extra.setdefault("host", "127.0.0.1")
    extra["generated_config_path"] = generated_path
    if config != pre_snapshot:
        save_config(config)
        print_success("Wrote gateway.platforms.agent365 stanza")
    else:
        print_info(
            "gateway.platforms.agent365 stanza unchanged — skipping config.yaml write."
        )

    print()
    print_success("Agent 365 configuration saved.")
    print_info("Next steps:")
    if detected_endpoint:
        print_info(
            f"  - Messaging endpoint already set: {detected_endpoint}. "
            "If your tunnel URL has changed, re-run "
            "`hermes a365 activity-bridge update-endpoint --url <new> --apply`."
        )
    else:
        print_info(
            "  - Start your tunnel (cloudflared / devtunnels / ngrok / etc.) "
            "and run `hermes a365 activity-bridge update-endpoint "
            "--agent-name '<display>' --url https://<tunnel>/api/messages --apply`."
        )
    print_info(
        "  - Source the per-agent .env into the gateway shell, export "
        "A365_BLUEPRINT_CLIENT_SECRET, then `hermes gateway run`."
    )


def register(ctx: Any) -> None:
    """Plugin entry point — invoked by the Hermes plugin system at
    gateway startup."""
    ctx.register_platform(
        name="agent365",
        label="Microsoft Agent 365",
        adapter_factory=lambda cfg: Agent365Adapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        setup_fn=interactive_setup,
        required_env=["A365_TENANT_ID", "A365_APP_ID"],
        install_hint="pip install 'hermes-a365[bridge]'",
        allowed_users_env="A365_ALLOWED_USERS",
        allow_all_env="A365_ALLOW_ALL_USERS",
        max_message_length=4000,
        emoji="🤝",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are interacting via Microsoft Agent 365 (Teams 1:1, "
            "M365 Copilot Chat, or Outlook depending on the surface). "
            "Reply within ~10 seconds — longer reasoning needs the "
            "proactive reply pattern. Adaptive Cards render natively; "
            "plain text is fine for short responses. Avoid heavy "
            "markdown — Teams renders only a subset."
        ),
    )
