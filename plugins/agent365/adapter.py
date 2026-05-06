"""Hermes gateway platform adapter — Microsoft Agent 365.

Slice 19n ports the bridge runtime under ``Agent365Adapter``: the
FastAPI ``/api/messages`` route, JWT validation, idempotency dedupe,
serviceUrl host-suffix gate, and outbound user-FIC chain that have
been baking in ``scripts/activity_bridge.py`` since slices 19a-19j
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
``scripts/activity_bridge.py`` rather than copy-pasting ~600 lines —
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
import sys
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

# ---------------------------------------------------------------------------
# Make scripts/ importable so we can reuse the bridge helpers.
# When the plugin is symlinked from <repo>/plugins/agent365 → ~/.hermes/
# /plugins/agent365, the symlink resolves to the repo path and scripts/
# is a sibling. Standalone-installed plugins (without scripts/ next to
# them) won't load these helpers; vendoring the bridge module into the
# plugin is queued for whenever this ships outside a checkout.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if _SCRIPTS_DIR.is_dir() and str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Bridge helpers — imported lazily inside methods so missing extras /
# missing scripts dir at import time produce a clear runtime error
# rather than blowing up at gateway-load time.

_DEFAULT_PORT = 3978


def _import_bridge() -> Any:
    """Import the bridge module on demand. Returns the module object."""
    import activity_bridge  # type: ignore[import-not-found]

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

        # Slice 19n state. ``_chat_contexts`` keeps the most recent
        # inbound activity per chat_id so ``send()`` can reach the
        # serviceUrl + conversation + activity_id without a full
        # session table (slice 19o ships the durable version).
        self._chat_contexts: dict[str, dict[str, Any]] = {}

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

            activity_type = activity.get("type", "message")
            if activity_type in ("conversationUpdate", "typing", "endOfConversation"):
                # Channel-control flows — ack and bail. Not interesting
                # to the agent loop.
                return JSONResponse({"status": "acked"})

            # Stash for outbound lookup. ``send()`` reads serviceUrl /
            # conversation.id / id from this dict.
            chat_id = (activity.get("conversation") or {}).get("id") or ""
            if chat_id:
                self._chat_contexts[chat_id] = activity

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

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Render `content` as a reply Activity and POST via serviceUrl.

        Looks up the most recent inbound activity for `chat_id` from
        ``self._chat_contexts`` to recover serviceUrl / conversation /
        activity-id. If no inbound has been seen for this chat (e.g.
        proactive cron delivery), returns a failure SendResult — that
        flow is #4's territory and lands in slice 19o+'s session table.
        """
        bridge = _import_bridge()
        inbound = self._chat_contexts.get(chat_id)
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

    async def send_typing(
        self, chat_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """TODO 19o: send a BF `typing` activity to the same conversation.
        For now this is a no-op so the gateway's typing-indicator pulse
        doesn't error out."""
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """TODO 19o: render an Adaptive Card with an Image element +
        caption, reuse send()'s outbound POST."""
        logger.warning(
            "agent365 send_image() is a 19m stub — slice 19o adds the "
            "Adaptive Card image renderer. chat_id=%s url=%s",
            chat_id,
            image_url,
        )
        return SendResult(success=True, message_id=None)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Return chat metadata. Slice 19o resolves the display name
        via the cached inbound activity's `conversation.name`."""
        cached = self._chat_contexts.get(chat_id)
        chat_type = "personal"
        chat_name = chat_id
        if cached:
            conv = cached.get("conversation") or {}
            conv_type = str(conv.get("conversationType") or "personal")
            chat_type = (
                "personal"
                if conv_type == "personal"
                else ("group" if conv_type == "groupChat" else "channel")
            )
            chat_name = str(conv.get("name") or chat_id)
        return {"name": chat_name, "type": chat_type, "chat_id": chat_id}


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


def is_connected() -> bool:
    """Best-effort liveness probe. Slice 19o will plumb this through
    the actual adapter instance."""
    return True


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
        required_env=["A365_TENANT_ID", "A365_APP_ID"],
        install_hint="uv sync --extra bridge",
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
