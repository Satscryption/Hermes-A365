"""Hermes gateway platform adapter — Microsoft Agent 365.

Slice 19m skeleton: PLUGIN.yaml + adapter.py + register entry point
matching the upstream Hermes plugin contract documented at
``gateway/platforms/ADDING_A_PLATFORM.md``. Reference plugin:
``plugins/platforms/irc/`` (IRC) in the upstream Hermes tree.

This module is **only the registration shape** for now — every
inbound/outbound method is a stub that logs ``TODO 19n`` and either
no-ops or returns ``SendResult(success=True)``. The real logic
lands in slice 19n, which ports the FastAPI webhook handlers,
JWT validation, idempotency dedupe, serviceUrl gate, and outbound
user-FIC chain from ``scripts/activity_bridge.py`` into this
adapter's lifecycle methods.

Configuration via ``config.yaml``::

    gateway:
      platforms:
        agent365:
          enabled: true
          extra:
            slug: inbox-helper
            port: 3978
            webhook_url: http://127.0.0.1:9090/respond  # operator-side responder

Or via environment variables (A365 wrapper already populates these
in ``~/.hermes/.env`` and the per-agent ``~/.hermes/agents/<slug>/.env``):

- ``A365_TENANT_ID`` — tenant the bridge serves
- ``A365_APP_ID`` — blueprint app id
- ``AA_INSTANCE_ID`` — agent instance id
- ``HERMES_BRIDGE_PORT`` — port to bind the webhook server (default 3978)

The plugin imports the Hermes harness's ``BasePlatformAdapter`` at
module level. When running outside a Hermes process (CI / unit tests
in this repo), the test fixture ``tests/test_agent365_plugin.py``
inserts stub modules into ``sys.modules`` so the import resolves
without requiring the harness on PYTHONPATH.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hermes harness imports.
# Live at gateway/platforms/base.py and gateway/config.py in the Hermes
# repo (~/.hermes/hermes-agent/). Plugin discovery puts them on sys.path
# at gateway-init time. Tests stub these modules.
# ---------------------------------------------------------------------------

from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platforms.base import (  # noqa: E402
    BasePlatformAdapter,
    SendResult,
)

# Slice 19m / round-4: the bridge already has this much of the runtime.
# Listing here so the 19n port is mechanical.
_PORTED_FROM_BRIDGE = (
    "scripts/activity_bridge.py: serve mode (FastAPI app, JWT validation, "
    "idempotency dedupe, serviceUrl gate, user-FIC outbound chain)"
)

# Default port matches what `bridge serve` uses today.
_DEFAULT_PORT = 3978


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class Agent365Adapter(BasePlatformAdapter):
    """Hermes platform adapter for Microsoft Agent 365 surfaces.

    The skeleton below sets up the registration plumbing so the
    plugin loads at gateway startup. Every method that touches the
    network is a stub until slice 19n.
    """

    # A365 / Teams turn budget — the BF connector times out after
    # ~15s so practical interactive turns must finish under that.
    # Longer turns will need the proactive reply pattern (#4).
    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config: PlatformConfig, **_kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform("agent365"))

        extra = getattr(config, "extra", {}) or {}

        # Per-agent slug — drives ~/.hermes/agents/<slug>/ lookups
        # the same way the bridge does today.
        self.slug: str = str(extra.get("slug") or os.getenv("AGENT_IDENTITY") or "")

        # Bridge webhook port + tenant context.
        self.port: int = int(
            os.getenv("HERMES_BRIDGE_PORT") or extra.get("port") or _DEFAULT_PORT
        )
        self.tenant_id: str = os.getenv("A365_TENANT_ID") or str(extra.get("tenant_id") or "")
        self.blueprint_app_id: str = os.getenv("A365_APP_ID") or str(extra.get("app_id") or "")

        # Operator-side webhook the bridge forwards normalized
        # activities to (slice 19n maps inbound activities onto
        # ``self.handle_message(event)`` instead, so this becomes
        # vestigial — kept on the skeleton so PLUGIN.yaml-only
        # operators can still point at a Tier-1 reference responder).
        self.webhook_url: str = (
            os.getenv("HERMES_BRIDGE_WEBHOOK")
            or str(extra.get("webhook_url") or "")
        )

    @property
    def name(self) -> str:
        return "Agent 365"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        """TODO 19n: stand up FastAPI on ``self.port``, register the
        ``/api/messages`` route, prime the JWKS / idempotency / FMI
        caches, and call ``self._mark_connected()`` once Uvicorn is
        accepting connections."""
        logger.warning(
            "agent365 adapter is a 19m skeleton — connect() is a no-op; "
            "runtime logic lands in 19n (port from %s)",
            _PORTED_FROM_BRIDGE,
        )
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """TODO 19n: stop Uvicorn, cancel the FastAPI task, drop the
        bridge.pid file, flush log handles."""
        self._mark_disconnected()

    # ── Outbound ──────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """TODO 19n: render ``content`` as a reply activity, mint the
        outbound user-FIC bearer (port from
        ``acquire_outbound_token`` in the bridge), and POST to
        ``{serviceUrl}/v3/conversations/{conv}/activities/{activity}``.
        ``metadata`` is expected to carry ``service_url``, ``conversation_id``,
        and ``activity_id`` so the skeleton can stay platform-side
        without reaching into Hermes internals."""
        logger.warning(
            "agent365 adapter send() — 19m stub. chat_id=%s content_len=%d",
            chat_id,
            len(content or ""),
        )
        return SendResult(success=True, message_id=None)

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> None:
        """TODO 19n: send a BF ``typing`` activity. A365 surfaces
        render this as the trailing-dots indicator."""
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """TODO 19n: build an Adaptive Card with an Image element
        and reuse ``send()``'s outbound POST."""
        logger.warning(
            "agent365 adapter send_image() — 19m stub. chat_id=%s url=%s",
            chat_id,
            image_url,
        )
        return SendResult(success=True, message_id=None)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """TODO 19n: surface BF ``conversation`` shape — at minimum
        ``{name, type, chat_id}``. Until 19o ships the session
        table, ``name`` is the same as ``chat_id``."""
        return {"name": chat_id, "type": "personal", "chat_id": chat_id}


# ---------------------------------------------------------------------------
# Plugin entry points
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """Probe for the bridge runtime extras (FastAPI, httpx, pyjwt[crypto]).

    Mirrors ``scripts/activity_bridge.py``'s lazy imports so an
    operator missing the extras gets a clean ``False`` rather than
    an import error mid-startup.
    """
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        import jwt  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config: Any) -> bool:
    """Plugin loader calls this before ``adapter_factory``. We accept
    any config that can populate ``A365_TENANT_ID`` + ``A365_APP_ID``
    either via env or ``extra``. Slice 19n tightens this once the
    runtime needs land."""
    extra = getattr(config, "extra", {}) or {}
    tenant = os.getenv("A365_TENANT_ID") or extra.get("tenant_id")
    app = os.getenv("A365_APP_ID") or extra.get("app_id")
    return bool(tenant and app)


def is_connected() -> bool:
    """Best-effort liveness probe. Slice 19n will plumb this through
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
        # Per-platform user authorization env vars. Slice 19n will
        # actually wire these against the inbound activity's `from`
        # field; for now they're declarative.
        allowed_users_env="A365_ALLOWED_USERS",
        allow_all_env="A365_ALLOW_ALL_USERS",
        # A365 surfaces (Teams chat) typically tolerate ~4k chars
        # before the BF connector truncates. Conservative.
        max_message_length=4000,
        # Display
        emoji="🤝",
        # No phone numbers in identifiers; agentic users use
        # Entra-style GUIDs that aren't PII per se.
        pii_safe=True,
        allow_update_command=True,
        # System-prompt hint — informs the agent about A365 surface
        # specifics. Mirrors the upstream Hermes prompt-builder
        # pattern (``msteams`` / ``signal`` etc.).
        platform_hint=(
            "You are interacting via Microsoft Agent 365 (Teams 1:1, "
            "M365 Copilot Chat, or Outlook depending on the surface). "
            "Reply within ~10 seconds — longer reasoning needs the "
            "proactive reply pattern. Adaptive Cards render natively; "
            "plain text is fine for short responses. Avoid heavy "
            "markdown — Teams renders only a subset."
        ),
    )
