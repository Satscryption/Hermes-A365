# `references/`

Reference material for the `hermes-a365` skill. Most files here are
**dated snapshots** of an external surface (Microsoft Agent 365 CLI,
Entra error codes, blueprint properties, OpenTelemetry vocabulary, Bot
Framework activity shapes, license catalog) — *not* a source of truth.
`live-tenant-test.md` is the operator runbook for end-to-end validation.

The doctor (`hermes_a365.doctor`) and individual subcommands cross-check
the live surface against these snapshots and warn on drift. When drift
is detected, the live surface is authoritative; update the corresponding
file here and bump the snapshot date.

Snapshot date convention: every snapshot file carries a top-level
`Snapshot date: YYYY-MM-DD` line. The skill's release notes call out
which references files were refreshed.

## Index

| File | Covers | Spec |
|---|---|---|
| [`a365-cli-reference.md`](a365-cli-reference.md) | A365 CLI variants, version pins, subcommands used | §6.2.2, §7.4 |
| [`error-codes.md`](error-codes.md) | AADSTS / A365 error codes the skill detects | §6.2, §9.1 |
| [`entra-blueprint-properties.md`](entra-blueprint-properties.md) | Known A365 blueprint properties | §6.4 |
| [`opentelemetry-config.md`](opentelemetry-config.md) | A365 span schema + canonical event vocabulary | §6.8 |
| [`activity-protocol-shapes.md`](activity-protocol-shapes.md) | Bot Framework activity shapes (Teams/Outlook/Copilot) | §6.7, §14.1 |
| [`license-cost-table.md`](license-cost-table.md) | A365 add-on / E7 pricing decision matrix | §6.1 |
| [`live-tenant-test.md`](live-tenant-test.md) | End-to-end runbook against a Frontier-Preview tenant (operator-side) | — |
| [`webhook-contract.md`](webhook-contract.md) | Stable JSON contract between `activity-bridge serve` and the operator's responder (slice 19b) | — |
| [`exposing-the-bot-endpoint.md`](exposing-the-bot-endpoint.md) | Operator-side options for exposing the local bridge to Microsoft (cloudflared / devtunnels / ngrok / reverse-proxy) | — |
| [`m365-surface-coverage.md`](m365-surface-coverage.md) | Per-surface coverage matrix for the `agent365` plugin (Teams 1:1, group/channel, Copilot Chat, Outlook) | — |
| [`manifest-schema-currency.md`](manifest-schema-currency.md) | Teams app-manifest schema versions — 1.21 CEA pin vs 1.25/1.27 features, bump criteria | §6.2.2 |
