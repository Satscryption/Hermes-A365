# Path B soak checklist (#84)

Operator boundary checks for the live soak. Execute in the order in [`soak-validation.md`](soak-validation.md); this page names **observable evidence**, **verdict rules**, **R10 client-visible rows**, **daily review**, **allowlisted export**, and **restoration** only. Preparation rounds authorize no live action; every live field stays **pending** until an authorized run fills it.

## Verdict vocabulary (I4)

| Verdict | Meaning |
| --- | --- |
| PASS | Required observable evidence met for the boundary or row. |
| FAIL | Explicit failure rule triggered (e.g. R4 delivery failure across token refresh). |
| NOT OBSERVED | Coverage attempted but evidence missing, uninstrumented, or not yet reviewed. |

Missing evidence cannot become PASS. No blanket PASS while required rows are pending.

## R4 — `_BfTokenCache` token replacement

Path B only; process exit is the cache-lifetime boundary (`soak-validation.md` §3).

**Observable evidence (DEBUG `token_cache_fill`):** `cache_kind`, `key_digest`, `prev_expiry` (or cold marker), `eligibility_at`, `replacement_expiry`. Correlate with process lifetime, operation marker, prior expiry UTC, eligibility UTC, and gateway delivery outcome for the marked slot spanning the boundary.

**PASS:** warm replacement at or after prior expiry minus 300 s skew, advancing `replacement_expiry`, and successful **marked** delivery across the boundary. Cold fill alone is insufficient.

**FAIL:** marked delivery succeeded before the boundary, then failed across refresh eligibility or expiry — token acquisition failure, outbound POST exception, non-2xx response, or no gateway-confirmed completion by the next 15-minute slot. Record delivery result/status or bounded missing-completion note. Successful cache fill does not override delivery failure. Concurrent tunnel or hosting outage is a separate class (not this FAIL). Gateway success with client miss is **R10**, not R4.

**NOT OBSERVED:** unattempted or uninstrumented boundary window.

| Eligibility UTC | Key digest | Prev expiry | Replacement expiry | Marker | Delivery across boundary | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| | | | | | | |

## R5 — Path B JWKS refresh and rollover

**Observable evidence (DEBUG `jwt_validated`):** `validator=bf`, `jwks_fetched_at`, `jwks_keys`, `kid_digest`. Review JWKS refresh-failure logs and Path B inbound accept/reject logs. Offline two-key `validate_inbound_jwt_bf` test is supplemental only.

**Refresh PASS:** accepted Path B traffic with advancing `_bf_jwks_cache` `fetched_at` across the 24 h TTL.

**Rollover PASS:** accepted Path B traffic using a changed signing-key digest.

**NOT OBSERVED:** no live rollover observed (uptime and TTL refresh alone do not imply rollover).

| Window (UTC) | `jwks_fetched_at` trend | `kid_digest` change | Inbound accept/reject summary | Verdict (refresh) | Verdict (rollover) |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

## R6 — Prune (owned adapter probe)

Follow [`soak-validation.md`](soak-validation.md) §5. Threshold **1/24** day captured before adapter construction. Invocation:

```text
<Hermes-python> scripts/soak_driver.py prune-probe \
  --platform-config <local-effective-platform-config.json> \
  --targets <local-target-alias-map.json>
```

**Observable evidence:** `prune_conversations` return count, adapter prune log line, harness JSON (`pruned_count`, `target_summaries`, `removed_target_post_count`), sanitized pin/age/presence on disk and in memory, survivor vs removed `send` results. Probe hours and probe `_bf_token_cache` do not count toward R3/R4.

**PASS:** unpinned dormant target absent from memory and persistence; pinned target survives; removed-target `send` fails with **zero** outbound POSTs on that attempt; threshold restored; gateway restart; inbound restores removed chat; proactive succeeds before new inbound.

| Pre-init 1/24? | Unpinned pruned? | Pinned kept? | Zero POST removed? | Restored after gateway? | Verdict |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

## R7 — Idempotency occupancy

Retain existing cap and TTL. **Observable evidence (DEBUG `idempotency_size`):** `entries`, `cap`, `ttl_seconds`, `result_bytes`, plus delivery/release/full-cache log events. Compare equivalent hourly windows across separate process lifetimes.

**PASS:** occupancy never exceeds effective cap; after warm-up, expiry-driven turnover rather than unbounded growth.

**NOT OBSERVED:** insufficient windows or ambiguous cap-disable path without retained records.

| Process lifetime | Hour window (UTC) | Peak entries | Cap | Turnover noted? | Verdict |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

## R8 — Gateway and tunnel restart

One **gateway restart** and one separate **tunnel restart** on an established Path B conversation (confirmed profile only). Record interrupted-turn outcome and outage duration.

**PASS:** after recovery, proactive delivery to persisted cron targets **before** new inbound; marked follow-up on scheduled Teams and Copilot conversations; persisted routing and subsequent Path B gateway traffic. In-memory interrupted turn need not resume.

| Event | Outage duration | Interrupted turn | Proactive-before-inbound | Follow-up Teams | Follow-up Copilot | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Gateway restart | | | | | | |
| Tunnel restart | | | | | | |

## R10 — Client-visible (Teams and Copilot)

One row per agent cron fire, each script-only proactive fire (`soak84 proactive <surface> <UTC>` from `soak_driver.py` with `<surface>` `teams` or `copilot`), and each R8 follow-up. Gateway cron markers use the `soak84 <surface> cron:` prompt form from scheduling.

| Marker | UTC | Surface | Gateway result | Client: rendered / not rendered / not observed |
| --- | --- | --- | --- | --- |
| | | teams | | |
| | | copilot | | |

#290, #337, or #338 may cite possible client limits when the gateway accepted the post; that is evidence only and changes no boundary verdict.

## Daily review (R1)

Each UTC day, review DEBUG diagnostics and boundary tables above. Record only **allowlisted** excerpts (I2): aliases, digests, counts, timestamps, verdicts — no tokens, assertions, credentials, claims, cache values, bodies, or raw identity keys.

| Day (UTC) | `token_cache_fill` reviewed | `jwt_validated` reviewed | `idempotency_size` reviewed | Interruptions / notes |
| --- | --- | --- | --- | --- |
| | | | | |

## Allowlisted export (R1 / I2)

Exported excerpts and child-issue references use aliases and the fields named in this checklist and the pending report template. Omit secret-bearing log lines even when DEBUG is enabled.

## Restoration

When the soak ends on the isolated stand-up: reconcile and remove the four native soak cron jobs; restore `conversations_prune_max_age_days` and other effective settings; release tunnel resources per the runbook. Walk resources stay unused. Overall soak verdict remains **PENDING** until authorized execution fills evidence. R9 and R11 are **retired**.

## Requirement verdict summary (R1)

| Req | PASS / FAIL / NOT OBSERVED | Primary evidence pointer |
| --- | --- | --- |
| R3 | | Scheduling and inbound ledgers in runbook §2 |
| R4 | | § R4 table; `token_cache_fill` |
| R5 | | § R5 table; `jwt_validated` |
| R6 | | § R6 table; prune-probe JSON |
| R7 | | § R7 table; `idempotency_size` |
| R8 | | § R8 table; R10 follow-up rows |
| R10 | | § R10 table |