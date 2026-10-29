# Client secret rotation (blueprint and Bot Framework)

Snapshot date: 2026-10-29

Hermes-A365 reads **only** ``passwordCredentials[].endDateTime`` from Microsoft
Graph during ``hermes-a365 doctor`` and ``hermes-a365 status``. Secret values
are never fetched or printed. When a secret is within **30 days** of expiry
(configurable with ``--secret-expiry-warn-days N``), those commands **warn**;
when it is expired or missing, they **fail** the check.

## Blueprint (agent identity) client secret

The blueprint secret is minted during ``hermes a365 register`` (default
lifetime is two years). Rotation options:

1. **Re-run register (preferred when the skill owns the app).**

   ```bash
   hermes a365 register --apply --agent-name <slug> --tenant-id <tenant-guid>
   ```

   When the blueprint is already owned and reconciled, register may no-op
   other steps; if the stored secret is missing or rejected at runtime, a
   fresh ``register --apply`` rotates the blueprint secret into the generated
   config and keychain per the skill's existing register contract.

2. **Manual Entra rotation (when register cannot run).**

   ```bash
   az login
   az ad app credential reset --id <blueprint-app-id> --append --years 2
   ```

   Or with the Agent 365 CLI wrapper:

   ```bash
   a365 az ad app credential reset --id <blueprint-app-id> --append --years 2
   ```

   Copy the **new secret value once**, store it where the gateway reads it
   (generated config / keychain per your deployment), and restart the activity
   bridge or Hermes gateway plugin so token minting picks up the new material.

## Bot Framework (Path B) client secret

Path B uses ``A365_BF_APP_ID`` and ``A365_BF_CLIENT_SECRET`` (or the OS secret
store when the env var is unset). The sidecar
(``a365.bot-service.config.json``) records the ``msaAppId`` but not the secret.

1. Create a new client secret in Entra for the Bot Framework application:

   ```bash
   az login
   az ad app credential reset --id <bf-app-id> --append --years 2
   ```

2. Update the operator profile:

   - Set ``A365_BF_CLIENT_SECRET`` in ``~/.hermes/.env`` (or the agent
     ``.env``), **or** store via the platform secret provider the gateway
     uses for Path B.
   - Do not commit secret values to git.

3. Restart the gateway / activity bridge so outbound Bot Framework connector
   traffic uses the new secret.

4. After the new secret works, remove older ``passwordCredentials`` entries in
   Entra for the same app (Entra portal → App registration → Certificates &
   secrets).

## Verification

After rotation:

```bash
hermes-a365 doctor --human
hermes-a365 status --human --agent-name <slug>
```

Both include ``end_date_time`` and ``days_remaining`` for each checked app in
``--json`` output. Live tenant walks (#187 / #90) should show real expiry
dates when Graph ``Application.Read.All`` (or equivalent) is available.

## Related issues

- #191 — expiry warnings in doctor/status
- #85 — v1 surface freeze (``--secret-expiry-warn-days`` flag)
- #87 — documentation sweep
