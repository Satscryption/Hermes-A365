# Hermes-A365

Integrate Hermes agents into the Microsoft 365 ecosystem using Microsoft Agent 365 (A365).

## Status

**Early implementation.** The authoritative design lives in [`SPEC.md`](SPEC.md) (v1, 2026-05-03 — two days after A365 reached general availability on 2026-05-01). The first vertical slice — blueprint and per-agent `.env` rendering, with golden-file tests — is in place. Most subcommands and the Activity bridge are still TODO.

## Repo layout

```
.
├── SPEC.md           # Authoritative spec for the hermes-a365 skill
├── README.md         # This file
├── LICENSE           # MIT
├── .gitignore
├── pyproject.toml    # Python 3.11+, uv-managed, pytest + ruff dev deps
├── references/       # Microsoft Learn pointers, BF activity shapes, blueprint property reference (TODO)
├── scripts/          # Helpers — render_blueprint, render_instance_env (more TODO)
│   └── _common.py    # Shared Jinja env + path helpers
├── templates/
│   ├── blueprint.json.j2
│   ├── instance.env.j2
│   ├── consent-url.txt.j2          (TODO)
│   └── adaptive-cards/             (TODO)
└── tests/
    ├── conftest.py
    ├── test_render_blueprint.py
    ├── test_render_instance_env.py
    └── golden/                     # Golden fixtures (regenerate with --update-golden)
```

## Repo split

This repo holds the **design artefacts** — spec, references, scripts, templates. The eventual Hermes `SKILL.md` is **contributed upstream** into the Hermes Agent harness at `hermes-agent/optional-skills/cloud-platforms/hermes-a365/SKILL.md`, pulling these artefacts in at contribution time. See [`SPEC.md` §3.1 and §13](SPEC.md) for the full rationale.

## What is A365?

[**Microsoft Agent 365**](https://learn.microsoft.com/en-us/microsoft-agent-365/developer/) is a governance / identity / observability control plane for AI agents that GA'd 2026-05-01. It is **not** an agent framework — it bolts on top of whichever agent stack you use (Microsoft Agent Framework, Microsoft 365 Agents SDK, OpenAI Agents SDK, OpenClaw, Claude Code SDK, etc.) and adds:

- Entra-backed agent identity (delegated permissions only)
- Tenant licensing (\$15/user/mo add-on, or M365 E7 \$99/user/mo)
- Agent blueprints, registered via `a365 setup blueprint`
- MCP-mediated access to Microsoft 365 data (Mail, Calendar, SharePoint, Teams) — "Work IQ tools"
- Bot Framework Activity protocol for notifications and Adaptive Card invokes
- OpenTelemetry observability surfaced in admin center
- Teams / Outlook / Microsoft 365 Copilot channel adapters

`hermes-a365` is the Hermes-side skill that drives these from inside the Hermes harness.

## Open questions

See [`SPEC.md` §10](SPEC.md). Highest-priority: the Hermes IPC contract that the Activity bridge will use to invoke the local agent.

## Status meta

- **2026-05-03:** repo created, `SPEC.md` v1 draft committed.
- **2026-05-03:** `SPEC.md` revision 2 (glossary, diagrams, examples, troubleshooting, migration recipe, risks).
- **2026-05-03:** first implementation slice — blueprint JSON and per-agent `.env` rendering with golden-file tests.
- **2026-05-03:** second slice — `doctor.py` (read-only environment probe; resolves §10 Q7 — `atk` vs `a365` variant detection).
- **2026-05-03:** third slice — `secrets.py` (OS-keychain wrapper; resolves §10 Q3 — macOS `security` and Linux `secret-tool`).
- **2026-05-03:** fourth slice — reconcilers (`deep_diff` in `_common.py`, `reconcile_app.py`, `reconcile_blueprint.py`) producing `create`/`noop`/`patch`/`abort` plans against captured `a365 query-entra` JSON.
- **2026-05-03:** fifth slice — `status.py` orchestrating nine components (license, T1/T2 apps, blueprint, instance, channels, activity bridge, telemetry, FIC) into a single report; exit codes 0/1/2/3 per spec. `QuerySource` Protocol abstracts `a365 query-entra` so the command works end-to-end with or without a live `a365` CLI.
- **2026-05-03:** sixth slice — Adaptive Card v1.6 templates (`greeting`, `confirmation`, `error`) under `templates/adaptive-cards/` plus `emit_card.py` builder with typed dataclass inputs. Golden-file tests verify JSON validity and round-trip stability.
- **2026-05-03:** seventh slice — `license.py` (read-only recommendation per §6.1) and `consent.py` (admin-consent URL rendering + grant polling per §6.3) plus the `consent-url.txt.j2` template. The polling loop is fully testable via monkeypatched `time.sleep`/`time.monotonic`.
- **2026-05-03:** eighth slice — `register.py` (Entra T1+T2 app registration and user-FIC, per §6.2). Composes `reconcile_app` plans with a new `Mutator` protocol (default `A365CliMutator` shells out; tests inject a `FakeMutator`). Default dry-run; `--apply` executes. AADSTS500011 retries with backoff (mockable `sleep_fn`); AADSTS90094 surfaces a `consent` follow-up rather than failing. T2 client secret stored via the keychain wrapper (never to disk). `~/.hermes/.env` updated atomically (tmp + rename) with `A365_TENANT_ID`, `A365_APP_ID`, optional `A365_CLI_VARIANT`. `QuerySource` gained `query_app_by_name` to support name-based lookup.
- **2026-05-03:** ninth slice — `blueprint_create.py` (register/patch an A365 agent blueprint, per §6.4). Composes `render_blueprint` and `reconcile_blueprint` with a new `Mutator.setup_blueprint` operation. Default dry-run renders to a tmp file and prints the plan + diff; `--apply` hands the tmp file to the CLI and atomically caches the rendered JSON at `~/.hermes/agents/<slug>/blueprint.json`. Server-assigned fields (`blueprintId`, `lastPatched`, `etag`, etc.) are stripped from the actual payload before diffing so noop plans aren't perturbed. Slug mismatches abort; `BlueprintCreateError` surfaces refusals.
- **2026-05-03:** tenth slice — `instance_create.py` (per-agent runtime config + cloud instance registration, per §6.5). Inherits `A365_APP_ID`/`A365_TENANT_ID`/`A365_CLI_VARIANT`/`HERMES_OTLP_ENDPOINT` from `~/.hermes/.env`. Existing `AA_INSTANCE_ID` is preserved across re-runs (idempotency); business-hours fields from a prior run are also preserved unless overridden. Atomically writes `~/.hermes/agents/<slug>/.env` (still no `A365_APP_PASSWORD` per spec). New `Mutator.create_instance` op drives `a365 create-instance --blueprint=<slug> --instance=<UUID>`; cloud step is skipped if the instance is already registered. Plan distinguishes `create` (fresh local + cloud), `create-cloud-only` (local id exists but cloud missing), and `noop`.
- **2026-05-03:** eleventh slice — `deploy.py` (channel deployment for Teams / Outlook / M365 Copilot, per §6.9). Reads `AA_INSTANCE_ID` from the agent .env, queries the instance's currently-bound channels (state == `ok`), and computes a set diff against the desired list. New `Mutator.deploy` op hands the desired absolute set to `a365 deploy --instance=<id> --channels=<list>`; A365 reconciles additions/removals server-side. Empty desired list = unbind all. Idempotent: same set → noop, no mutator call. Surfaces deep-links from the response when present.
- **2026-05-03:** twelfth slice — `workiq.py` (toggle Work IQ MCP exposure, per §6.6). Config-only — no local MCP server runs. Reads the cached blueprint at `~/.hermes/agents/<slug>/blueprint.json`, reconstitutes `BlueprintInputs`, applies `--enable`/`--disable`/`--set` to the workiq tool list, and delegates to `blueprint_create`'s pipeline so the underlying reconciler decides create vs patch. `--set` is mutually exclusive with the additive flags; unknown tool names are rejected up-front against the `WORKIQ_TOOLS` constant.

## Development

This repo is a [uv](https://docs.astral.sh/uv/)-managed Python project. Python 3.11+.

### One-time setup

```bash
uv sync --extra dev
```

That installs runtime deps (`jinja2`) and dev deps (`pytest`, `ruff`).

### Common commands

```bash
# Run the test suite
uv run pytest

# Regenerate golden fixtures after intentionally changing the rendering
uv run pytest --update-golden

# Lint and format
uv run ruff check .
uv run ruff format .

# Render a blueprint from the CLI (dry stdout output)
uv run python scripts/render_blueprint.py \
    --slug inbox-helper \
    --description "Summarises unread mail" \
    --purpose productivity \
    --workiq mail,calendar

# Render a per-agent .env from the CLI
uv run python scripts/render_instance_env.py \
    --agent-identity inbox-helper \
    --owner sadiq@contoso.com \
    --owner-aad-id 00000000-0000-0000-0000-000000000001 \
    --a365-app-id 00000000-0000-0000-0000-00000000aaa1 \
    --a365-tenant-id contoso.onmicrosoft.com \
    --a365-cli-variant a365-dotnet \
    --hermes-otlp-endpoint https://contoso.otel.agent365.microsoft.com
```

### What's implemented vs TODO

| Area | Status |
|---|---|
| Blueprint render (template + script + tests) | done |
| Per-agent `.env` render (template + script + tests) | done |
| `_common.py` shared helpers (Jinja env, `safe_run`, `tcp_reachable`, `parse_env`) | done |
| `doctor.py` (env probe — resolves §10 Q7) | done |
| `secrets.py` (OS-keychain wrapper — resolves §10 Q3) | done |
| `reconcile_app.py`, `reconcile_blueprint.py` (idempotent diff/plan) | done |
| `status.py` (per-component report; resolves SPEC §6.11) | done |
| Adaptive Card templates + `emit_card.py` (greeting / confirmation / error) | done |
| Consent URL template + `consent.py` (URL render + grant poll; §6.3) | done |
| `license.py` (recommendation engine; §6.1) | done |
| `register.py` (Entra T1+T2 app + user-FIC; §6.2) | done |
| `blueprint_create.py` (register/patch agent blueprint; §6.4) | done |
| `instance_create.py` (per-agent .env + cloud instance; §6.5) | done |
| `deploy.py` (channel set reconciliation; §6.9) | done |
| `workiq.py` (toggle Work IQ MCP exposure; §6.6) | done |
| `activity_bridge.py` | TODO (blocked on §10 Q1 — Hermes IPC contract) |
| `telemetry`, `fic rotate`, `cleanup` | TODO (will compose existing reconcilers + secrets + status helpers) |
| `references/` content | TODO |
| `SKILL.md` (drafted here, upstreamed later) | TODO |

The doctor can be run directly:

```bash
uv run python scripts/doctor.py --human            # operator-friendly output
uv run python scripts/doctor.py                    # JSON to stdout
uv run python scripts/doctor.py --no-network       # offline diagnostic
echo $?                                            # 0=ok, 1=warn, 2=error
```

The keychain wrapper too:

```bash
# Store interactively (prompts for the secret, doesn't echo)
uv run python scripts/secrets.py store --tenant contoso.onmicrosoft.com --app-id <appId>

# Or pipe from stdin
echo -n "<secret>" | uv run python scripts/secrets.py store \
    --tenant contoso.onmicrosoft.com --app-id <appId> --secret -

uv run python scripts/secrets.py get    --tenant contoso.onmicrosoft.com --app-id <appId>
uv run python scripts/secrets.py delete --tenant contoso.onmicrosoft.com --app-id <appId>
```

> **macOS note.** First time the script writes to the keychain, macOS will pop a UI dialog asking permission for `python` to access your login keychain. Click "Always Allow" to avoid further prompts. Non-interactive contexts (CI, headless SSH, some IDEs) may fail with `rc=36 User interaction is not allowed` — unlock the keychain first with `security unlock-keychain` if needed.

The status command works whether or not the `a365` CLI is installed (cloud components get marked `skipped` rather than failing):

```bash
uv run python scripts/status.py --human                    # markdown table
uv run python scripts/status.py inbox-helper --human       # for a specific agent
uv run python scripts/status.py                            # JSON to stdout
echo $?                                                    # 0=ok, 1=partial, 2=broken, 3=uninitialized
```

Adaptive Card payloads can be emitted from the CLI for ad-hoc testing:

```bash
uv run python scripts/emit_card.py greeting --command "Summarise mail" --command "List events"
uv run python scripts/emit_card.py confirmation --action "Reply sent" --fact "Recipient=team@contoso.com"
uv run python scripts/emit_card.py error --heading "FIC expired" --message "Rotate now"
```

License recommendation (read-only; never purchases):

```bash
uv run python scripts/license.py --users 12 --agents 3 --plan E5
uv run python scripts/license.py --users 250 --agents 40 --plan E5 --bundled-security
```

Admin-consent URL rendering and grant polling:

```bash
uv run python scripts/consent.py --print-url-only        # just emit the URL
uv run python scripts/consent.py --no-open               # render + poll, no browser
uv run python scripts/consent.py --timeout 60            # custom poll timeout (seconds)
```

Entra app registration (default dry-run; `--apply` to mutate):

```bash
# Plan only — prints what would change, no mutations
uv run python scripts/register.py \
    --app-name "Hermes Inbox Agent" \
    --tenant-id contoso.onmicrosoft.com

# Execute the plan
uv run python scripts/register.py \
    --app-name "Hermes Inbox Agent" \
    --tenant-id contoso.onmicrosoft.com \
    --cli-variant a365-dotnet \
    --apply
```

Blueprint registration (default dry-run; `--apply` to register/patch):

```bash
# Plan only — renders to a tmp file, prints diff vs cloud actual
uv run python scripts/blueprint_create.py inbox-helper \
    --description "Summarises unread mail" \
    --purpose productivity \
    --workiq mail,calendar

# Execute the plan
uv run python scripts/blueprint_create.py inbox-helper \
    --description "Summarises unread mail" \
    --purpose productivity \
    --workiq mail,calendar \
    --apply
```

Instance create (per-agent `.env` + cloud registration; idempotent):

```bash
# Plan only — shows what AA_INSTANCE_ID will be (existing or new)
uv run python scripts/instance_create.py inbox-helper \
    --owner sadiq@contoso.com \
    --owner-aad-id 00000000-0000-0000-0000-000000000001

# Execute the plan
uv run python scripts/instance_create.py inbox-helper \
    --owner sadiq@contoso.com \
    --owner-aad-id 00000000-0000-0000-0000-000000000001 \
    --apply
```

Channel deployment (idempotent set reconciliation):

```bash
# Plan only — shows additions / removals vs current cloud state
uv run python scripts/deploy.py inbox-helper --channels=teams,outlook

# Execute the plan
uv run python scripts/deploy.py inbox-helper --channels=teams,outlook --apply

# Unbind everything
uv run python scripts/deploy.py inbox-helper --channels="" --apply
```

Work IQ MCP exposure (drives blueprint reconciliation):

```bash
# Add tools (additive)
uv run python scripts/workiq.py inbox-helper --enable=mail,calendar --apply

# Remove tools
uv run python scripts/workiq.py inbox-helper --disable=teams --apply

# Replace the whole list
uv run python scripts/workiq.py inbox-helper --set=mail,calendar --apply
```

## License

MIT — see [`LICENSE`](LICENSE).
