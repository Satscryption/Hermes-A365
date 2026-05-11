# Exposing the bot endpoint to Microsoft

**Snapshot:** 2026-05-06.

This is a non-prescriptive reference. The Hermes-A365 skill is
infrastructure-agnostic: the plugin (`hermes_a365.plugin`) and the
bridge (`hermes_a365.activity_bridge serve`) both listen on
`localhost:<port>`. *How* that port becomes a publicly reachable
HTTPS URL is the operator's choice, driven by the deployment
environment, security posture, and operational constraints — not
by anything the skill needs.

This file lists the common options with tradeoffs. Other options
work fine; this isn't an exhaustive list.

## What Microsoft requires

Microsoft Bot Framework (and therefore A365's MCP Platform when
routing activities) requires the messaging endpoint to be:

- **HTTPS** with a publicly trusted certificate.
- **Publicly reachable** from Microsoft's data-centre IP ranges.
- **Live** — BF retries on 5xx / timeouts but eventually gives up
  after a retry budget. There is no Microsoft-side queue / buffer.

Whatever option you pick must satisfy those three constraints.
Everything else (caching, auth at the proxy layer, observability,
CDN) is operator preference.

## Options

### Cloudflare quick tunnel

```bash
cloudflared tunnel --url http://localhost:3978
```

- ✅ Free, zero config, works in 30 seconds.
- ❌ Anonymous URL re-issued on every restart — must re-run
  `update-endpoint --apply` each time, which churns the messaging
  endpoint registration.
- ❌ Per Cloudflare's terms, "Quick Tunnels" have no uptime
  guarantee.

**When to use:** ad-hoc validation walkthroughs (this is what
the live-tenant runbook examples use). Not appropriate for any
deployment that's expected to outlive a single session.

### Cloudflare named tunnel

```bash
cloudflared tunnel create hermes-a365
cloudflared tunnel route dns hermes-a365 hermes.<your-domain>
cloudflared tunnel run hermes-a365 --url http://localhost:3978
```

- ✅ Stable URL on your domain — no `update-endpoint` churn across
  restarts.
- ✅ Free; same Cloudflare account that hosts your DNS works.
- ✅ Optional Cloudflare Access in front for Entra-auth gating at
  the tunnel layer.
- ⚠️ ~30 min one-time setup; Cloudflare account required.

**When to use:** any deployment that runs longer than a single
walkthrough but isn't ready for full Azure hosting.

### Microsoft devtunnels

```bash
devtunnel create hermes-a365 --allow-anonymous
devtunnel port create hermes-a365 -p 3978
devtunnel host hermes-a365
```

- ✅ Microsoft-blessed tunneling service. Integrates with the
  M365 Agents Toolkit and Visual Studio / VS Code.
- ✅ Stable per-tunnel URL; can be private (Entra-auth at the
  tunnel layer).
- ⚠️ Requires a Microsoft account; the tunnel-host process pins
  to that account's session.
- ⚠️ Subject to Microsoft's tunneling-service quotas.

**When to use:** if you're already in the Microsoft developer
toolchain (M365 Agents Toolkit, VS / VS Code), devtunnels is the
path of least friction. Same shape as cloudflared otherwise.

Reference: [Microsoft devtunnels overview](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/overview)

### ngrok

```bash
ngrok http 3978
```

- ✅ Familiar, well-documented.
- ⚠️ Free tier rotates URLs (same problem as cloudflared
  quick-tunnel); paid tier gets stable URLs.
- ⚠️ Adds a third-party dependency outside Microsoft / Cloudflare.

**When to use:** if your team already standardises on ngrok for
local-dev tunneling.

### Azure App Service / Container Apps / Functions

Deploy Hermes (with the `agent365` plugin loaded) to Azure as a
web app. The messaging endpoint becomes a stable
`https://<app>.azurewebsites.net/api/messages` (or a custom
domain).

- ✅ Production-grade. Stable URL, scaling, monitoring,
  centralised logs.
- ✅ Same Azure tenant as the A365 control plane; can use
  managed identity for outbound calls (in addition to or
  instead of the user-FIC chain — though our outbound design
  is currently keyed on the agentic user-FIC).
- ⚠️ Costs money. Adds a CD pipeline. Adds an operator runbook.
- ⚠️ This is a *Hermes-side* deployment question, not strictly a
  plugin one. The plugin doesn't change shape between local and
  Azure — only the URL fronting it does.

**When to use:** any deployment with real users on it. The
quick-tunnel and named-tunnel paths are for development +
validation; production lives in your cloud-of-choice's hosting.

### Reverse proxy in front of an existing service

If your tenant already has an HTTPS-terminating reverse proxy
(nginx, Caddy, Cloudflare, Azure Front Door, AWS ALB, etc.), you
can route a path on it to your Hermes host's `/api/messages`. The
plugin doesn't care.

The only correctness requirement is that the proxy preserves
the `Authorization` header (which carries the BF/AAD-v2 JWT) and
delivers the request body to the plugin without rewriting.

## Skill-side guarantees

These are the things the skill DOES guarantee, regardless of
which option above you pick:

- The endpoint listens on `localhost:<port>` (configurable via
  `HERMES_BRIDGE_PORT` or the plugin's `extra.port`).
- The path is `/api/messages` (BF standard; not configurable —
  Microsoft's connector is hard-coded to it).
- HTTP requests with a valid AAD-v2 JWT for the configured tenant
  + audience + `azp` are accepted; everything else is rejected
  with the appropriate 4xx (slices 19f / 19j).
- The endpoint is idempotent under BF retries (slice 19i).
- Response is delivered within 15 seconds for non-streaming
  paths or via the streaming protocol once #3 lands.

## Skill-side non-guarantees

These are explicitly outside the skill's scope:

- TLS termination, certificate management, certificate rotation.
- Geographic / region failover.
- Rate limiting at the network layer (the skill rate-limits at
  the activity layer where it matters — slice 19i dedupe, future
  #3's 1 req/s on streaming).
- DDoS / abuse mitigation at the network edge.
- Observability of network-layer concerns (request latency at
  the tunnel, dropped connections, etc.). The skill emits its own
  application-layer metrics; if you need network-edge metrics
  use whatever your reverse proxy provides.

If you find yourself wanting the skill to grow features in any of
those rows, that's a smell — the principle pinned in the project
memory says: **the a365 skill is the smallest possible thing that
does A365-specific work; everything else (Hermes' machinery,
operator infrastructure choices, model selection, hosting) is
delegated.**

## Cross-references

- Runbook walkthrough commands (live-tenant-test.md §9c, §9d.4)
  use Cloudflare quick-tunnel as the worked example. That's
  expedient for a 30-minute walkthrough; substitute any of the
  options above in production.
- README quick-start cites this file rather than prescribing a
  specific option.
- The `update-endpoint --apply` wrapper takes whatever URL you
  give it; it doesn't care which option produced the URL.
