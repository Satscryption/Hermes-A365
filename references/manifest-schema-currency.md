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
- The issue's `1.25 agenticUserTemplates` / `1.27 agentConnectors` framing
  conflates two *separate* manifest tracks (see below) — neither maps to a
  capability we author today. **But** a *different* 1.27 feature — bot
  command-list **prompt starters** (`type:"prompt"` + `prompt`) — IS a
  capability we want: it is **#74**, and it is the concrete motivator for a
  future `1.27` bump. (`type`/`prompt` were introduced in manifest **1.27**;
  the `1.21` command schema is `additionalProperties:false`, `title` +
  `description` only.)
- The minimal, correct **v0.7.6** deliverable is this doc: **no bump, no
  opt-in flag**. The `1.27` bump + #74's prompt starters land together,
  **walk-validated at the #89 v0.8.1 walk** — a schema-version bump must not
  ship blind on a unit-tests-only release.

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
and Teams. The CEA capabilities the wrapper emits today (`bots`, streaming
via `streaminfo`, `copilotAgents.customEngineAgents`, and command lists
restricted to `{title, description}`) are all expressible at `1.21`.

Note what is **not** expressible at `1.21`: bot command-list **prompt
starters** (`type:"prompt"` + `prompt`). Verified against Microsoft's
published v1.21 schema, the `commandLists.commands` item is
`{additionalProperties: false, properties: {title, description}, required:
[title, description]}` — extra fields are rejected. `type`/`prompt` were
introduced in manifest **1.27**. (Surfaced by the v0.7.6 red-team, which
caught an initial #74 attempt that emitted those fields under a `1.21`
package; #74 was deferred to land with the bump.)

## Do we need 1.25 / 1.27?

Yes — for one concrete capability: **prompt starters (#74)** require
manifest **1.27** (`type:"prompt"` is a 1.27 field, per the schema above).
That is the named, committed capability that justifies the bump.

The other two framings do *not* drive a Path B bump:

- We do not surface MCP through the publish manifest (`agentConnectors`),
  and Path A's `agenticUserTemplates` is CLI-authored (`devPreview`).

So the honest position is **"bump to 1.27 to ship #74 — but only once it is
walk-validated"**, not "no bump ever" and not a blind bump now.

## Open validation item (deferred to the #89 v0.8.1 walk)

The concrete v0.8.1 item: publish a **`1.27`** CEA manifest carrying #74's
`type:"prompt"` prompt starters and confirm at the Admin Center that it
(a) uploads and (b) renders the starters in the Copilot Chat zero-state.
Open until walked — do **not** assert the outcome here.

## Bump criteria + owner

Move `_COPILOT_CHAT_MANIFEST_VERSION` off `1.21` only when **both** hold:

1. A concrete, committed Copilot Chat / Teams capability we need cannot be
   expressed at `1.21`. **This is now satisfied: prompt starters (#74)
   require `1.27`.**
2. The higher-version manifest has been **live-validated** at the Admin
   Center on a walk (the #89 gate). *Not yet satisfied* — this is the
   remaining gate.

The bump itself is a one-line constant change plus #74's command-list
emission and a test update; it should ride the #89 walk that validates it,
not a blind release. Owner: whoever runs the #89 walk.
