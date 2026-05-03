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
| Adaptive Card templates | TODO |
| Consent URL template | TODO |
| `status.py` | TODO |
| `activity_bridge.py` | TODO (blocked on §10 Q1 — Hermes IPC contract) |
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

## License

MIT — see [`LICENSE`](LICENSE).
