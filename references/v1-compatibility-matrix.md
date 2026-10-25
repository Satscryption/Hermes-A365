# v1.0 compatibility matrix

Snapshot date: 2026-10-24. Authoritative support policy for Hermes-A365
v1.0 ([#126](https://github.com/Satscryption/Hermes-A365/issues/126)).
Coordinates with the public-surface freeze ([#85](https://github.com/Satscryption/Hermes-A365/issues/85))
and the GA documentation sweep ([#87](https://github.com/Satscryption/Hermes-A365/issues/87)).
Live tenant validation versions are recorded on the #90 freeze walk.

Programmatic constants: `hermes_a365.compatibility`. `hermes a365 doctor`
probes the host against this matrix (Python, OS, Hermes floors, keychain).

## Python

| Role | Supported range | CI evidence |
| --- | --- | --- |
| **Standalone CLI / wheel** (`hermes-a365`, `pipx install`) | `>=3.11` (3.11–3.14 tested in CI; 3.15+ untested) | **Ubuntu:** ruff + full pytest on 3.11–3.14 |
| **Hermes gateway plugin** | **Python 3.14** in the Hermes venv | **Ubuntu 3.14** + **macOS 3.14** (same pytest suite; `overrides.txt` only when `hermes-agent[web]` is present — refs [#148](https://github.com/Satscryption/Hermes-A365/issues/148)) |

Hermes Agent upstream still declares `requires-python = ">=3.11,<3.15"` and
documents **3.14** as the supported gateway runtime. This package does not
lower the bridge security floors; see
[`hermes-runtime-compatibility.md`](hermes-runtime-compatibility.md).

## Host operating systems

| OS | v1 support | Secret store | Notes |
| --- | --- | --- | --- |
| **macOS** | Supported | `security` CLI (login keychain) | macOS keychain **writes** require `HERMES_A365_ALLOW_INSECURE_MACOS_KEYCHAIN_CLI=1` (secret in argv). Reads/deletes work by default. CI runs the full pytest suite on **macOS 3.14 only**; live keychain round-trips are **operator-verified** on an unlocked login keychain — GitHub-hosted macOS skips headless `security` writes. |
| **Linux** (glibc distros) | Supported | `secret-tool` (libsecret / Secret Service) | CI `keychain-linux` job exercises libsecret with a disposable Secret Service. Distros such as Ubuntu 22.04+ are **operator-verified**, not separate CI lanes. |
| **Windows** | **Not supported** | *None in this package* | The GA `a365` CLI may write blueprint secrets with DPAPI; Hermes-A365 does not implement Credential Manager and does not integrate DPAPI persistence. CI runs **negative probe tests only** on `windows-latest` (unsupported-host contract). End-to-end v1 flows require macOS or Linux. |

`doctor` reports **error** on Windows and on missing keychain backends.

## Hermes Agent (gateway plugin)

Two install contracts:

1. **Plugin baseline** — `agent365` entry point + `BasePlatformAdapter` lifecycle.
2. **Multiplex** — `gateway.multiplex_profiles: true` with isolated credentials per profile.

| Mode | Minimum Hermes (calendar tag) | Minimum Hermes Agent (semver) | Evidence |
| --- | --- | --- | --- |
| Plugin (single-profile or multiplex off) | `v2026.4.30` (Teams adapter ship tag; pairs with **0.12.0**) | `0.12.0` | CI Hermes source smoke (import + contract files); plugin unit tests |
| `gateway.multiplex_profiles: true` | `v2026.6.19` | `0.17.0` | Source smoke; multiplex config guard in `doctor` |
| Copilot→Teams session pin (resolution) | `v2026.9.7` | `0.21.1` | Documented in [`hermes-runtime-compatibility.md`](hermes-runtime-compatibility.md); `doctor` **warns** below floor |

**CI integration smoke refs:** `v2026.6.19` (multiplex floor) and `v0.22.5`
(current 3.14 gateway line; PyJWT 2.15.1 satisfies our floor). `scripts/hermes_compat_smoke.py` checks the
plugin adapter contract plus upstream multiplex phase-0 tests:
`gateway.multiplex_profiles` defaults **false** and a **two-profile** session
isolation smoke exists in Hermes source. A full two-profile live gateway walk
remains on the #90 gate.

**Recommended for new installs:** Hermes **v0.22.5** (or newer `v0.22.x`) on
Python 3.14:

```bash
uv pip install 'hermes-a365[bridge]'
```

Add `--override overrides.txt` only when `hermes-agent[web]` or `[all]` is in
the same environment (FastAPI pin; refs #148).

## Microsoft operator tooling

Tested against live walks (update snapshot dates when re-verified on #90):

| Tool | Tested / required | Doctor behaviour |
| --- | --- | --- |
| **A365 CLI** (`a365`, .NET tool) | Live-verified through **1.1.181**; NuGet may be newer | **Warn** on all builds until Microsoft#408 is walked clean; keep `register --auto-recover-secret` |
| **Azure CLI** (`az`) | **≥ 2.55.0**, signed in; exact build **recorded at final walk (#90)** | **Error** if missing; **warn** if not signed in |
| **PowerShell** (`pwsh`) | **7+**; exact build **recorded at final walk (#90)** | **Error** if missing |
| **.NET runtime** | **10+** when CLI installed via `dotnet tool`; exact SDK **recorded at final walk (#90)** | Not probed directly; install docs in [`a365-cli-reference.md`](a365-cli-reference.md) |

Record exact CLI builds used on the #90 walk in the closing report.

## Microsoft surfaces and tenant prerequisites

| Item | v1 declaration |
| --- | --- |
| **Frontier Preview** | Required; not auto-verified (`doctor` mentions enrollment URL) |
| **Path A manifest** | AI Teammate shape from GA `a365 publish` (`devPreview`; CLI-owned) |
| **Path B CEA manifest** | `manifestVersion` **1.27** (prompt starters + Copilot Chat); see [`manifest-schema-currency.md`](manifest-schema-currency.md) |
| **Licenses** | `MICROSOFT_AGENT_365_TIER_3` or documented E7 path — see [`license-cost-table.md`](license-cost-table.md) |

## What CI proves vs live-only

| Claim | Automated | Live (#90 / operator) |
| --- | --- | --- |
| Python 3.11–3.14 unit + lint (Ubuntu) | Yes | — |
| Python 3.14 unit + lint (macOS) | Yes | — |
| Windows unsupported-host probes | Yes (`windows-negative` job only) | — |
| macOS keychain round-trip | No (skipped on GitHub macOS runners) | Operator Mac with unlocked keychain |
| Linux libsecret round-trip | Yes when Secret Service available (`keychain-linux`) | — |
| Wheel/sdist build + fresh install smoke | Yes | — |
| Hermes source tags (import + adapter contract) | Yes (pinned refs) | Full gateway multiplex traffic |
| Teams / Copilot UI round-trips | — | #90 feature matrix |

## Related references

- [`hermes-runtime-compatibility.md`](hermes-runtime-compatibility.md) — Hermes v0.22.5 PyJWT resolve + FastAPI override + session-pin floors
- [`a365-cli-reference.md`](a365-cli-reference.md) — real `a365` subcommands and CLI drift
- [`live-tenant-test.md`](live-tenant-test.md) — operator walk (uses versions declared here)
