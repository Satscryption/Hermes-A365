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


def interactive_setup() -> None:
    """``hermes gateway setup --platform agent365`` wizard.

    Assumes ``hermes a365 register --apply --m365 --aiteammate`` has
    already been run (the blueprint Entra app + permissions are set
    up). This wizard wires the platform side: bootstraps env vars in
    ``~/.hermes/.env``, ensures ``agent365`` is in ``plugins.enabled``
    in ``~/.hermes/config.yaml``, and writes the
    ``gateway.platforms.agent365`` block.

    Idempotent — re-running detects existing values and prompts
    update-vs-keep.

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
        prompt_yes_no,
        save_env_value,
    )

    print_header("Microsoft Agent 365")

    existing_tenant = get_env_value("A365_TENANT_ID")
    existing_app = get_env_value("A365_APP_ID")
    if existing_tenant and existing_app:
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

    # 4. Slug — default to single per-agent dir if there's only one.
    agents_dir = Path.home() / ".hermes" / "agents"
    slug_options = (
        sorted(d.name for d in agents_dir.iterdir() if d.is_dir())
        if agents_dir.is_dir()
        else []
    )
    existing_slug = get_env_value("AGENT_IDENTITY")
    if slug_options:
        print_info(f"Existing per-agent dirs: {', '.join(slug_options)}")
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

    config = load_config()
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
    save_config(config)
    print_success("Wrote gateway.platforms.agent365 stanza")

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
