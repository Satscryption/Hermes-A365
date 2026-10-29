"""Hand-maintained public-surface metadata for the v1.0 freeze (#193).

Values here are merged with live extraction in ``scripts/render_surface.py``.
Owner decisions D1-D8 were accepted on 2026-10-29 (see ``ACCEPTED_OWNER_DECISIONS``).
The D1-D8 constants below remain the single flip points if an decision is reopened.
"""

from __future__ import annotations

from dataclasses import fields as _dataclass_fields
from typing import Any, Final, Literal

from hermes_a365.a365_config import A365Config

# ---------------------------------------------------------------------------
# Owner decisions (accepted 2026-10-29 — constants match the accepted option)
# ---------------------------------------------------------------------------

OWNER_DECISIONS_STATUS: Final[str] = "accepted"

ACCEPTED_OWNER_DECISIONS: Final[dict[str, str]] = {
    "D1": "A — activity-bridge serve is a supported secondary runtime at 1.0",
    "D2": "A — default CEA publish scopes remain copilot, personal, team",
    "D3": "A — freeze the task/fetch placeholder card (Hermes / Request received.)",
    "D4": "A — security fixes for the latest 1.x minor only",
    "D5": (
        "Removed by #267: HERMES_OTLP_ENDPOINT is not part of the 1.0 public env "
        "(deprecated literals may remain in source for migration warnings only)"
    ),
    "D6": "B — additive auto-stamps only; keep bot-service and publish confirm gates",
    "D7": "B — do not ship py.typed / Typing :: Typed",
    "D8": "B — public python -m hermes_a365.keychain; hermes_responder reference-only",
}

# D1 — activity-bridge serve at 1.0 (accepted A)
D1_SERVE_SUPPORTED: Final[bool] = True  # A=True supported; B=False present-but-unsupported

# D2 — default CEA publish scopes include team (accepted A)
D2_CEA_SCOPES: Final[tuple[str, ...]] = ("copilot", "personal", "team")  # A; B drops team

# D3 — task/fetch placeholder card (accepted A)
D3_TASK_FETCH_CARD_FROZEN: Final[bool] = True  # A=True keep card; B=False remove before snapshot

# D4 — 1.x security support window (accepted A; SECURITY.md + SURFACE.md)
D4_SECURITY_LATEST_1X_MINOR_ONLY: Final[bool] = True  # A=True; B=False latest two minors

# D5 — removed from public env by #267 (merged on this branch).

# D6 — sidecar auto-migration vs confirm gates (accepted B; slice 2 implements)
D6_AUTO_MIGRATE_ADDITIVE_ONLY: Final[bool] = True  # B=True additive-only + confirm gates

# D7 — PEP 561 py.typed marker (accepted B)
D7_SHIP_PY_TYPED: Final[bool] = False  # B=False omit; A=True add py.typed

# D8 — python -m entry modules (accepted B)
D8_KEYCHAIN_PUBLIC: Final[bool] = True  # B=True public CLI
D8_RESPONDER_REFERENCE_ONLY: Final[bool] = True  # B=True reference, not product runtime

# ---------------------------------------------------------------------------
# Env classification (every A365_/HERMES_ string literal in src/hermes_a365)
# ---------------------------------------------------------------------------

EnvClass = Literal["public", "internal"]

ENV_CLASSIFICATION: dict[str, EnvClass] = {
    "A365_AGENT_IDENTITY_IDS": "public",
    "A365_ALLOWED_USERS": "public",
    "A365_ALLOW_ALL_USERS": "public",
    "A365_APP_ID": "public",
    "A365_BF_APP_ID": "public",
    "A365_BF_CLIENT_SECRET": "public",
    "A365_BLUEPRINT_CLIENT_SECRET": "public",
    "A365_CLIENT_APP_NAME": "public",
    "A365_CONVERSATIONS_PATH": "public",
    "A365_FEEDBACK_LOOP": "public",
    "A365_FILE_HOST_ALLOWLIST": "public",
    "A365_GENERATED_CONFIG_PATH": "public",
    "A365_HANDOFF_LINK": "public",
    "A365_TENANT_ID": "public",
    "HERMES_A365_ALLOW_INSECURE_MACOS_KEYCHAIN_CLI": "public",
    "HERMES_A365_COPILOT_FIRST_TURN_MIN_STREAM_AGE_SEC": "public",
    "HERMES_A365_COPILOT_MIN_STREAM_AGE_BEFORE_FINAL_SEC": "public",
    "HERMES_A365_COPILOT_SHORT_TURN_MAX_DURATION_SEC": "public",
    "HERMES_BRIDGE_PORT": "public",
    "HERMES_BRIDGE_WEBHOOK": "public",
    "HERMES_GATEWAY_PROFILE": "public",
    "HERMES_HOME": "public",
    "HERMES_OTLP_ENDPOINT": "internal",
    "HERMES_PROFILE": "public",
    "HERMES_RESPONDER_HISTORY_TOKEN": "public",
}

# Per-agent .env keys written by instance create (not matched by the A365_/HERMES_ scanner).
INSTANCE_ENV_PUBLIC_KEYS: Final[tuple[str, ...]] = (
    "A365_APP_ID",
    "A365_BF_APP_ID",
    "A365_BF_CLIENT_SECRET",
    "A365_TENANT_ID",
    "AA_INSTANCE_ID",
    "AGENT_IDENTITY",
    "BUSINESS_HOURS_END",
    "BUSINESS_HOURS_START",
    "BUSINESS_HOURS_TZ",
    "OWNER",
    "OWNER_AAD_ID",
)

# ---------------------------------------------------------------------------
# Gateway platforms.agent365.extra keys
# ---------------------------------------------------------------------------

GATEWAY_EXTRA_KEYS: Final[tuple[str, ...]] = (
    "slug",
    "port",
    "host",
    "tenant_id",
    "app_id",
    "blueprint_client_secret",
    "bf_app_id",
    "bf_client_secret",
    "generated_config_path",
    "conversations_path",
    "conversations_prune_max_age_days",
    "idempotency_max_entries",
    "file_host_allowlist",
    "agent_identity_ids",
    "notice_delivery",
    "reactions_dispatch_agent_turn",
    "group_sessions_per_user",
    "thread_sessions_per_user",
)

A365_CONFIG_FIELDS: Final[tuple[str, ...]] = tuple(
    f.name for f in _dataclass_fields(A365Config) if f.name != "extra"
)

PLUGIN_REQUIRES_ENV: Final[tuple[str, ...]] = (
    "A365_TENANT_ID",
    "A365_APP_ID",
    "AA_INSTANCE_ID",
    "HERMES_BRIDGE_PORT",
)

# ---------------------------------------------------------------------------
# Sidecar inventory
# ---------------------------------------------------------------------------

HERMES_OWNED_SIDECARS: Final[tuple[dict[str, Any], ...]] = (
    {
        "path": "a365.bot-service.config.json",
        "location": "cwd",
        "schema_version_write": 2,
        "schema_version_read": (1, 2),
    },
    {
        "path": "~/.hermes/agents/<slug>/copilot-chat-publish.json",
        "schema_version_write": 2,
        "schema_version_read": (1, 2),
    },
    {
        "path": "~/.hermes/agents/<slug>/conversations.json",
        "schema_version_write": 1,
        "schema_version_read": (1,),
        "missing_version_means": 1,
        "on_disk_version_field": "schema",
    },
)

UPSTREAM_SIDECARS: Final[tuple[str, ...]] = (
    "a365.config.json",
    "a365.generated.config.json",
)

NOT_SCHEMA_ARTIFACTS: Final[tuple[str, ...]] = (
    "bridge.log",
    "responder.log",
    "media cache",
    "keychain",
    ".env",
)

# ---------------------------------------------------------------------------
# Python public adapter API (methods/properties on Agent365Adapter)
# ---------------------------------------------------------------------------

ADAPTER_PUBLIC_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "message_len_fn",
        "streaming_overflow_limit",
        "build_app",
        "connect",
        "disconnect",
        "prune_conversations",
        "send",
        "on_processing_start",
        "on_processing_complete",
        "send_private_notice",
        "send_or_update_status",
        "send_typing",
        "send_image",
        "send_document",
        "send_image_file",
        "send_exec_approval",
        "send_slash_confirm",
        "send_clarify",
        "edit_message",
        "get_chat_info",
    }
)

KEYCHAIN_CLI_SUBCOMMANDS: Final[tuple[str, ...]] = ("store", "get", "delete")
KEYCHAIN_CLI_OPTIONS: Final[tuple[str, ...]] = (
    "--app-id",
    "--output-fd",
    "--secret",
    "--tenant",
)

# ---------------------------------------------------------------------------
# HTTP product routes (gateway build_app + activity-bridge serve)
# ---------------------------------------------------------------------------

PRODUCT_HTTP_ROUTES: Final[tuple[dict[str, str], ...]] = (
    {"method": "POST", "path": "/api/messages"},
    {"method": "GET", "path": "/healthz"},
)

# ---------------------------------------------------------------------------
# Invoke wire names (contractual)
# ---------------------------------------------------------------------------

INVOKE_REGISTRY_PUBLIC: Final[tuple[str, ...]] = ("task/fetch",)

INVOKE_ADAPTER_PUBLIC: Final[tuple[str, ...]] = (
    "handoff/action",
    "message/fetchTask",
    "message/submitAction",
)

TASK_FETCH_CARD: Final[dict[str, Any]] = {
    "adaptive_card_version": "1.5",
    "title": "Hermes",
    "body_text": "Request received.",
}

# ---------------------------------------------------------------------------
# CLI exit codes (as implemented — not the simplified 0/1/2 walk)
# ---------------------------------------------------------------------------

EXIT_CODES: Final[dict[str, Any]] = {
    "missing_verb_or_usage": 2,
    "register_aiteammate": 2,
    "doctor": {"ok": 0, "warn": 1, "error": 2},
    "activity_bridge_verify": {"ok": 0, "warn": 1, "error": 2},
    "status": {"ok": 0, "partial": 1, "broken": 2, "uninitialized": 3},
    "license": (0, 2),
    "instance_create": (0, 2),
    "consent": {"success": 0, "failed_or_partial": 1, "refusal": 2},
    "register": {"success": 0, "failed_or_partial": 1, "refusal": 2},
    "cleanup": {"success": 0, "failed_or_partial": 1, "refusal": 2},
    "bot_service": {"success": 0, "failed_or_partial": 1, "refusal": 2},
    "bot_service_verify": (0, 1, 2),
    "publish_run": (0, 2),
}

# ---------------------------------------------------------------------------
# Sidecar migration policy (verbatim for SURFACE.md — slice 2 implements)
# ---------------------------------------------------------------------------

SIDECAR_MIGRATION_TODAY: Final[str] = (
    "Today (slice 1) `conversations.json` is persisted with a `schema` field only "
    "(`conversations.py`); the dual-key stamp lands in slice 2."
)

SIDECAR_MIGRATION_POLICY: Final[str] = (
    "Planned for slice 2: within 1.x, Hermes-A365 will read every Hermes-owned sidecar "
    "written by any 1.x release and every 0.9.x shape listed in SURFACE. Additive "
    "`schemaVersion` stamps will run on the owning writer, once, under an exclusive lock, "
    "and be idempotent. The original bytes will be copied to a sibling "
    "`*.backup-<UTC-timestamp>.json` mode `0600` before the first rewrite. A second run "
    "against an already-current file will not write another backup. Identity-binding changes "
    "will stay explicit: bot-service v1→v2 will still require `--confirm-legacy-binding` "
    "equal to `--agent-name` and `--confirm-bot-target` equal to the ARM id; publish v1→v2 "
    "will still require `--app-version` strictly above the Admin Center version. A "
    "`schemaVersion` newer than that build will be an error and the file will be left "
    "untouched. 1.x will not claim to read 2.x sidecars. Downgrade will be restore-the-backup, "
    "not an automatic rewrite to 0.9.x. " + SIDECAR_MIGRATION_TODAY
)

SECURITY_SUPPORT_AFTER_V1: Final[str] = (
    "After v1.0, security fixes are provided for the latest 1.x minor release only. "
    "Earlier 1.x minors and all 0.x releases are unsupported."
)
