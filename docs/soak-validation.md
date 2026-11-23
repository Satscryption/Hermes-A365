# Path B soak validation runbook (#84)

Ordered operator template for a multi-day Path B gateway soak. **Preparation only:** completing or filing this document does not schedule jobs, stand up a tenant, or start the soak. Leave every live field **pending** until recorded evidence exists; missing evidence cannot become **PASS**.

Refs [#84](https://github.com/Satscryption/Hermes-A365/issues/84). Path B provisioning context: [`quickstart-path-b.md`](quickstart-path-b.md), [`references/live-tenant-test.md`](../references/live-tenant-test.md) §11. Walk resources stay on [#187](https://github.com/Satscryption/Hermes-A365/issues/187).

**Execution order:** preflight confirmation → Path B scheduling → boundary probes → daily review → client-visible record → restoration and report.

## 1 — Preflight confirmation (R1)

Record before any soak scheduling. Scheduling before this block is signed is a **failed preflight**.

| Field | Recorded value |
| --- | --- |
| Walk owner (#187) | |
| Stand-up alias (isolated Path B tenant) | |
| Soak Hermes profile alias | |
| Owner-confirmed Hermes build (tag / commit) | |
| Owner-confirmed `a365` CLI version | |
| Fresh isolated Path B tenant confirmed | ☐ yes (soak target) |
| No walk resource in use for soak window | ☐ yes |
| Preflight signed (UTC) | |

**Verdict (R1):** **PASS** only when every row is filled and both boxes are checked. **FAIL** if scheduling or live probes begin first.

## 2 — Path B scheduling (R3)

**Scope:** 72 hours of **observed gateway operation** on one **unchanged** Hermes-A365 + Hermes build, **excluding** operator downtime. Require at least one uninterrupted interval **> 24 h**. One Path B profile; leave `gateway.multiplex_profiles` **false**.

**Four Hermes cron jobs** (native scheduler, 15-minute cadence): on each of two persisted targets (**Teams** Path A 1:1 and **Copilot Chat**), one **agent-backed turn** and one **script-only proactive** send. Distinct conversation ids and job names. Cron-generated outbound exercises delivery and registry persistence; it **does not** satisfy the Path B **inbound** validator (§3).

Verify every command form below against the owner-confirmed Hermes build during preflight. This repo does not vendor the Hermes CLI. Job creation or a zero exit from `hermes cron create` is **not** delivery evidence.

### Teams target (`<teams-conv-id>`)

```bash
hermes cron create "every 15m" \
  'Soak084 Teams agent turn: reply with one line "soak084-teams-turn" and the current UTC ISO-8601 timestamp.' \
  --name soak084-teams-agent-turn \
  --deliver agent365:<teams-conv-id>

hermes cron create "every 15m" \
  --no-agent \
  --script soak084-proactive-teams.sh \
  --name soak084-teams-script-proactive \
  --deliver agent365:<teams-conv-id>
```

### Copilot Chat target (`<copilot-conv-id>`)

```bash
hermes cron create "every 15m" \
  'Soak084 Copilot agent turn: reply with one line "soak084-copilot-turn" and the current UTC ISO-8601 timestamp.' \
  --name soak084-copilot-agent-turn \
  --deliver agent365:<copilot-conv-id>

hermes cron create "every 15m" \
  --no-agent \
  --script soak084-proactive-copilot.sh \
  --name soak084-copilot-script-proactive \
  --deliver agent365:<copilot-conv-id>
```

Install `soak084-proactive-*.sh` under `~/.hermes/scripts/` on the soak profile (non-empty stdout → proactive `sendToConversation` per [`adapter.py`](../src/hermes_a365/plugin/adapter.py) `_send_proactive`).

**Observed hours (exclude downtime):**

| Interval start (UTC) | Interval end (UTC) | Gateway up? | Notes |
| --- | --- | --- | --- |
| | | | |

**Verdict (R3):** **PASS** when summed observed hours ≥ 72, one row is uninterrupted > 24 h, four jobs remain scheduled on unchanged build, and gateway logs show periodic agent turns plus proactive sends on both targets. **FAIL** otherwise.

## 3 — Inbound validator (Path B, with R3)

At least **hourly authenticated Path B inbound** on **each** target (operator-initiated message, not cron). Marker example: `soak084-inbound-<surface> <UTC>`.

| Time (UTC) | Surface | Inbound accepted (no 401/403)? | Turn completed? |
| --- | --- | --- | --- |
| | Teams | | |
| | Copilot | | |

**Verdict:** **PASS** when ≥ 72 h of hourly rows exist per surface with JWT validation success (`validate_inbound_jwt_bf` branch per [`activity_bridge.py`](../src/hermes_a365/activity_bridge.py)). **FAIL** on sustained rejections or gaps.

## 4 — Boundary probes (R6)

**Registry prune** — owned soak adapter vs separate **probe** adapter. Shortened threshold is read at `Agent365Adapter` init (`conversations_prune_max_age_days`, default 30 — [`adapter.py`](../src/hermes_a365/plugin/adapter.py) init and `prune_conversations` at `:5675`). Set the shortened value in `platforms.agent365.extra` **before** constructing the probe adapter. The probe is a **separate live gateway**; its hours do **not** count toward R3. Do **not** assume a running-gateway periodic prune hook — wire `await adapter.prune_conversations()` explicitly (cron one-shot or driver).

**Owned-adapter procedure:**

1. On the probe profile, set `conversations_prune_max_age_days` to the owner-agreed shortened days (example `0.001`).
2. Seed registry entries (stale, pinned, in-flight); record `conversations.json` fingerprint.
3. Invoke `await adapter.prune_conversations()` once; capture log line `agent365 prune_conversations: dropped …`.
4. Assert stale unpinned entries dropped; pinned / active / `last_used_at is None` retained per [`conversations.py`](../src/hermes_a365/plugin/conversations.py) `prune_old_entries`.

**Driver invocation (when present):**

```bash
python scripts/soak_driver.py prune-probe \
  --soak-profile <soak-profile-alias> \
  --probe-profile <probe-profile-alias>
```

**Evidence:** | Prune threshold set pre-init? | Entries dropped? | Pinned retained? | Probe isolated from soak gateway? | **Verdict** |
| --- | --- | --- | --- | --- |
| | | | | **PASS** / **FAIL** |

## 5 — Restart and tunnel boundaries (R8)

During the soak window, execute **once per surface** (log only; do not fix tunnel/hosting faults):

1. **Gateway restart** — stop/start Hermes gateway; note PID and registry reload.
2. **Tunnel restart** — restart cloudflared (or named tunnel); re-confirm public URL if ephemeral.
3. **Proactive before inbound** — on each persisted Path B cron target, confirm one proactive delivery succeeds **before** the next operator inbound on that chat ([`send`](../src/hermes_a365/plugin/adapter.py) proactive path via `_send_proactive` at `:11167`).
4. **Marked follow-up** — after restart, send one marked inbound per scheduled conversation; confirm reply chain.

Classify tunnel/hosting failures as **observed external** (record timestamps; out of scope to repair).

| Event (UTC) | Surface | Proactive OK pre-inbound? | Follow-up turn OK? | Tunnel/hosting note |
| --- | --- | --- | --- | --- |
| | | | | |

**Verdict (R8):** **PASS** when all four steps are documented for both surfaces. **FAIL** if proactive recovery is untested after restart.

## 6 — Client-visible delivery (R10)

For **every** scheduled cron turn and every R8 follow-up, record client UI outcome:

| Event id | Surface | Scheduled / follow-up | Gateway send OK? | Client: rendered / not rendered / not observed |
| --- | --- | --- | --- | --- |

A **gateway success** with a **client miss** is **R10 evidence**, not an R4 failure, and does not trigger extra rendering work. **R4 failure** (gateway/outbound): log shows send error, non-2xx, or `delivery_failed` on the cron job — file separately.

**Verdict (R10):** **PASS** when the table is complete for the soak window. **FAIL** on missing rows.

## 7 — Daily subsystem review

Once per soak day, review allowlisted exports (gateway log excerpts, `hermes cron runs`, sanitized registry snapshots — no secrets/tokens):

| Day | Token caches (FMI / user / BF) | JWKS refresh | Idempotency cache growth | Registry / prune signals | Notes |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

**Verdict:** **PASS** when each day has a row. Anomalies become follow-up issues; they do not auto-fail R3 unless gateway observed hours drop.

## 8 — Restoration and report

Teardown: pause/remove soak cron jobs, restore default prune threshold on probe profile, release tunnel, document profile env reversions. Export allowlisted evidence bundle for [#84](https://github.com/Satscryption/Hermes-A365/issues/84) comment (not in-repo secrets).

**Overall soak verdict:** **PENDING** until R1, R3, R6, R8, and R10 are all **PASS** with recorded evidence. R9 and R11 remain retired — do not reopen.