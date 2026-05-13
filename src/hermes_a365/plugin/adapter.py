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
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

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
from gateway.session import SessionSource  # noqa: E402

# Plugin-local imports — these don't depend on the Hermes harness.
from .conversations import ConversationRef, ConversationRegistry  # noqa: E402

# Bridge helpers are imported lazily inside methods so missing optional
# extras (e.g. fastapi for `activity-bridge serve`) produce a clear runtime
# error rather than blowing up at gateway-load time.

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


def _strip_streaming_cursor(text: str) -> str:
    """Remove a trailing cursor glyph that Hermes' stream consumer
    appends to intermediate chunks. Idempotent + no-op when text
    doesn't end with one."""
    for cursor in _STREAMING_CURSORS_TO_STRIP:
        if cursor and text.endswith(cursor):
            return text[: -len(cursor)]
    return text


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
        self.slug: str = str(extra.get("slug") or os.getenv("AGENT_IDENTITY") or "")
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

        # Lazily-built runtime objects (populated in connect()).
        self._http_client: Any = None
        self._jwks_cache: Any = None
        self._idempotency_cache: Any = None
        self._fmi_cache: Any = None
        self._user_cache: Any = None
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
            # Slice 19j — serviceUrl gate before anything else.
            service_url = activity.get("serviceUrl") or ""
            trusted_suffixes = bridge.DEFAULT_TRUSTED_SERVICE_URL_HOST_SUFFIXES
            if not trusted_suffixes:
                raise HTTPException(
                    status_code=403,
                    detail="trusted_service_url_suffixes is empty — refusing to "
                    "process inbound activity. This is a config bug.",
                )
            if not bridge._is_trusted_service_url(service_url, trusted_suffixes):
                raise HTTPException(
                    status_code=403,
                    detail=f"untrusted serviceUrl: {service_url!r}",
                )

            # Slice 19f — JWT validation against AAD-v2 + azp allowlist.
            if not authorization or not authorization.lower().startswith("bearer "):
                raise HTTPException(status_code=401, detail="missing bearer token")
            token = authorization.split(None, 1)[1]
            try:
                await bridge.validate_inbound_jwt(
                    token=token,
                    tenant_id=self.tenant_id,
                    expected_app_id=self.blueprint_app_id,
                    azp_allowlist=bridge.DEFAULT_INBOUND_AZP_ALLOWLIST,
                    client=self._http_client,
                    cache=self._jwks_cache,
                )
            except bridge.JwtValidationError as e:
                raise HTTPException(status_code=403, detail=str(e)) from e

            # Slice 19i — dedupe (conversationId, activityId).
            delivery_id = bridge._activity_delivery_id(activity)
            if delivery_id is not None and self._idempotency_cache.is_duplicate(
                delivery_id
            ):
                return JSONResponse({"status": "duplicate"})

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
                self._conversations.upsert(ref)
                self._persist_conversations()

            # Build event + dispatch through Hermes' loop.
            event = self._activity_to_event(activity)
            await self.handle_message(event)
            return JSONResponse({"status": "dispatched"})

        return app

    def _activity_to_event(self, activity: dict[str, Any]) -> MessageEvent:
        conv = activity.get("conversation") or {}
        sender = activity.get("from") or {}
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
        return MessageEvent(
            text=str(activity.get("text") or ""),
            message_type=MessageType.TEXT,
            source=source,
            raw_message=activity,
            message_id=str(activity.get("id") or ""),
            timestamp=datetime.now(),
        )

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        """Build the bridge runtime + start uvicorn on `self.port`."""
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
        self._mark_disconnected()

    # ── Outbound ──────────────────────────────────────────────────────────

    def _persist_conversations(self) -> None:
        """Best-effort save of the registry. Persistence failures are
        logged but never block message processing — the in-memory
        copy still works for this run."""
        try:
            self._conversations.save(self._conversations_path)
        except OSError as e:
            logger.warning(
                "agent365 conversations: save failed for %s: %s",
                self._conversations_path,
                e,
            )

    def _cached_inbound_for(self, chat_id: str) -> dict[str, Any] | None:
        """Return the most recent inbound activity for ``chat_id``,
        sourced from the registry's ``raw`` field. Slice 19o's
        registry is the only authoritative source; legacy callers
        should not reach into ``_chat_contexts`` (gone)."""
        ref = self._conversations.get(chat_id)
        if ref is None or not ref.raw:
            return None
        return ref.raw

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
                "path": "A" | "unknown",      # convenience tag for callers
            }

        Path-tagging rule: an entry is **Path A** when the cached
        inbound's ``recipient`` carries both ``agenticAppId`` and
        ``agenticUserId`` (the Microsoft A365 agentic-user routing
        signal). Anything else is tagged ``"unknown"`` because Path B
        (Custom Engine Agent via Azure Bot Service) uses a different
        token chain that slice 19x-b deliberately doesn't ship — the
        send-side fallback surfaces a deferred-error rather than 401.

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
        path_tag = "A" if (agentic_app_id and agentic_user_id) else "unknown"

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
        durable registry (slice 19o). When the registry has no entry
        for this chat — e.g. a proactive cron delivery against a
        conversation we've never seen — returns a failure SendResult.

        Slice 19s-bis: in personal chats with no active stream for the
        conversation, ``send()`` emits a BF streaming-start activity
        (typing + streaminfo + streamSequence:1) and captures the
        returned ``streamId``. Subsequent ``edit_message`` calls
        continue that same stream rather than creating a separate one.
        This satisfies Microsoft's "one streaming sequence per user
        turn" rule (custom-engine-agents doc) and gives a single
        growing bubble per Hermes segment.

        Fallback to a non-streaming ``message`` activity when:
        - The conversation already has an active stream (tool progress
          or content streaming claimed it first; only one stream
          per turn is allowed).
        - ``chat_type != "personal"`` (BF streaming is DM-only).
        - The streaming-start POST itself fails.
        """
        bridge = _import_bridge()
        inbound = self._cached_inbound_for(chat_id)
        if not inbound:
            msg = f"no cached inbound for chat_id={chat_id!r} — cannot reply"
            logger.error("agent365 send: %s", msg)
            return SendResult(success=False, error=msg)

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
        # fires. We auto-close any stale stream when a new ``send()``
        # arrives for the same conversation.
        if chat_id in self._active_stream_by_chat:
            stale_msg_id = self._active_stream_by_chat[chat_id]
            await self._auto_finalize_stale_stream(
                chat_id=chat_id, message_id=stale_msg_id, inbound=inbound,
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

        reply = bridge.render_reply_activity(inbound, {"text": content})
        try:
            await bridge.send_reply(
                inbound=inbound,
                reply=reply,
                cfg=self._bridge_cfg,
                client=self._http_client,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
            )
        except Exception as e:
            logger.error("agent365 send_reply failed: %s", e)
            return SendResult(success=False, error=str(e))
        return SendResult(success=True, message_id=str(inbound.get("id") or ""))

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
        url = f"{service_url}/v3/conversations/{conv_id}/activities"
        try:
            token = await bridge.acquire_outbound_token(
                client=self._http_client,
                cfg=self._bridge_cfg,
                activity=inbound,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
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
        clean_content = _strip_streaming_cursor(content)
        self._streams[bf_stream_id] = {
            "bf_stream_id": bf_stream_id,
            "sequence": 1,
            "last_emit_ts": loop.time(),
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
        url = f"{service_url}/v3/conversations/{conv_id}/activities"
        token = await bridge.acquire_outbound_token(
            client=self._http_client,
            cfg=self._bridge_cfg,
            activity=inbound,
            fmi_cache=self._fmi_cache,
            user_cache=self._user_cache,
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
        if self._http_client is None or self._bridge_cfg is None:
            msg = "agent365 send_image: adapter not connected"
            logger.error(msg)
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
            )
        except Exception as e:
            logger.error("agent365 send_image failed: %s", e)
            return SendResult(success=False, error=str(e))
        return SendResult(success=True, message_id=str(inbound.get("id") or ""))

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

        Returns ``SendResult(success=False, ...)`` and falls back to
        ``send()`` on:
        - non-personal chat types (BF streaming is DM-only),
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

        # DM-only: BF streaming-ux doc:
        # "Streaming bot message is available only for one-on-one chats."
        conv = inbound.get("conversation") or {}
        if str(conv.get("conversationType") or "") != "personal":
            return SendResult(
                success=False,
                error="streaming requires personal chat",
            )

        if self._http_client is None or self._bridge_cfg is None:
            return SendResult(
                success=False,
                error="agent365 edit_message: adapter not connected",
            )

        # Slice 19s-bis follow-up — drop a recently-finalized message_id
        # follow-up call as a successful no-op. See ``_recently_finalized``
        # docstring for the Hermes stream-consumer quirk this guards.
        loop_now = asyncio.get_event_loop().time()
        self._prune_recently_finalized(loop_now)
        if message_id in self._recently_finalized:
            return SendResult(success=True, message_id="")

        state = self._streams.get(message_id)
        is_first = state is None
        if state is None:
            state = {"bf_stream_id": None, "sequence": 0, "last_emit_ts": 0.0}
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
        service_url = str(inbound.get("serviceUrl") or "").rstrip("/")
        conv_id = conv.get("id")
        if not service_url or not conv_id:
            return SendResult(
                success=False,
                error="agent365 edit_message: serviceUrl or conversation.id missing",
            )
        url = f"{service_url}/v3/conversations/{conv_id}/activities"

        try:
            token = await bridge.acquire_outbound_token(
                client=self._http_client,
                cfg=self._bridge_cfg,
                activity=inbound,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
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
    ) -> None:
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
            return

        bf_stream_id = state["bf_stream_id"]
        conv = inbound.get("conversation") or {}
        service_url = str(inbound.get("serviceUrl") or "").rstrip("/")
        conv_id = conv.get("id")
        if not service_url or not conv_id:
            self._drop_stream_state(chat_id, message_id)
            return

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
        url = f"{service_url}/v3/conversations/{conv_id}/activities"
        try:
            bridge = _import_bridge()
            token = await bridge.acquire_outbound_token(
                client=self._http_client,
                cfg=self._bridge_cfg,
                activity=inbound,
                fmi_cache=self._fmi_cache,
                user_cache=self._user_cache,
            )
            resp = await self._http_client.post(
                url,
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            logger.info(
                "agent365 auto-finalize stale stream %s: status=%s",
                bf_stream_id, resp.status_code,
            )
        except Exception as e:
            logger.warning(
                "agent365 auto-finalize stale stream %s failed: %s",
                bf_stream_id, e,
            )
        finally:
            self._drop_stream_state(chat_id, message_id)
            loop_now = asyncio.get_event_loop().time()
            self._recently_finalized[message_id] = loop_now

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
    ``extra``."""
    extra = getattr(config, "extra", {}) or {}
    tenant = os.getenv("A365_TENANT_ID") or extra.get("tenant_id")
    app = os.getenv("A365_APP_ID") or extra.get("app_id")
    return bool(tenant and app)


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
            save_env_value("A365_BLUEPRINT_CLIENT_SECRET", detected_secret)
            print_success("Secret bootstrap saved to ~/.hermes/.env")
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
            save_env_value("A365_BLUEPRINT_CLIENT_SECRET", manual_secret)

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
