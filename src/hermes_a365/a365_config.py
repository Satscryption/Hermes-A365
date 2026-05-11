"""Read / write ``a365.config.json`` — the file the real CLI consumes.

The Microsoft.Agents.A365.DevTools.Cli reads agent metadata from
``a365.config.json`` next to the operator's working directory (or
falls back to ``--agent-name`` for config-less invocations). We mirror
the schema from
<https://github.com/microsoft/Agent365-devTools/blob/main/src/a365.config.example.json>
verified 2026-05-04.

Only fields the CLI actually reads are modelled. Unknown fields are
preserved on round-trip so a hand-edited file isn't clobbered.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "a365.config.json"


@dataclass
class A365Config:
    """Mirrors ``a365.config.example.json`` from Agent365-devTools.

    All fields default to empty strings so a partial config survives;
    callers fill in what they have and let the CLI fail-cleanly on the
    rest. The CLI itself validates required fields per command.
    """

    tenantId: str = ""
    clientAppId: str = ""
    subscriptionId: str = ""
    resourceGroup: str = ""
    location: str = "westus"
    environment: str = "preprod"  # CLI accepts: preprod / prod
    appServicePlanName: str = ""
    appServicePlanSku: str = "B1"
    webAppName: str = ""
    agentIdentityDisplayName: str = ""
    agentBlueprintDisplayName: str = ""
    agentUserPrincipalName: str = ""
    agentUserDisplayName: str = ""
    managerEmail: str = ""
    agentUserUsageLocation: str = "US"
    deploymentProjectPath: str = ""
    agentDescription: str = ""
    # Anything we didn't model goes here, preserved on round-trip.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json_text(cls, text: str) -> A365Config:
        """Parse a JSON text into an :class:`A365Config`. Unknown keys land in ``extra``."""
        payload: dict[str, Any] = json.loads(text) if text.strip() else {}
        known = {f.name for f in fields(cls) if f.name != "extra"}
        recognised = {k: v for k, v in payload.items() if k in known}
        extras = {k: v for k, v in payload.items() if k not in known}
        return cls(**recognised, extra=extras)

    def to_json_text(self) -> str:
        """Serialise as canonical JSON — extra fields merged at top level."""
        d = asdict(self)
        extras = d.pop("extra", {}) or {}
        merged: dict[str, Any] = {**d, **extras}
        return json.dumps(merged, indent=2, sort_keys=True) + "\n"


def write_atomic(path: Path, config: A365Config) -> None:
    """Atomically write ``config`` to ``path`` (tmp + rename). Creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(config.to_json_text())
    os.replace(tmp, path)


def read(path: Path) -> A365Config:
    """Read ``a365.config.json`` from disk. Returns an empty config if absent."""
    if not path.exists():
        return A365Config()
    return A365Config.from_json_text(path.read_text())


def merge(base: A365Config, updates: dict[str, Any]) -> A365Config:
    """Return a copy of ``base`` with ``updates`` applied.

    ``updates`` keys that match modelled fields update those fields;
    everything else lands in ``extra``. None / empty values are skipped
    so a partial update doesn't clobber existing populated fields.
    """
    known = {f.name for f in fields(A365Config) if f.name != "extra"}
    merged_main = asdict(base)
    merged_main.pop("extra", None)
    merged_extra = dict(base.extra)
    for k, v in updates.items():
        if v is None or (isinstance(v, str) and not v):
            continue
        if k in known:
            merged_main[k] = v
        else:
            merged_extra[k] = v
    return A365Config(**merged_main, extra=merged_extra)
