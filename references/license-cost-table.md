# A365 license decision matrix

Snapshot date: 2026-05-04

Pricing source: Microsoft public list prices as of GA (2026-05-01).
Update this file when Microsoft publishes new pricing tiers; the
`hermes a365 license` command (SPEC §6.1) is read-only and surfaces a
recommendation without ever purchasing.

## SKUs

| SKU | List price | Bundles | Notes |
|---|---|---|---|
| Agent 365 add-on | $15 / user / month | A365 governance + identity | Add-on to any M365 plan; no Copilot, no Defender, no Purview. |
| Microsoft 365 E7 | $99 / user / month | M365 + A365 + Copilot + Defender + Purview | Full enterprise bundle; cheapest path when adopting Copilot anyway. |

## Decision rule (encoded in `scripts/license.py`)

| Users | M365 plan | Recommendation |
|---|---|---|
| < 25 | any | Agent 365 add-on |
| ≥ 25 | < E5 | Agent 365 add-on |
| ≥ 25 | E5 + Copilot/Defender/Purview wanted | M365 E7 |
| ≥ 25 | E5 + add-on already covers needs | Agent 365 add-on |

The rule favours the add-on at small scale because E7 only pays off when
Copilot/Defender/Purview are also adopted. At 250 seats:
- Add-on: 250 × $15 × 12 = **$45,000 / yr**
- E7: 250 × $99 × 12 = **$297,000 / yr**

E7 is rarely cost-justified on A365 alone; flag with FinOps before
recommending it.

## Recorded as

The chosen SKU is recorded in `~/.hermes/.env` as:

```
A365_LICENSE_MODEL=per_agent  # add-on
A365_LICENSE_MODEL=e7         # E7 bundle
```

`hermes a365 status` surfaces the model alongside seat utilisation
(`<used> of <total>`) from `query-entra --license`.

## Admin centre purchase URLs

- Agent 365 add-on: <https://admin.microsoft.com/Adminportal/Home#/catalog>
- Microsoft 365 E7: <https://admin.microsoft.com/Adminportal/Home#/catalog/category/E7>

The skill does **not** open these automatically; it prints the URL with
the recommendation. Operator buys the licence, then re-runs
`hermes a365 doctor` once propagation completes (~5–30 min; SPEC §6.2
handles `AADSTS500011` retry-with-backoff).
