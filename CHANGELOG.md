# Changelog

All notable changes to the `hermes-a365` skill / plugin live here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] — 2026-05-11

Repackaging-only release: `hermes-a365` is now `pip install`-able from
PyPI. No behavioural changes; the apply paths, read paths, and Bot
Framework activity bridge are identical to `v0.1.0`.

### Changed

- **Distribution.** Source tree moved to a real `src/hermes_a365/`
  layout. `[tool.uv] package = false` is gone; the wheel is built via
  `hatchling` and published to PyPI. Two install paths now supported:
  - **Standalone CLI:** `pipx install 'hermes-a365[bridge]'` exposes a
    `hermes-a365 <verb>` console script for operators who drive the
    wrappers without spinning up a Hermes gateway.
  - **Gateway plugin:** `~/.hermes/hermes-agent/venv/bin/pip install
    'hermes-a365[bridge]'`. The Hermes plugin loader auto-discovers
    `agent365` via the `hermes_agent.plugins` entry point — no
    `~/.hermes/plugins/agent365/` directory, no symlink.
- **Imports.** Every module is now `hermes_a365.<x>`. The
  symlink-walking `Path(__file__).resolve().parent.parent.parent /
  "scripts"` trick in the plugin (`plugins/agent365/{adapter,cli}.py`)
  is retired; the plugin imports `from hermes_a365 import
  activity_bridge` directly.
- **Templates.** `templates/` is now packaged as `hermes_a365._data/
  templates/` and resolved via `importlib.resources` so lookups work
  for both editable installs and wheels.
- **Tests.** Bare imports (`from a365_config import …`) rewritten to
  `from hermes_a365.a365_config import …`. `tests/conftest.py` no
  longer pokes `scripts/` onto `sys.path`. 624 tests still passing.
- **Docs.** README, SKILL.md, and the `references/` runbooks updated
  to drop the symlink instructions and the `uv run python scripts/<x>.py`
  invocation style in favour of `pipx install` + `hermes-a365 <verb>`
  (or `python -m hermes_a365.<x>` for the modules that aren't surfaced
  as CLI subcommands).

## [0.1.0] — 2026-05-08

First operator-targeted release. Validated end-to-end against
Microsoft.Agents.A365.DevTools.Cli **1.1.171** (round-5 walkthrough,
2026-05-06) and the secret-null regression-recovery path on **1.1.174**
(round-6, 2026-05-07).

### Added — apply path (operator-side wrappers)

- `hermes a365 register` — orchestrates `a365 setup blueprint` +
  `setup permissions mcp` + `setup permissions bot` with AADSTS-aware
  retry, layer-1 client-secret regression detection, and opt-in
  `--auto-recover-secret` (handles Microsoft#408 on macOS / Linux).
- `hermes a365 consent` — render admin-consent URL, optionally launch a
  browser, poll `query-entra blueprint-scopes` until consent is granted.
- `hermes a365 instance create <slug>` — write the per-agent runtime
  `~/.hermes/agents/<slug>/.env` (no cloud step).
- `hermes a365 publish` — package the AI Teammate manifest zip for
  Microsoft 365 Admin Centre upload.
- `hermes a365 cleanup` — destructive teardown with `--purge-orphans`
  for blueprint-flow agentic users + agentRegistry instances. AI
  Teammate-flow store-managed instances always 403 on delete (Microsoft
  platform limitation, documented in `references/live-tenant-test.md`).
- `hermes a365 license` — recommends an A365 license tier given a user
  + agent count and plan.

### Added — read path

- `hermes a365 doctor` — read-only environment probe (CLI version, az
  signed-in, pwsh on PATH, network reachability, OS keychain backend).
- `hermes a365 status [<slug>]` — per-component status report against
  `query-entra` (local config, blueprint scopes, instance scopes, local
  bridge PID).

### Added — runtime

- **`agent365` Hermes platform adapter** (`plugins/agent365/`).
  Validated end-to-end against a Frontier-Preview tenant on Microsoft
  Teams 1:1 chat (rounds 3 → 5). Inbound activities go through AAD-v2
  JWT validation, BF idempotency dedupe, and `serviceUrl` host-suffix
  allowlist before reaching the agent loop.
- **`hermes a365 activity-bridge`** — Bot Framework adapter daemon.
  - `verify` — one-shot diagnostic (config + auth + reachability).
  - `serve` — long-running `/api/messages` webhook (FastAPI + uvicorn
    via the optional `bridge` extras).
  - `update-endpoint` — re-points the agent's messaging endpoint at a
    public tunnel URL.
- **Three-stage user-FIC token chain** for outbound replies (BF S2S →
  agent FMI delegation → user FIC), plus per-conversation
  durable registry that survives gateway restarts
  (`~/.hermes/agents/<slug>/conversations.json`).
- **`agents`-channel synthetic-event filter** — drops M365 onboarding
  probes + email-template render activities so they don't waste an
  agent turn (round-5 walkthrough finding).

### Added — CLI surface

- `hermes a365 <verb>` is wired via the supported Hermes plugin
  `register_cli_command` API (slice 19x-a, this release). Each verb
  delegates to the matching `scripts/<x>.py` module; running
  `python scripts/<x>.py` continues to work for development.

### Added — references / runbooks

- `references/live-tenant-test.md` — end-to-end runbook for a
  Frontier-Preview tenant; flags macOS 26 device-code prompt-volume
  failure mode (~10–12 prompts per `register --apply --m365`).
- `references/m365-surface-coverage.md` — per-surface coverage matrix.
- `references/exposing-the-bot-endpoint.md` — operator-side options
  (cloudflared, devtunnels, ngrok, reverse-proxy) — non-prescriptive.
- `references/a365-cli-reference.md`, `webhook-contract.md`,
  `activity-protocol-shapes.md`, `error-codes.md`,
  `entra-blueprint-properties.md`, `opentelemetry-config.md`,
  `license-cost-table.md`.

### Filed upstream

- **microsoft/Agent365-devTools#402** — cosmetic logging fixes when
  Observability-only S2S app-role assignment is the intended state.
  Microsoft confirmed intent + shipped fixes in CLI 1.1.174.
- **microsoft/Agent365-devTools#408** — `agentBlueprintClientSecret`
  null-on-disk regression on macOS (DPAPI unavailable). Layer 1
  detection + auto-recovery shipped in this release; Layer 2 is the
  upstream fix.

### Known limitations

- **M365 Copilot streaming** ([#3](https://github.com/satscryption/Hermes-A365/issues/3))
  not yet implemented — `Agent365Adapter.edit_message` is a no-op and
  `REQUIRES_EDIT_FINALIZE` is unset. Required for Copilot Chat surface.
- **Proactive replies for >10 s agent thinking**
  ([#4](https://github.com/satscryption/Hermes-A365/issues/4)) — `send()`
  still requires a cached inbound; cron-driven sends do not work yet.
- **`hermes gateway setup` wizard**
  ([#13](https://github.com/satscryption/Hermes-A365/issues/13)) not yet
  shipped — operators must hand-edit `~/.hermes/config.yaml` and
  `~/.hermes/.env` per the README quickstart.
- **Invoke activities** (Outlook compose-action, Teams compose
  extensions, search, signin) tracked under
  [#18](https://github.com/satscryption/Hermes-A365/issues/18); umbrella
  not yet implemented.
- **Plaintext on-disk secret on macOS / Linux.** DPAPI is Windows-only;
  the keychain shim in `scripts/keychain.py` writes the agent blueprint
  client secret to `a365.generated.config.json` with mode `0600`. See
  README "Security model".
- **AI Teammate-flow agentRegistry entries cannot be deleted** by
  operators (only "blocked" via the M365 Admin Centre). Microsoft
  platform limitation; not a wrapper bug.

[Unreleased]: https://github.com/satscryption/Hermes-A365/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/satscryption/Hermes-A365/releases/tag/v0.1.0
