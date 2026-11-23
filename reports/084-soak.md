# Path B soak report (#84) — pending template

**Status:** This template records **no live result**. Filing or completing preparation does **not** schedule jobs, start the soak, or close [#84](https://github.com/Satscryption/Hermes-A365/issues/84).

Procedures: [`docs/soak-validation.md`](../docs/soak-validation.md). Boundary rules: [`docs/soak-checklist.md`](../docs/soak-checklist.md). Leave every live field **pending** or blank; missing evidence cannot become **PASS**.

## 1 — Preflight (R1)

Scheduling before recorded confirmation is **failed preflight**.

| Field | Value |
| --- | --- |
| Walk owner (preflight recorder) | |
| Stand-up alias / profile alias `<profile>` | |
| Q1 tuple: Hermes-A365 `0.9.8` git SHA, wheel+sdist digests, plugin version, Hermes upstream ref, Python, OS, `a365`, `az` | *pending* |
| Hermes build accepts runbook §2 commands; delivery observed on gateway adapter for this process lifetime | |
| Fresh isolated Path B tenant (soak target); no walk resource in use | ☐ / ☐ |
| Effective soak settings | *pending* |
| UTC soak start / end (when live) | *pending* / *pending* |
| Gateway process lifetime(s) | *pending* |
| Preflight signed (UTC) | |

## 2 — Scheduling and inbound (R3)

One Path B profile; `gateway.multiplex_profiles` **false**. Four jobs at 15-minute cadence; authenticated inbound ≥ hourly per surface. Counts below stay **pending** until live execution.

| Job name | Surface | Scheduled | Attempted | Successful |
| --- | --- | --- | --- | --- |
| `soak84-teams-turn` | teams | *pending* | *pending* | *pending* |
| `soak84-teams-proactive` | teams | *pending* | *pending* | *pending* |
| `soak84-copilot-turn` | copilot | *pending* | *pending* | *pending* |
| `soak84-copilot-proactive` | copilot | *pending* | *pending* | *pending* |

| UTC hour | Surface | Inbound attempted | Accepted | Note |
| --- | --- | --- | --- | --- |
| *pending* | teams | | | |
| *pending* | copilot | | | |

Missed 15-minute slots: runbook §2 missed-slot ledger (*pending*).

## 3 — R4 token boundary (`_BfTokenCache`)

Boundary table and verdict: checklist § R4 (*pending*).

## 4 — R5 JWKS (Path B)

JWKS table and verdict: checklist § R5 (*pending*).

## 5 — R6 prune (owned adapter, one profile)

Probe record fields: `pruned_count`, `pinned_alias`, `unpinned_alias`, `target_summaries`, `removed_target_post_count`, … (see `scripts/soak_driver.py`). Verdict table: checklist § R6 (*pending*).

## 6 — R7 idempotency

Occupancy table: checklist § R7; DEBUG `idempotency_size` (*pending*).

## 7 — R8 restart

Restart table: checklist § R8 (*pending*).

## 8 — R10 client-visible

Row per agent cron fire, script-only proactive fire, and R8 follow-up. Live run appends through soak end (288 fires per job in 72 h). Markers: `soak84 <surface> cron: …`; proactive `soak84 proactive proactive <pid> <UTC>` (no-arg script); or `soak84 proactive <surface> <UTC>`.

| Marker | UTC | Surface | Gateway result | Client: rendered / not rendered / not observed |
| --- | --- | --- | --- | --- |
| | | teams | | |
| | | teams | | |
| | | copilot | | |
| | | copilot | | |
| *R8 gateway follow-up* | | teams | | |
| *R8 gateway follow-up* | | copilot | | |
| *R8 tunnel follow-up* | | teams | | |
| *R8 tunnel follow-up* | | copilot | | |

## 9 — Daily review, verdicts, restoration

| Day (UTC) | `_BfTokenCache` diagnostics | JWKS | Idempotency | Notes |
| --- | --- | --- | --- | --- |
| | | | | |

**Interruptions** (aliases only):

| UTC start | UTC end | Class | Note |
| --- | --- | --- | --- |
| | | | |

**Evidence references** (aliases only; allowlisted export):

| Alias | Kind | Pointer |
| --- | --- | --- |
| | | |

| Req | PASS / FAIL / NOT OBSERVED | Evidence |
| --- | --- | --- |
| R3 | *pending* | §2 tables |
| R4 | *pending* | Checklist R4 |
| R5 | *pending* | Checklist R5 |
| R6 | *pending* | Checklist R6; prune-probe JSON |
| R7 | *pending* | Checklist R7 |
| R8 | *pending* | Checklist R8; §8 follow-up rows |
| R9 | **retired** | — |
| R10 | *pending* | §8 client-visible table |
| R11 | **retired** | — |

**Restoration:** *pending* — remove four soak cron jobs; restore settings; release tunnel (runbook §9). Overall verdict **PENDING**.