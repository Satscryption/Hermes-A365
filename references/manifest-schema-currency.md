# Manifest schema currency — 1.21 CEA pin vs 1.25 / 1.27

Snapshot date: 2026-06-30

Investigation for #75. **Outcome: no schema bump in v0.7.6.** The default
Custom Engine Agent manifest version stays `1.21`; this file records what
the newer schema versions add, whether `hermes-a365` needs them, and the
criteria for a future, walk-validated bump.

This is a *snapshot* of an external surface (the Teams app-manifest schema
and Microsoft's docs) per the [`references/` convention](README.md) — the
live surface is authoritative; update + re-date this file when it drifts.

## TL;DR

- v0.7.5 **live-validated** the `1.21` Custom Engine Agent publish + Copilot
  Chat surface path on 2026-06-30. `1.21` is current-as-of-release.
- A "bump 1.21 → 1.27" is a **category error**: the issue conflates two
  *separate* manifest tracks (see below). Neither newer feature maps to a
  capability `hermes-a365` authors today.
- The minimal, correct deliverable is this doc. No default bump, no opt-in
  `--manifest-version` flag (an un-validatable knob on a validated path —
  when a bump is justified it is a one-line constant edit, no easier to
  pre-stage now than at walk time).

## Two manifest tracks — they are not the same version line

The wrapper emits two different manifest shapes for the two surfaces, and
only **one** of them carries the `1.21` pin:

| Track | Wrapper command | Shape | `manifestVersion` | Who authors the version |
|---|---|---|---|---|
| **Path A — AI Teammate** | `publish --aiteammate` | `agenticUserTemplates` block | `devPreview` | the GA `a365` CLI emits it; the wrapper passes it through untouched |
| **Path B — Custom Engine Agent** | `publish --copilot-chat` | `bots` + `copilotAgents.customEngineAgents` | **`1.21`** (`_COPILOT_CHAT_MANIFEST_VERSION`) | the wrapper, in `_transform_manifest_to_copilot_chat` |

Crucially, the Copilot Chat transform **strips `agenticUserTemplates`**
(`publish.py`, in `_transform_manifest_to_copilot_chat`) because it is the
AI-Teammate shape and is not part of the `1.21+` CEA schema. So:

- **`1.25 agenticUserTemplates`** is about the **Path A** schema, which the
  wrapper does *not* version — it forwards whatever the installed CLI emits
  (`devPreview` today). A change there is a GA-CLI / Path A concern, not a
  CEA-pin bump.
- **`1.27 agentConnectors`** is a manifest-level MCP surface. `hermes-a365`
  wires MCP through the `a365` CLI (`develop` / `develop-mcp`,
  `ToolingManifest.json`, `setup permissions mcp`), **not** through the
  publish manifest. We do not author `agentConnectors` at all today;
  introducing it would be a new feature, not a currency fix.

## Current state in the wrapper

`_COPILOT_CHAT_MANIFEST_VERSION = "1.21"` has exactly **three** internal use
sites and no cross-module consumers:

- `publish.py` — the constant definition
- `publish.py` — `out["manifestVersion"] = _COPILOT_CHAT_MANIFEST_VERSION` in the transform
- `publish.py` — the apply-summary `"manifest_version"` line (operator-facing only)

So the constant is fully internal; moving it is a localized edit guarded by
the existing tests (`test_bumps_manifest_version_to_1_21` and the
integration assertions that pin `manifestVersion == "1.21"`).

## Is `1.21` still accepted?

Yes, as of this snapshot. Microsoft documents Custom Engine Agents as
supported in "app manifest version 1.21 and later" (recorded in
[`m365-surface-coverage.md`](m365-surface-coverage.md)), and the v0.7.5
walk published a `1.21` CEA zip that validated and surfaced in Copilot Chat
and Teams. The CEA capabilities the wrapper emits (`bots`, streaming via
`streaminfo`, `copilotAgents.customEngineAgents`, command lists / prompt
starters) are all expressible at `1.21`.

## Do we need 1.25 / 1.27?

No named `hermes-a365` capability currently requires a `1.25`- or
`1.27`-only field:

- The prompt-starters work (#74) is `type:"prompt"` on bot command lists,
  which is expressible in the current schema we already emit.
- We do not surface MCP through the publish manifest (`agentConnectors`),
  and Path A's `agenticUserTemplates` is CLI-authored.

The honest position is **"no bump needed; revisit when a concrete feature
requires it"** rather than pre-building bump machinery.

## Open validation item (deferred to the #89 v0.8.1 walk)

The core compatibility question can only be answered operator-side, at the
Microsoft Admin Center, with a real upload:

> When handed a `manifestVersion` of `1.25` / `1.27`, does the Admin Center
> **accept-and-ignore** unknown blocks, or **hard-reject** the package?

This is recorded as an open item for the #89 walk and must **not** be
asserted here — it is unverified until walked.

## Bump criteria + owner

Move `_COPILOT_CHAT_MANIFEST_VERSION` off `1.21` only when **both** hold:

1. A concrete, committed Copilot Chat / Teams capability we need cannot be
   expressed at `1.21` (name it in the motivating issue), **and**
2. The higher-version manifest has been **live-validated** at the Admin
   Center on a walk (the #89 gate or a successor).

The bump itself is a one-line constant change plus a test update; it should
ride the walk that validates it, not a blind release. Owner: whoever runs
the motivating walk.
