# Hermes-A365

Integrate Hermes agents into the Microsoft 365 ecosystem using Microsoft Agent 365 (A365).

## Status

**Design-stage v0.1 — apply-side needs rebuild for v0.2.** The authoritative design lives in [`SPEC.md`](SPEC.md) (v1, 2026-05-03 — two days after A365 reached general availability on 2026-05-01). All planner-side scripts ship and unit tests pass (393, ruff-clean). The validator-compliant [`SKILL.md`](SKILL.md) is drafted and harness-loadable.

**However**, on 2026-05-04 we installed the real `Microsoft.Agents.A365.DevTools.Cli` and discovered that the spec's command surface (drafted from public docs that pre-dated GA) does not match the GA reality. Verified divergences are catalogued in [`references/a365-cli-reference.md`](references/a365-cli-reference.md) — most notably: there's no `setup app --tier=N`, no `fic configure/rotate`, no `create-instance`, no `deploy --channels=…`, no per-kind `cleanup app/deployment`, and the `query-entra` flag set is much smaller than assumed. The operator must also pre-register a custom Entra client app *before* the CLI works (reverse of what `register.py` does today).

The planner architecture — reconcilers, status report, doctor, secrets, render helpers, local artefact handling — survives the redesign. The `Mutator` protocol and most of the apply-path scripts (`register.py`, `blueprint_create.py`, `instance_create.py`, `deploy.py`, `fic_rotate.py`, `cleanup.py`) need to be re-implemented around the real command set as v0.2 work. Activity-bridge work remains blocked on SPEC §10 Q1 (Hermes IPC contract).

## Repo layout

```
.
├── SPEC.md           # Authoritative spec for the hermes-a365 skill
├── SKILL.md          # Validator-compliant upstream contribution (per SPEC §3.1)
├── README.md         # This file
├── LICENSE           # MIT
├── .gitignore
├── pyproject.toml    # Python 3.11+, uv-managed, pytest + ruff dev deps
├── references/       # Dated snapshots: CLI, errors, blueprint props, OTel, BF, license
├── scripts/          # Subcommand implementations (planner + applier per command)
│   └── _common.py    # Shared Jinja env, deep_diff, parse_env, safe_run, tcp_reachable
├── templates/
│   ├── blueprint.json.j2
│   ├── instance.env.j2
│   ├── consent-url.txt.j2
│   └── adaptive-cards/             # greeting / confirmation / error
└── tests/
    ├── conftest.py
    ├── test_*.py                   # one per scripts/ module
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
- **2026-05-03:** thirteenth slice — `telemetry.py` (read-only OTLP / span verifier, per §6.8). Three checks: `HERMES_OTLP_ENDPOINT` set in agent .env, `AA_INSTANCE_ID` recorded, last span seen via `QuerySource.query_telemetry`. JSON output by default, `--human` for a markdown table. Exit codes mirror `status` (0 ok / 1 partial / 2 broken). Span injection itself is the activity bridge's responsibility (§6.7); this command only verifies the pipeline.
- **2026-05-04:** fourteenth slice — `fic_rotate.py` (rotate the user-FIC for the T2 confidential client, per §6.10). New `Mutator.fic_rotate` op wraps `a365 fic rotate --app=<T2-appId>`; the new client secret is written to the OS keychain via the existing `secrets` wrapper, replacing the entry written at `register` time. Default dry-run; `--apply` rotates. Surfaces an explicit reminder to restart the activity bridge after rotation.
- **2026-05-04:** fifteenth slice — `cleanup.py` (per-agent destructive teardown, per §6.13). Order matters per spec: `deployment → instance → blueprint`. Apps (T1/T2) are deliberately *not* touched — they're tenant-wide infrastructure shared across every agent in the skill. Safety: `--confirm` is required and must be the literal agent slug; the plan is always printed (even without `--apply`) so the operator can audit before mutating. Defensive plan-building: each step is included only when the underlying state appears to exist; missing state turns into a recorded skip rather than an error. New `Mutator.cleanup(kind, identifier)` op covers the `a365 cleanup deployment/instance/blueprint/app` family. Local artefacts (`.env`, `blueprint.json`) are removed only after the cloud steps succeed; the empty agent dir is also reaped.
- **2026-05-04:** sixteenth slice — `references/` content. Filled in seven dated-snapshot files: `README.md` (index), `a365-cli-reference.md` (variants, version pins, every CLI subcommand the skill calls + which module owns it), `error-codes.md` (AADSTS catalogue + delegated-scope drift table), `entra-blueprint-properties.md` (top-level + sub-property allowlist + the server-assigned-fields list that `blueprint_create` strips), `opentelemetry-config.md` (canonical event vocabulary, required span attributes, sampler), `activity-protocol-shapes.md` (forward-looking Bot Framework shape catalogue for the still-blocked activity bridge), `license-cost-table.md` (decision matrix matching `scripts/license.py`). Doctor handles drift detection; this folder is the snapshot, not the source of truth.
- **2026-05-04:** seventeenth slice — `SKILL.md` (the upstream contribution). Validator-compliant frontmatter per SPEC §3.2 (name, description, version 0.1.0, MIT, hermes tags, related-skills). Body follows §4 structure: Overview, When to Use, Prerequisites, Core procedures (one block per subcommand including the still-blocked activity-bridge), Conflict resolution, Common pitfalls, Verification checklist, One-shot recipes. ~14.9k chars — comfortably under the 15k target; capability detail beyond that lives in `references/`. This file is what gets contributed to `hermes-agent/optional-skills/cloud-platforms/hermes-a365/SKILL.md` per SPEC §3.1.

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

**v0.2 redesign in progress.** v0.1 targeted a speculative CLI that
diverged materially from Microsoft.Agents.A365.DevTools.Cli's GA surface
(see [`references/a365-cli-reference.md`](references/a365-cli-reference.md)).
Slices 18a–18f have landed: foundation reset (`mutator.py` thin
`run(argv)` wrapper + `a365_config.py`), apply-path rebuild (`register`,
`instance_create`, `cleanup`, `publish`), and read-path rework
(`doctor` + `status` against the real `query-entra` surface;
`telemetry.py` dropped because `a365 query-entra --telemetry` does
not exist).

| Area | Status |
|---|---|
| `_common.py` shared helpers (Jinja env, `safe_run`, `tcp_reachable`, `parse_env`, `deep_diff`) | done |
| `secrets.py` (OS-keychain wrapper — resolves §10 Q3) | done |
| `reconcile_app.py`, `reconcile_blueprint.py` (idempotent diff/plan abstraction) | done |
| Adaptive Card templates + `emit_card.py` (greeting / confirmation / error) | done |
| `license.py` (recommendation engine; §6.1) | done |
| Consent URL template + `consent.py` (URL render + grant poll; §6.3) | done |
| `render_instance_env.py` + template (per-agent runtime `.env`) | done |
| `mutator.py` (v0.2 thin CLI wrapper + AADSTS handling) | **done (Slice 18a)** |
| `a365_config.py` (`a365.config.json` round-trip) | **done (Slice 18a)** |
| `register.py` → setup orchestrator (`a365 setup blueprint` + `setup permissions {mcp,bot}`) | **done (Slice 18b)** |
| `instance_create.py` → local runtime `.env` writer only | **done (Slice 18c)** |
| `cleanup.py` rewrite around `cleanup blueprint/instance/azure` | **done (Slice 18d)** |
| `publish.py` (manifest packaging via `a365 publish`) | **done (Slice 18e)** |
| `doctor.py` (env probe — real CLI variant + PowerShell + custom-client-app prereqs) | **done (Slice 18f)** |
| `status.py` (per-component report against `query-entra`; resolves SPEC §6.11) | **done (Slice 18f)** |
| `references/` content (CLI surface, error codes, etc.) | done |
| `SKILL.md` 0.2.0 final | **TODO (Slice 18g)** |
| `activity_bridge.py` | TODO (blocked on SPEC §10 Q1 — Hermes IPC contract) |
| Live integration test against an M365 tenant | TODO (Slice 18g — once apply path is rebuilt) |

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

> **v0.2 in progress.** The apply-path scripts (`register.py`,
> `instance_create.py`, `cleanup.py`, plus the new `publish.py`) are
> being rebuilt against the real `a365` CLI surface. v0.1 commands that
> targeted non-existent CLI verbs (`deploy.py`, `fic_rotate.py`,
> `blueprint_create.py`, `workiq.py`, `render_blueprint.py`) have been
> deleted. Usage examples for the rebuilt commands will return as each
> sub-slice lands.

Work IQ MCP exposure (drives blueprint reconciliation):

```bash
# Add tools (additive)
uv run python scripts/workiq.py inbox-helper --enable=mail,calendar --apply

# Remove tools
uv run python scripts/workiq.py inbox-helper --disable=teams --apply

# Replace the whole list
uv run python scripts/workiq.py inbox-helper --set=mail,calendar --apply
```

Telemetry verifier (read-only):

```bash
uv run python scripts/telemetry.py inbox-helper --human    # markdown table
uv run python scripts/telemetry.py inbox-helper            # JSON
echo $?                                                    # 0=ok, 1=partial, 2=broken
```

User-FIC rotation (refreshes the T2 client secret in the OS keychain):

```bash
uv run python scripts/fic_rotate.py             # plan only
uv run python scripts/fic_rotate.py --apply     # rotate
```

Per-agent cleanup (destructive — apps are *not* removed):

```bash
# Plan only — always prints what would be removed
uv run python scripts/cleanup.py inbox-helper

# Execute the plan — --confirm must equal the agent slug
uv run python scripts/cleanup.py inbox-helper --apply --confirm=inbox-helper
```

## License

MIT — see [`LICENSE`](LICENSE).
