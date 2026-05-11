# A365 license decision matrix

Snapshot date: 2026-05-05 (verified against live tenant during the
walkthrough; refreshed in slice 18o for SKU naming corrections)

Pricing source: Microsoft public list prices as of GA (2026-05-01).
Update this file when Microsoft publishes new pricing tiers; the
`hermes a365 license` command (SPEC §6.1) is read-only and surfaces a
recommendation without ever purchasing.

## SKUs

| Marketing name | `subscribedSkus` partNumber | List price | Bundles | Notes |
|---|---|---|---|---|
| Agent 365 add-on | `MICROSOFT_AGENT_365_TIER_3` (verified) | $15 / user / month | A365 governance + identity | Add-on to any M365 plan; no Copilot, no Defender, no Purview. The Graph `subscribedSkus` API exposes this as `MICROSOFT_AGENT_365_TIER_3`; tier numbering may differ in other regions. |
| Microsoft 365 E7 | (TBD — not seen in test tenant) | $99 / user / month | M365 + A365 + Copilot + Defender + Purview | Full enterprise bundle; cheapest path when adopting Copilot anyway. |

## Decision rule (encoded in `hermes_a365.license`)

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

## Verifying assigned licences

`hermes a365 license` is read-only — it never writes to
`~/.hermes/.env`. To check what's actually assigned to a user:

```bash
az rest --method GET \
    --url "https://graph.microsoft.com/v1.0/users/<upn>/licenseDetails" \
    --query "value[].skuPartNumber" -o json
```

For tenant-wide seat utilisation:

```bash
az rest --method GET --url "https://graph.microsoft.com/v1.0/subscribedSkus" \
    --query "value[].{sku:skuPartNumber, prepaid:prepaidUnits.enabled, consumed:consumedUnits}" \
    -o table
```

⚠️ Tenants that already hold a Microsoft 365 Business / Enterprise
SKU containing the `OFFICESUBSCRIPTION` service plan will collide
with `MICROSOFT_AGENT_365_TIER_3`'s `OFFICESUBSCRIPTION`. Disable
the conflicting plan when assigning Tier 3:

```bash
az rest --method POST \
    --url "https://graph.microsoft.com/v1.0/users/<upn>/assignLicense" \
    --headers "Content-Type=application/json" \
    --body '{"addLicenses":[{"skuId":"<tier-3-skuId>",
        "disabledPlans":["43de0ff5-c92c-492b-9116-175376d08c38"]}],
        "removeLicenses":[]}'
```

The conflicting service plan id (`43de0ff5-…`) is the GA Office
productivity SKU; suppressing it keeps the user's existing Office
plan intact.

## Admin centre purchase URLs

- Agent 365 add-on: <https://admin.microsoft.com/Adminportal/Home#/catalog>
- Microsoft 365 E7: <https://admin.microsoft.com/Adminportal/Home#/catalog/category/E7>

The skill does **not** open these automatically; it prints the URL with
the recommendation. Operator buys the licence, then re-runs
`hermes a365 doctor` once propagation completes (~5–30 min; SPEC §6.2
handles `AADSTS500011` retry-with-backoff).
