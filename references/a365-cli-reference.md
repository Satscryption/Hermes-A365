# A365 CLI reference

Snapshot date: 2026-05-04

## CLI variants

The skill drives the **GA** Microsoft Agent 365 CLI. Two variants ship the
same command surface and both expose the binary as `a365` on PATH; the
doctor disambiguates them (SPEC §10 Q7).

| Variant | Source | Min version | Last tested | Detection signal |
|---|---|---|---|---|
| `a365` (.NET) | Microsoft package feed | **1.0.0** (GA, 2026-05-01) | 1.0.0 | `a365 --version` returns `Microsoft Agent 365 CLI <ver>` |
| `atk` (npm) | `@microsoft/agent-365-cli` on npmjs.org | **1.0.0** (GA) | 1.0.0 | `a365 --version` returns `@microsoft/agent-365-cli@<ver>` |
| `az` CLI | Microsoft package feed | **2.55.0** | 2.62.0 | Used for Entra reads only (not A365 itself) |

**Pin policy.** The doctor fails-soft on a higher version (warns, lets you
proceed) and fails-hard on a lower version. When upgrading the pin,
re-run the integration tests (§11.2) on both variants before bumping.

## Subcommands consumed by this skill

Read-only (used by `status.py` / `doctor.py` / planners):

| Subcommand | Purpose | Module |
|---|---|---|
| `a365 query-entra --license` | Tenant license posture | `status.py` |
| `a365 query-entra --by-name <name>` | T1/T2 app lookup by display name | `register.py` |
| `a365 query-entra --by-app-id <id>` | App lookup by id | `status.py` |
| `a365 query-entra --consent-status --app=<id>` | Consent grant state | `consent.py`, `status.py` |
| `a365 query-entra --blueprint=<slug>` | Blueprint payload | `blueprint_create.py`, `status.py` |
| `a365 query-entra --instance=<id>` | Instance + channel state | `instance_create.py`, `deploy.py`, `status.py` |
| `a365 query-entra --telemetry --instance=<id>` | OTLP / span surface | `telemetry.py`, `status.py` |
| `a365 query-entra --fic --app=<id>` | FIC expiry / status | `status.py` |
| `a365 query-entra --scopes` | Live A365 delegated scope catalog | `doctor.py` (drift check) |

Mutating (used through `Mutator` protocol in `scripts/register.py`):

| Subcommand | Mutator op | Module |
|---|---|---|
| `a365 setup app --tier=<n> --name=<name>` | `setup_app` | `register.py` |
| `a365 fic configure --app=<id>` | `fic_configure` | `register.py` |
| `a365 fic rotate --app=<id>` | `fic_rotate` | `fic_rotate.py` |
| `a365 setup blueprint --file=<path>` | `setup_blueprint` | `blueprint_create.py` |
| `a365 create-instance --blueprint=<slug> --instance=<id>` | `create_instance` | `instance_create.py` |
| `a365 deploy --instance=<id> --channels=<list>` | `deploy` | `deploy.py` |
| `a365 cleanup deployment --instance=<id>` | `cleanup` (`kind="deployment"`) | `cleanup.py` |
| `a365 cleanup instance --instance=<id>` | `cleanup` (`kind="instance"`) | `cleanup.py` |
| `a365 cleanup blueprint --slug=<slug>` | `cleanup` (`kind="blueprint"`) | `cleanup.py` |
| `a365 cleanup app --app=<id>` | `cleanup` (`kind="app"`) | (not yet wired; reserved for full-skill teardown) |

## JSON output convention

Every mutating call uses `--output=json` so we can parse a structured
response. The skill's `_extract_json_object` helper pulls the last
balanced `{ … }` from mixed log/JSON output; both CLI variants prefix
status lines to stdout, so don't `json.loads` the whole output.

## Auth posture

The CLI authenticates the operator (delegated user). It does **not** use
the T2 confidential-client secret directly — that secret backs the agent
runtime (activity bridge), not the CLI. Operator login is handled by
`a365 login` (or the underlying `az login` for tenant reads); this skill
does not attempt to drive login automatically.

## Drift handling

If a subcommand the skill calls is renamed or its flag set changes
(e.g. `--name=` → `--display-name=`):

1. The mutator's `_run` will return a non-zero exit code with stderr
   surfacing the unknown-flag error.
2. Update this file's "Subcommands consumed" row with the new flag.
3. Update the corresponding `A365CliMutator` method in `scripts/register.py`.
4. Bump the snapshot date.

Treat any change to CLI behaviour as a release-gating event per SPEC §14.1.
