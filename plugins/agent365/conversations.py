"""Durable session table for the agent365 adapter.

Slice 19o replaces the in-memory ``_chat_contexts`` dict the
slice 19n adapter used with a JSON-backed registry, so:

- proactive sends (#4) can target a conversation the bridge hasn't
  seen *this run*;
- the agent's reply context survives a uvicorn restart;
- session metadata (chat name, user info, last-seen activity id)
  survives across runs for status/debug surfaces.

The on-disk format is one JSON file per slug at
``~/.hermes/agents/<slug>/conversations.json``. Reads tolerate the
file being absent, malformed, or carrying entries from a future
schema (extra keys are kept in ``raw`` so we don't lose data on a
round-trip). Writes are atomic via tmpfile + ``os.replace``.

The shape is loosely modelled on the upstream Hermes msteams
adapter (NousResearch/hermes-agent#10037, ``msteams_state.py``),
but pruned to fields we actually use today. Group/channel-specific
fields land in subsequent slices when those surfaces matter.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ConversationRef:
    """Per-conversation pointer the adapter needs to talk back."""

    conversation_id: str
    service_url: str
    chat_type: str = "personal"  # `personal` | `groupChat` | `channel`
    chat_name: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    tenant_id: str | None = None
    last_inbound_activity_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ConversationRef:
        # Round-trip-safe: tolerate extra keys (future schema) by
        # filtering against the dataclass field names.
        valid = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in payload.items() if k in valid}
        kwargs.setdefault("raw", payload.get("raw") or {})
        # Required fields
        kwargs.setdefault("conversation_id", payload.get("conversation_id", ""))
        kwargs.setdefault("service_url", payload.get("service_url", ""))
        return cls(**kwargs)

    @classmethod
    def from_activity(cls, activity: dict[str, Any]) -> ConversationRef | None:
        """Build a ``ConversationRef`` from an inbound BF activity.

        Returns ``None`` when the activity is missing the load-bearing
        ``conversation.id`` — the caller should treat that as
        un-routable rather than persist a bad entry.
        """
        conv = activity.get("conversation") or {}
        if not isinstance(conv, dict):
            return None
        conv_id = conv.get("id")
        if not conv_id:
            return None
        sender = activity.get("from") or {}
        if not isinstance(sender, dict):
            sender = {}
        conv_type_raw = str(conv.get("conversationType") or "personal")
        chat_type = (
            "personal"
            if conv_type_raw == "personal"
            else ("groupChat" if conv_type_raw == "groupChat" else "channel")
        )
        return cls(
            conversation_id=str(conv_id),
            service_url=str(activity.get("serviceUrl") or ""),
            chat_type=chat_type,
            chat_name=str(conv.get("name") or "") or None,
            user_id=str(sender.get("id") or "") or None,
            user_name=str(sender.get("name") or "") or None,
            tenant_id=str(conv.get("tenantId") or "") or None,
            last_inbound_activity_id=str(activity.get("id") or "") or None,
            raw=activity,
        )


class ConversationRegistry:
    """In-memory cache + JSON-on-disk persistence."""

    SCHEMA_VERSION = 1

    def __init__(self) -> None:
        self._by_id: dict[str, ConversationRef] = {}

    # ── Lookup / mutation ─────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, conversation_id: str) -> bool:
        return conversation_id in self._by_id

    def get(self, conversation_id: str) -> ConversationRef | None:
        return self._by_id.get(conversation_id)

    def upsert(self, ref: ConversationRef) -> ConversationRef:
        """Insert or merge a ConversationRef. Existing entries' fields
        are kept when the incoming value is empty/None — operators can
        rename a chat without losing the cached `chat_name` on a
        subsequent activity that doesn't echo it back."""
        existing = self._by_id.get(ref.conversation_id)
        if existing is None:
            self._by_id[ref.conversation_id] = ref
            return ref
        merged = ConversationRef(
            conversation_id=ref.conversation_id,
            service_url=ref.service_url or existing.service_url,
            chat_type=ref.chat_type or existing.chat_type,
            chat_name=ref.chat_name or existing.chat_name,
            user_id=ref.user_id or existing.user_id,
            user_name=ref.user_name or existing.user_name,
            tenant_id=ref.tenant_id or existing.tenant_id,
            last_inbound_activity_id=(
                ref.last_inbound_activity_id or existing.last_inbound_activity_id
            ),
            raw=ref.raw or existing.raw,
        )
        self._by_id[ref.conversation_id] = merged
        return merged

    def items(self) -> list[ConversationRef]:
        return list(self._by_id.values())

    # ── Persistence ───────────────────────────────────────────────────────

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA_VERSION,
            "conversations": [r.to_dict() for r in self._by_id.values()],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ConversationRegistry:
        reg = cls()
        for entry in payload.get("conversations") or []:
            if not isinstance(entry, dict):
                continue
            ref = ConversationRef.from_dict(entry)
            if ref.conversation_id:
                reg._by_id[ref.conversation_id] = ref
        return reg

    @classmethod
    def load(cls, path: Path) -> ConversationRegistry:
        """Read the registry from disk. Returns an empty registry if
        the file doesn't exist, isn't readable, or doesn't parse."""
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return cls()
        except OSError as e:
            logger.warning("agent365 conversations: read failed for %s: %s", path, e)
            return cls()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("agent365 conversations: json parse failed for %s: %s", path, e)
            return cls()
        if not isinstance(payload, dict):
            return cls()
        return cls.from_payload(payload)

    def save(self, path: Path) -> None:
        """Write atomically: tmpfile in same dir → fsync → os.replace.
        Same-dir is required so ``os.replace`` is atomic on POSIX."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile with delete=False so we can rename outside
        # the with-block.
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(self.to_payload(), tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        try:
            os.replace(tmp_path, path)
        except OSError:
            # Best-effort: drop the tmpfile if the rename failed.
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
