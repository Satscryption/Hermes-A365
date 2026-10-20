# v1.0 compatibility matrix

Snapshot date: 2026-10-20. Authoritative support policy for Hermes-A365
v1.0 ([#126](https://github.com/Satscryption/Hermes-A365/issues/126)).
Coordinates with the public-surface freeze ([#85](https://github.com/Satscryption/Hermes-A365/issues/85))
and the GA documentation sweep ([#87](https://github.com/Satscryption/Hermes-A365/issues/87)).
Live tenant validation versions are recorded on the #90 freeze walk.

Programmatic constants: `hermes_a365.compatibility`. `hermes a365 doctor`
probes the host against this matrix (Python, OS, Hermes floors, keychain).

## Python

| Role | Supported range | CI evidence |
| --- | --- | --- |
| **Standalone CLI / wheel** (`hermes-a365`, `pipx install`) | `>=3.11,<3.15` (3.11–3.14 tested) | Ubuntu + macOS + Windows: pytest + ruff per version |
| **Hermes gateway plugin** | **Python 3.14** in the Hermes venv | Same CI on 3.14; combined install uses `overrides.txt` ([#148](https://github.com/Satscryption/Hermes-A365/issues/148)) |

Hermes Agent declares `requires-python = ">=3.11,<3.15"` and documents **3.14**
as the supported gateway runtime. This package does not lower the bridge
security floors; see [`hermes-runtime-compatibility.md`](hermes-runtime-compatibility.md).

## Host operating systems

| OS | v1 support | Secret store | Notes |
| --- | --- | --- | --- |
| **macOS** (13+) | Supported | `security` CLI (login keychain) | macOS keychain **writes** require `HERMES_A365_ALLOW_INSECURE_MACOS_KEYCHAIN_CLI=1` (secret in argv). Reads/deletes work by default. CI runs the full pytest suite on macOS; **live keychain round-trips** need an unlocked login keychain (operator Mac) — GitHub-hosted macOS cannot complete headless writes. |
| **Linux** (glibc distros, e.g. Ubuntu 22.04+) | Supported | `secret-tool` (libsecret / Secret Service) | CI `keychain-linux` job exercises libsecret with a disposable Secret Service. |
| **Windows** | **Not supported** | *None in this package* | The GA `a365` CLI may write blueprint secrets with DPAPI; Hermes-A365 does not implement Credential Manager and does not integrate DPAPI persistence. End-to-end v1 flows require macOS or Linux. |

`doctor` reports **error** on Windows and on missing keychain backends.

## Hermes Agent (gateway plugin)

Two install contracts:

1. **Plugin baseline** — `agent365` entry point + `BasePlatformAdapter` lifecycle.
2. **Multiplex** — `gateway.multiplex_profiles: true` with isolated credentials per profile.

| Mode | Minimum Hermes (calendar tag) | Minimum Hermes Agent (semver) | Evidence |
| --- | --- | --- | --- |
| Plugin (single-profile or multiplex off) | `v2026.4.30` | `0.12.0` | Source smoke in CI; plugin unit tests |
| `gateway.multiplex_profiles: true` | `v2026.6.19` | `0.17.0` | Source smoke; multiplex config guard in `doctor` |
| Copilot→Teams session pin (resolution) | `v2026.9.7` | `0.21.1` | Documented in [`hermes-runtime-compatibility.md`](hermes-runtime-compatibility.md); `doctor` **warns** below floor |

**CI integration smoke refs:** `v2026.6.19` (multiplex floor) and `v0.22.2`
(current 3.14 gateway line). A full two-profile live multiplex walk remains
on the #90 gate.

**Recommended for new installs:** latest `v0.22.x` / `v2026.9.x` Hermes on
Python 3.14 with:

```bash
uv pip install --override overrides.txt 'hermes-a365[bridge]'
```

## Microsoft operator tooling

Tested against live walks (update snapshot dates when re-verified on #90):

| Tool | Tested / required | Doctor behaviour |
| --- | --- | --- |
| **A365 CLI** (`a365`, .NET tool) | Live-verified through **1.1.181**; NuGet may be newer | **Warn** on all builds until Microsoft#408 is walked clean; keep `register --auto-recover-secret` |
| **Azure CLI** (`az`) | **≥ 2.55.0**, signed in | **Error** if missing; **warn** if not signed in |
| **PowerShell** (`pwsh`) | **7+** | **Error** if missing |
| **.NET runtime** | **10+** when CLI installed via `dotnet tool` | Not probed directly; install docs in [`a365-cli-reference.md`](a365-cli-reference.md) |

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
| Python 3.11–3.14 unit + lint | Yes | — |
| macOS / Linux / Windows pytest | Yes (Windows = unsupported **host**; tests run on runner) | — |
| macOS keychain round-trip | Full suite on macOS runner; live store/get/delete on operator Mac with unlocked keychain | GitHub macOS blocks headless `security` writes |
| Linux libsecret round-trip | Yes when Secret Service available | — |
| Wheel/sdist build + fresh install smoke | Yes | — |
| Hermes source tags + plugin import path | Yes (pinned refs) | Full gateway multiplex traffic |
| Teams / Copilot UI round-trips | — | #90 feature matrix |

## Related references

- [`hermes-runtime-compatibility.md`](hermes-runtime-compatibility.md) — PyJWT / FastAPI overrides + session-pin floors
- [`a365-cli-reference.md`](a365-cli-reference.md) — real `a365` subcommands and CLI drift
- [`live-tenant-test.md`](live-tenant-test.md) — operator walk (uses versions declared here)
