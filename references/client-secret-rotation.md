# Client secret rotation (blueprint and Bot Framework)

Snapshot date: 2026-10-29

Hermes-A365 reads **only** ``passwordCredentials`` ``startDateTime`` / ``endDateTime``
from Microsoft Graph during ``hermes-a365 doctor`` and ``hermes-a365 status``. Secret
values, key ids, and hints are never fetched or printed. The commands monitor the
**last still-valid** password credential (``start <= now < end`` with the latest
``endDateTime``). **Warn** when that date is within **30 days** by default
(``--secret-expiry-warn-days N``); **fail** only when every password credential has
expired. Apps with no password credentials (certificate or federated only) report
*no client secret to monitor* — not a failure.

## Blueprint (agent identity) client secret

### Mint or rotate in Entra

```bash
az login
az ad app credential reset \
  --id <blueprint-app-id> \
  --append \
  --display-name "Hermes blueprint $(date +%Y-%m-%d)" \
  --years 2 \
  -o json
```

Without ``--append``, Entra **replaces** existing password credentials — plan a
overlap window before removing old secrets.

The command prints the new password **once** on stdout. Capture it through the
skill's hidden prompt path:

1. Write ``A365_BLUEPRINT_CLIENT_SECRET`` to ``~/.hermes/.env`` (this wins over
   keychain on load).
2. Update ``agentBlueprintClientSecret`` in ``a365.generated.config.json`` when
   that file is your deployment anchor.

Restart the activity bridge or Hermes gateway plugin after the secret is stored.

``register --apply --auto-recover-secret`` is only for the case where
``agentBlueprintClientSecret`` is **null on disk** and the operator deliberately
opts in to minting a new secret — it is not a general rotation path.

### Path B Bot Framework client secret

```bash
az ad app credential reset \
  --id <bf-app-id> \
  --append \
  --display-name "Hermes BF $(date +%Y-%m-%d)" \
  --years 2 \
  -o json
```

Update ``A365_BF_CLIENT_SECRET`` in ``~/.hermes/.env`` (or the agent ``.env``),
or the OS secret store your gateway uses for Path B. Restart the gateway. Remove
superseded Entra password entries after traffic succeeds.

## Verification

```bash
hermes-a365 doctor --human --agent-name <slug>
hermes-a365 status --human --agent-name <slug>
```

JSON output includes ``end_date_time`` and ``days_remaining`` for monitored apps.

## Related issues

- #191 — expiry warnings in doctor/status
- #85 — v1 surface freeze (``--secret-expiry-warn-days`` flag)
- #87 — documentation sweep
