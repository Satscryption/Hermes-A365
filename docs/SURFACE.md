# Hermes-A365 public surface (frozen inventory)

Package version **0.9.5**. Machine-readable snapshot: [`docs/surface/public-surface.json`](surface/public-surface.json). Regenerate with `scripts/render_surface.py --write`; CI runs `--check`.

## Owner decisions (accepted)

Status: **accepted** (recorded in `src/hermes_a365/surface.py`).

- **D1** A — activity-bridge serve is a supported secondary runtime at 1.0
- **D2** A — default CEA publish scopes remain copilot, personal, team
- **D3** A — freeze the task/fetch placeholder card (Hermes / Request received.)
- **D4** A — security fixes for the latest 1.x minor only
- **D5** Removed by #267: HERMES_OTLP_ENDPOINT is not part of the 1.0 public env (deprecated literals may remain in source for migration warnings only)
- **D6** B — additive auto-stamps only; keep bot-service and publish confirm gates
- **D7** B — do not ship py.typed / Typing :: Typed
- **D8** B — public python -m hermes_a365.keychain; hermes_responder reference-only

- **D1 constant** `D1_SERVE_SUPPORTED=True`.
- **D2 constant** `D2_CEA_SCOPES=('copilot', 'personal', 'team')`.
- **D3 constant** `D3_TASK_FETCH_CARD_FROZEN=True`.
- **D4 constant** `D4_SECURITY_LATEST_1X_MINOR_ONLY=True`.
- **D6 constant** `D6_AUTO_MIGRATE_ADDITIVE_ONLY=True`.
- **D7 constant** `D7_SHIP_PY_TYPED=False`.
- **D8 constants** `D8_KEYCHAIN_PUBLIC=True`, `D8_RESPONDER_REFERENCE_ONLY=True`.

## CLI

Verbs: `doctor`, `license`, `register`, `consent`, `instance create`, `publish`, `status`, `cleanup`, `activity-bridge` (`verify` / `serve` / `update-endpoint`), `bot-service` (`create` / `enable-channel` / `update-endpoint` / `cleanup` / `verify`). Both `hermes-a365` and `hermes a365` share identical argparse trees (see JSON). Help strings are not frozen except `register --aiteammate` refusal (exit 2).

### Exit codes

```json
{
  "activity_bridge_verify": {
    "error": 2,
    "ok": 0,
    "warn": 1
  },
  "bot_service": {
    "failed_or_partial": 1,
    "refusal": 2,
    "success": 0
  },
  "bot_service_verify": [
    0,
    1,
    2
  ],
  "cleanup": {
    "failed_or_partial": 1,
    "refusal": 2,
    "success": 0
  },
  "consent": {
    "failed_or_partial": 1,
    "refusal": 2,
    "success": 0
  },
  "doctor": {
    "error": 2,
    "ok": 0,
    "warn": 1
  },
  "instance_create": [
    0,
    2
  ],
  "license": [
    0,
    2
  ],
  "missing_verb_or_usage": 2,
  "publish_run": [
    0,
    2
  ],
  "register": {
    "failed_or_partial": 1,
    "refusal": 2,
    "success": 0
  },
  "register_aiteammate": 2,
  "status": {
    "broken": 2,
    "ok": 0,
    "partial": 1,
    "uninitialized": 3
  }
}
```

## Environment

Scanner classifies every `A365_*` / `HERMES_*` string literal in `src/hermes_a365`. Per-agent `.env` keys from `instance create` are listed separately.

### Public env literals (scanner)

A365_AGENT_IDENTITY_IDS, A365_ALLOWED_USERS, A365_ALLOW_ALL_USERS, A365_APP_ID, A365_BF_APP_ID, A365_BF_CLIENT_SECRET, A365_BLUEPRINT_CLIENT_SECRET, A365_CLIENT_APP_NAME, A365_CONVERSATIONS_PATH, A365_FEEDBACK_LOOP, A365_FILE_HOST_ALLOWLIST, A365_GENERATED_CONFIG_PATH, A365_HANDOFF_LINK, A365_TENANT_ID, HERMES_A365_ALLOW_INSECURE_MACOS_KEYCHAIN_CLI, HERMES_A365_COPILOT_FIRST_TURN_MIN_STREAM_AGE_SEC, HERMES_A365_COPILOT_MIN_STREAM_AGE_BEFORE_FINAL_SEC, HERMES_A365_COPILOT_SHORT_TURN_MAX_DURATION_SEC, HERMES_BRIDGE_PORT, HERMES_BRIDGE_WEBHOOK, HERMES_GATEWAY_PROFILE, HERMES_HOME, HERMES_PROFILE, HERMES_RESPONDER_HISTORY_TOKEN

### Instance create `.env` keys

A365_APP_ID, A365_BF_APP_ID, A365_BF_CLIENT_SECRET, A365_TENANT_ID, AA_INSTANCE_ID, AGENT_IDENTITY, BUSINESS_HOURS_END, BUSINESS_HOURS_START, BUSINESS_HOURS_TZ, OWNER, OWNER_AAD_ID

### Internal env literals

HERMES_OTLP_ENDPOINT

## Gateway config

`platforms.agent365.extra` keys: slug, port, host, tenant_id, app_id, blueprint_client_secret, bf_app_id, bf_client_secret, generated_config_path, conversations_path, conversations_prune_max_age_days, idempotency_max_entries, file_host_allowlist, agent_identity_ids, notice_delivery, reactions_dispatch_agent_turn, group_sessions_per_user, thread_sessions_per_user.

`A365Config` fields: tenantId, clientAppId, subscriptionId, resourceGroup, location, environment, appServicePlanName, appServicePlanSku, webAppName, agentIdentityDisplayName, agentBlueprintDisplayName, agentUserPrincipalName, agentUserDisplayName, managerEmail, agentUserUsageLocation, deploymentProjectPath, agentDescription (upstream `a365.config.json`; no `schemaVersion`).

`plugin.yaml` `requires_env`: A365_TENANT_ID, A365_APP_ID, AA_INSTANCE_ID, HERMES_BRIDGE_PORT.

## Sidecars

Planned for slice 2: within 1.x, Hermes-A365 will read every Hermes-owned sidecar written by any 1.x release and every 0.9.x shape listed in SURFACE. Additive `schemaVersion` stamps will run on the owning writer, once, under an exclusive lock, and be idempotent. The original bytes will be copied to a sibling `*.backup-<UTC-timestamp>.json` mode `0600` before the first rewrite. A second run against an already-current file will not write another backup. Identity-binding changes will stay explicit: bot-service v1→v2 will still require `--confirm-legacy-binding` equal to `--agent-name` and `--confirm-bot-target` equal to the ARM id; publish v1→v2 will still require `--app-version` strictly above the Admin Center version. A `schemaVersion` newer than that build will be an error and the file will be left untouched. 1.x will not claim to read 2.x sidecars. Downgrade will be restore-the-backup, not an automatic rewrite to 0.9.x. Today (slice 1) `conversations.json` is persisted with a `schema` field only (`conversations.py`); the dual-key stamp lands in slice 2.

## Python API

- Console script `hermes-a365`; entry point `hermes_agent.plugins` / `agent365` → `hermes_a365.plugin:register`.
- `hermes_a365.__version__` → `0.9.5`.
- `Agent365Adapter` public members: build_app, connect, disconnect, edit_message, get_chat_info, handle_message, invalidate_exec_approval_capabilities, message_len_fn, name, on_processing_complete, on_processing_start, prune_conversations, send, send_clarify, send_document, send_exec_approval, send_image, send_image_file, send_or_update_status, send_private_notice, send_slash_confirm, send_typing, streaming_overflow_limit.
- Public `python -m`: `hermes_a365.keychain` subcommands store, get, delete.

## HTTP (product)

Gateway `build_app` and `activity-bridge serve`:

- `POST /api/messages`
- `GET /healthz`

`GET /healthz` → 200 `{ok, slug, blueprint_client_id}` (id truncated to 8 chars). `POST /api/messages`: 400 bad activity; 401 bearer; 403 serviceUrl / allowlist / user; 200 ack bodies; plugin-only 503 while connect failed. `serve` speaks operator webhook contract version `"1"` (`references/webhook-contract.md`).

## Invoke wire names

- Registry: task/fetch.
- Adapter handlers: handoff/action, message/fetchTask, message/submitAction.
- `task/fetch` card (D3): Adaptive Card 1.5, title `Hermes`, text `Request received.`.

## Packaging (snapshot; slice 3 may change metadata)

Requires Python >=3.11. Extras: bridge, dev.

