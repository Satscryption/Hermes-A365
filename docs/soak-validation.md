# Path B soak validation runbook (#84)

Ordered Path B operator template. **Preparation only:** filing this does not schedule jobs, confirm a stand-up, or start the soak. Leave live fields **pending**; missing evidence cannot become **PASS**. [#84](https://github.com/Satscryption/Hermes-A365/issues/84) stays open while required evidence is missing.

Provisioning: [`quickstart-path-b.md`](quickstart-path-b.md). [#187](https://github.com/Satscryption/Hermes-A365/issues/187) walk resources stay unused.

**Order:** preflight → Path B scheduling → boundary probes (R4–R7, R6, R8) → daily review → client-visible record (R10) → restoration and report.

## 1 — Preflight (R1)

Scheduling before recorded confirmation is **failed preflight**. The [#187](https://github.com/Satscryption/Hermes-A365/issues/187) Walk owner records this block.

| Field | Value |
| --- | --- |
| Walk owner (preflight recorder) | |
| Stand-up alias / profile alias `<profile>` | |
| Q1 tuple: Hermes-A365 `0.9.8` git SHA, wheel+sdist digests, plugin version, Hermes upstream ref, Python, OS, `a365`, `az` | |
| Hermes build accepts §2 commands; delivery observed on gateway adapter for this process lifetime | |
| Fresh isolated Path B tenant (soak target); no walk resource in use | ☐ / ☐ |
| Effective soak settings; UTC soak start/end (when live); gateway process lifetime(s) | |
| Preflight signed (UTC) | |

Keep exact commands, job IDs, preflight evidence. Hours on a prior build pin do not count toward R3.

## 2 — Scheduling and inbound (R3)

**Scope:** ≥72 h observed gateway on one unchanged Q1 pin, excluding **all** downtime; one uninterrupted interval **>24 h**. One Path B profile; `gateway.multiplex_profiles` **false**. Two Path B conversations (Teams, Copilot Chat). Four native cron jobs (agent turn + script-only proactive per surface, 15-min cadence). **Authenticated Path B inbound** ≥ hourly per surface (operator message, not cron). Cron outbound does not satisfy inbound validation.

Verify against owner-confirmed Hermes build; stop if rejected. This repo does not vendor Hermes CLI. Job create / process exit is not delivery evidence. Record scheduled, attempted, and successful counts.

```text
hermes --profile <profile> cron create "*/15 * * * *" \
  "soak84 <surface> cron: return a unique marked acknowledgement" \
  --timezone UTC --deliver "agent365:<turn-chat>" --name "soak84-<surface>-turn"

hermes --profile <profile> cron create "*/15 * * * *" \
  --no-agent --script soak_driver.py \
  --timezone UTC --deliver "agent365:<proactive-chat>" --name "soak84-<surface>-proactive"
```

`<surface>` is `teams` or `copilot`. Copy `soak_driver.py` into that profile’s scripts directory.

**Missed-slot ledger (each 15-min slot):** | UTC | Job | Attempted | Gateway completion | Note |

**Hourly inbound ledger:** | UTC hour | Surface | Accepted (401/403 none) | Turn done | — unattempted hour → **NOT OBSERVED**.

## 3 — R4 token boundary (`_BfTokenCache`)

Path B only; process exit is cache-lifetime boundary. **PASS:** warm replacement at/after prior expiry minus 300 s skew (`activity_bridge.py:1050`, decision `:3449`), advancing expiry, marked delivery across boundary. **FAIL:** marked delivery worked before boundary, then failed across refresh eligibility/expiry — token acquisition fail (`adapter.py:11258`), POST exception (`:11305`), non-2xx (`:11323`), or no gateway-confirmed completion by next 15-min slot. Record cache kind `bf_s2s`, key digest, process lifetime, prior expiry, eligibility UTC, marker, delivery result/status. Concurrent **tunnel or hosting outage** is a separate recorded class; alone it is not this R4 FAIL. Cold fill ≠ PASS; cache fill does not override delivery failure; unattempted/uninstrumented → **NOT OBSERVED**. Gateway success, client miss → R10.

## 4 — R5 JWKS (Path B)

**Refresh PASS:** accepted Path B traffic + advancing `_bf_jwks_cache` `fetched_at` over 24 h TTL (`activity_bridge.py:968`, `fetched_at` field `:2092`). **Rollover PASS:** accepted traffic with changed signing-key digest. Review JWKS refresh-failure logs and Path B inbound-rejection logs. No live rollover → **NOT OBSERVED**. Offline two-key `validate_inbound_jwt_bf` test is supplemental.

## 5 — R6 prune (owned adapter, one profile)

Set `gateway.platforms.agent365.extra.conversations_prune_max_age_days` to **1/24** before constructing probe adapter (`adapter.py:1949`; prune `:5675`, log `:5707`). Two dormant chats (not cron targets): inbound seed → stop gateway (registry+port free) → while stopped `ConversationRegistry.load`/`pin`/`unpin`/`save` (`conversations.py:525`, `:399`, `:413`, `:554`); no JSON hand-edit.

```text
<Hermes-python> scripts/soak_driver.py prune-probe \
  --platform-config <local-effective-platform-config.json> \
  --targets <local-target-alias-map.json>
```

Harness keeps one `Agent365Adapter`, `connect()` (`:3666`), >1 h idle with recorded `last_used_at` on both targets and **no active or pending work** protecting the unpinned one. Await `prune_conversations()`; record return count, prune log (`:5707`), and sanitized presence/pin/age summaries. **PASS:** unpinned gone from memory and persistence; pinned remains. Before inbound this lifetime: `send` (`:5825`) on both — survivor delivers; removed target fails at proactive return (`:11192`) with **zero POSTs** (before token mint `:11244`, `client.post` `:11287`) per a harness-only request counter storing no content. Call `disconnect()` (`:3834`) on completion **or failure**. Do not run beside gateway on same registry; probe hours/`_bf_token_cache` exclude R3/R4. Restore threshold; start gateway; inbound restores removed chat; proactive before new inbound.

| Pre-init 1/24? | Unpinned pruned? | Pinned kept? | Zero POST removed? | Verdict |
| --- | --- | --- | --- | --- |
| | | | | |

## 6 — R7 idempotency

Retain cap/TTL (`activity_bridge.py:1001`, `:1011`). **PASS:** occupancy ≤ effective cap; after warm-up, expiry turnover in equivalent hourly windows; separate process lifetimes. Non-positive cap returns without retaining a record (`:2473`; cap-disable warning `:2407`). Review delivery, release, and full-cache log events and retained-result bytes. Else **NOT OBSERVED**.

## 7 — R8 restart

One **gateway restart** and one separate **tunnel restart** on an established Path B conversation (confirmed profile only — not walk profile/hosts named in `live-tenant-test.md`). Record interrupted-turn outcome + outage duration. After recovery: proactive to persisted cron targets before new inbound; marked follow-up per scheduled conversation (§9d.6 shape at `live-tenant-test.md:951`, `:967`). **PASS:** persisted routing + later Path B gateway traffic; in-memory interrupted turn need not resume. R10 on follow-ups. Classify tunnel/hosting faults; do not fix.

## 8 — R10 client-visible

Row per agent cron fire, script-only proactive fire, and R8 follow-up:

| Marker | UTC | Surface | Gateway result | Client: rendered / not rendered / not observed |
| --- | --- | --- | --- | --- |
| | | | | |

Citing #290, #337, or #338 explains a possible client limit when the gateway accepted the post; that adds no rendering work and does not change R4.

## 9 — Daily review, verdicts, restoration

Daily allowlisted excerpts only (no tokens, assertions, credentials, claims, cache values, bodies, raw ids). Record failures and coverage gaps as sanitized non-security child-issue references:

| Day | `_BfTokenCache` diagnostics | JWKS | Idempotency | Notes |
| --- | --- | --- | --- | --- |
| | | | | |

Also log interruptions and evidence references in the pending report.

| Req | PASS / FAIL / NOT OBSERVED | Evidence |
| --- | --- | --- |
| R3 | | |
| R4 | | |
| R5 | | |
| R6 | | |
| R7 | | |
| R8 | | |
| R10 | | |

**Restoration:** native job listing and status, then remove soak cron jobs; restore settings on isolated stand-up; release tunnel. Overall **PENDING** until rows filled. R9/R11 **retired**.