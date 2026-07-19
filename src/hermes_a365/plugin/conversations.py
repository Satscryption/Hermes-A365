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
from typing import Any, ClassVar

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
    # L4 (#100): the JWT-validated inbound path ("A"/"B") captured at inbound
    # time (which validator passed), so decoupled agent-loop outbound mints bind
    # to the validated path instead of re-deriving it from the untrusted activity
    # body. None for legacy payloads / lifecycle-captured refs (no user turn).
    validated_path: str | None = None
    # Slice 19x-c (#4): last_used_at is the Unix timestamp the registry
    # last touched this entry (set by ConversationRegistry.upsert on
    # capture, by mark_used on outbound). pinned=True makes prune_old_entries
    # always skip — for operator-pinned cron targets.
    last_used_at: float | None = None
    pinned: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ConversationRef:
        # Round-trip-safe: tolerate extra keys (future schema) by
        # filtering against the dataclass field names. New fields
        # (`last_used_at`, `pinned`) absent from older payloads fall
        # back to their dataclass defaults.
        valid = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in payload.items() if k in valid}
        # M10 (#105): a corrupted / hand-edited conversations.json can carry a
        # non-dict ``raw`` (e.g. ``"raw": "oops"``). It round-trips through
        # ``load()`` and then crashes the first send/edit/proactive path with
        # ``AttributeError`` on ``raw.get(...)``, breaking that conversation.
        # M11 (#105) review: M10 only guards a *non-dict* raw — a PRE-M11 file
        # (full untrimmed raw) or a corrupted one can carry a *dict* raw that is
        # oversized or nested arbitrarily deep, which would later RecursionError
        # ``to_payload()``/save (outside the save's ``OSError`` guard) and
        # permanently break persistence. Re-project a dict raw through the SAME
        # size-bounded allowlist the inbound path uses: this bounds it AND
        # preserves the identity/routing keys (so a pinned proactive target
        # keeps working across an upgrade), falling to ``{}`` only when the raw
        # is unroutable/corrupt. ``_project_cached_raw`` fails closed on
        # ``RecursionError`` internally, so a deep raw never reaches ``asdict``.
        raw_val = kwargs.get("raw")
        if isinstance(raw_val, dict):
            kwargs["raw"] = cls._project_cached_raw(raw_val) or {}
        else:
            kwargs["raw"] = {}
        # Required fields
        kwargs.setdefault("conversation_id", payload.get("conversation_id", ""))
        kwargs.setdefault("service_url", payload.get("service_url", ""))
        return cls(**kwargs)

    # M11 (#105): the cached ``raw`` is an ALLOWLIST projection of the inbound
    # activity — only the top-level keys the outbound/mint paths read (verified
    # against a full consumer trace). Everything else — attachments,
    # channelData, text, and any *unknown* vendor field — is dropped, so no
    # valid inbound can create an arbitrarily large durable entry (a denylist
    # would let unknown large keys through; bounded storage is M11's goal).
    _RAW_KEEP_TOP = frozenset(
        {"conversation", "serviceUrl", "recipient", "from", "id", "channelId", "type"}
    )
    # Backstop for nested bloat: if a KEPT dict (conversation/from/recipient)
    # still carries an oversized nested value, fall back to a minimal subkey
    # projection so per-entry size is bounded regardless of nesting.
    _RAW_MAX_BYTES = 16384
    # ClassVar → not a @dataclass field. Kept dict → subkeys to retain when the
    # allowlisted raw overflows _RAW_MAX_BYTES.
    _RAW_MIN_PROJECTION: ClassVar[dict[str, tuple[str, ...]]] = {
        "conversation": ("id", "tenantId", "conversationType", "name"),
        "recipient": ("id", "name", "agenticAppId", "agenticUserId", "tenantId"),
        "from": ("id", "name"),
    }

    @staticmethod
    def _fits(payload: dict[str, Any], ceiling: int) -> bool:
        """True iff ``payload`` serializes within ``ceiling`` bytes under the
        same serialization *flags* ``write_payload`` uses.

        Measured with ``indent=2, sort_keys=True`` (matching ``write_payload``)
        — NOT a compact dump — because indentation cost grows with nesting
        depth, so a deeply-nested kept sub-object can be tiny compact yet
        balloon the on-disk artifact. This measures ``payload`` **standalone**;
        on disk the projection is nested a few levels deeper (so ~+6 bytes/line)
        and its routing scalars are also duplicated into the top-level
        ``ConversationRef`` fields, so the whole on-disk entry runs ~2x this
        figure — a small constant multiple, not unbounded. A payload that can't
        be serialized or bounded — ``TypeError``/``ValueError`` (incl. a circular
        ref) or ``RecursionError`` (pathological nesting depth) — fails closed as
        over-ceiling, so the caller falls back to the flat minimal projection
        (or rejects)."""
        try:
            return (
                len(json.dumps(payload, indent=2, sort_keys=True, default=str))
                <= ceiling
            )
        except (TypeError, ValueError, RecursionError):
            return False

    @classmethod
    def _project_cached_raw(cls, activity: dict[str, Any]) -> dict[str, Any] | None:
        """Build the size-bounded cached ``raw`` (#105/M11), or ``None`` when
        even the minimal routing projection can't fit ``_RAW_MAX_BYTES``.

        Order: (1) allowlist the top-level keys the outbound paths read and
        return that if it fits; (2) if a kept dict still carries an oversized
        nested value, fall back to a minimal identity-only subkey projection;
        (3) if even that overflows — a retained identity field (a routing id/URL
        such as ``id``/``serviceUrl``/``conversation.id``, *or* a retained
        display name such as ``conversation.name``/``from.name``) is itself
        unreasonably large — return ``None`` so ``from_activity`` rejects the
        activity as unroutable. We never truncate (trimming an id/URL would
        silently retarget a reply or token; the minimal projection is
        all-or-nothing), so an oversized identity field is a hard reject.
        The return is a projection whose **standalone** serialized size is
        ``<= _RAW_MAX_BYTES`` (or ``None``); since every top-level
        ``ConversationRef`` field is drawn from these same projected keys, an
        accepted ref's whole on-disk entry is bounded to a small constant
        multiple of that ceiling (see ``_fits``) — bounded, never unbounded."""
        raw = {k: v for k, v in activity.items() if k in cls._RAW_KEEP_TOP}
        if cls._fits(raw, cls._RAW_MAX_BYTES):
            return raw
        out: dict[str, Any] = {}
        for key, subkeys in cls._RAW_MIN_PROJECTION.items():
            src = activity.get(key)
            if isinstance(src, dict):
                out[key] = {k: src[k] for k in subkeys if k in src}
        for scalar in ("serviceUrl", "id", "channelId", "type"):
            if scalar in activity:
                out[scalar] = activity[scalar]
        if cls._fits(out, cls._RAW_MAX_BYTES):
            return out
        return None  # a retained identity field too large → reject, never trim

    @classmethod
    def from_activity(cls, activity: dict[str, Any]) -> ConversationRef | None:
        """Build a ``ConversationRef`` from an inbound BF activity.

        Returns ``None`` when the activity is un-routable — the caller treats
        that as ack-and-skip rather than persisting a bad entry. Two cases:
        the load-bearing ``conversation.id`` is missing, or the minimal identity
        projection can't fit ``_RAW_MAX_BYTES`` (any retained identity field is
        unreasonably large — the routing ids/URL *or* a retained display name
        like ``conversation.name``/``from.name``; #105/M11 review). We reject
        rather than truncate, since trimming an id/URL would silently retarget
        replies/tokens.
        """
        conv = activity.get("conversation") or {}
        if not isinstance(conv, dict):
            return None
        conv_id = conv.get("id")
        if not conv_id:
            return None
        # M11 (#105): build the size-bounded PROJECTION first — a *new* dict,
        # never the passed-in activity (the capturing turn still reads it for
        # media/text extraction). ``None`` means the minimal identity projection
        # is itself over-ceiling (a routing id/URL or a retained display name is
        # oversized) → reject the whole activity, which also bounds the
        # top-level fields below (all drawn from these same projected keys).
        raw = cls._project_cached_raw(activity)
        if raw is None:
            sender = activity.get("from") if isinstance(activity.get("from"), dict) else {}
            recip = (
                activity.get("recipient")
                if isinstance(activity.get("recipient"), dict)
                else {}
            )
            logger.warning(
                "agent365 conversations: identity projection exceeds %d bytes — "
                "rejecting as unroutable rather than persisting an unbounded "
                "entry (byte-lengths conv.id=%d conv.name=%d serviceUrl=%d "
                "from.id=%d from.name=%d recipient.name=%d activity.id=%d)",
                cls._RAW_MAX_BYTES,
                len(str(conv_id)),
                len(str(conv.get("name") or "")),
                len(str(activity.get("serviceUrl") or "")),
                len(str(sender.get("id") or "")),
                len(str(sender.get("name") or "")),
                len(str(recip.get("name") or "")),
                len(str(activity.get("id") or "")),
            )
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
            raw=raw,
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

    def upsert(
        self, ref: ConversationRef, *, now: float | None = None
    ) -> ConversationRef:
        """Insert or merge a ConversationRef. Existing entries' fields
        are kept when the incoming value is empty/None — operators can
        rename a chat without losing the cached `chat_name` on a
        subsequent activity that doesn't echo it back.

        Slice 19x-c: stamps ``last_used_at`` on the stored entry —
        upsert is the canonical "this conversation just did something"
        signal (inbound captured, registry merge after token refresh,
        etc.). ``pinned`` is preserved from the existing entry; an
        incoming ref's ``pinned=True`` flips it on; ``pinned=False`` on
        the incoming ref does NOT unpin (callers wanting to unpin use
        ``unpin(conversation_id)`` explicitly).
        """
        import time as _time

        cur = now if now is not None else _time.time()
        existing = self._by_id.get(ref.conversation_id)
        if existing is None:
            ref.last_used_at = cur
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
            last_used_at=cur,
            pinned=bool(ref.pinned or existing.pinned),
            validated_path=ref.validated_path or existing.validated_path,
            raw=ref.raw or existing.raw,
        )
        self._by_id[ref.conversation_id] = merged
        return merged

    def evict(self, conversation_id: str) -> bool:
        """Remove a conversation entry outright (#79 uninstall hygiene).

        Returns ``True`` if an entry was removed, ``False`` if the id was
        not present. Unlike ``prune_old_entries`` (age-based, skips pinned
        and active entries), this is an explicit tenant-driven removal: an
        ``installationUpdate`` (remove) means the agent was uninstalled
        from the conversation, so proactive POSTs into it must stop
        immediately rather than wait out the 30-day prune. An uninstall is
        a harder signal than an operator ``pin``, so pinned entries are
        evicted too.
        """
        return self._by_id.pop(conversation_id, None) is not None

    def mark_used(
        self, conversation_id: str, *, now: float | None = None
    ) -> bool:
        """Slice 19x-c: bump ``last_used_at`` without touching other fields.

        Called from outbound paths (``send``, ``send_typing``, …) so
        pruning honours conversations that are write-active even when no
        inbound arrives. Returns True when the entry exists, False
        otherwise.
        """
        import time as _time

        ref = self._by_id.get(conversation_id)
        if ref is None:
            return False
        ref.last_used_at = now if now is not None else _time.time()
        return True

    def pin(self, conversation_id: str) -> bool:
        """Slice 19x-c: mark a conversation as operator-pinned.

        ``prune_old_entries`` always skips pinned entries regardless of
        age. Returns True when the entry exists and is now pinned, False
        when there's no such conversation. Caller is responsible for
        ``save()`` if persistence matters.
        """
        ref = self._by_id.get(conversation_id)
        if ref is None:
            return False
        ref.pinned = True
        return True

    def unpin(self, conversation_id: str) -> bool:
        """Slice 19x-c: clear the pinned flag. Same return contract as ``pin``."""
        ref = self._by_id.get(conversation_id)
        if ref is None:
            return False
        ref.pinned = False
        return True

    def prune_old_entries(
        self,
        max_age_days: float,
        *,
        active_session_keys: set[str] | None = None,
        now: float | None = None,
    ) -> int:
        """Slice 19x-c (#4): drop stale entries.

        Mirrors ``gateway/session.py:1031``'s ``SessionStore.prune_old_entries``
        shape. Three skip conditions:

        1. ``conversation_id in active_session_keys`` — a Hermes session
           is currently in flight for this chat; don't drop the registry
           entry underneath it.
        2. ``ref.pinned is True`` — operator-pinned cron target.
        3. ``ref.last_used_at is None`` — never marked used (e.g. loaded
           from disk in a schema migration); treated as recent rather
           than prune-now to avoid catastrophic data loss on the first
           prune after a registry-schema migration. Operators can
           explicitly drop these via a separate path if needed.

        Otherwise an entry is dropped when ``(now - last_used_at) >
        max_age_days * 86400``.

        Returns the number of entries removed. Does NOT save to disk —
        caller decides when to persist.
        """
        import time as _time

        cur = now if now is not None else _time.time()
        cutoff = cur - float(max_age_days) * 86400.0
        active = active_session_keys or set()
        to_drop: list[str] = []
        for conv_id, ref in self._by_id.items():
            if conv_id in active:
                continue
            if ref.pinned:
                continue
            if ref.last_used_at is None:
                continue
            if ref.last_used_at >= cutoff:
                continue
            to_drop.append(conv_id)
        for cid in to_drop:
            del self._by_id[cid]
        return len(to_drop)

    def enforce_cap(
        self,
        max_entries: int | None,
        *,
        active_conversation_ids: set[str] | None = None,
    ) -> int:
        """#105/M11: bound the registry size so a long-running gateway can't
        grow it (and its on-disk save) without limit.

        When over ``max_entries``, drop the **least-recently-used** entries
        (by ``last_used_at``; a ``None`` stamp — e.g. loaded-from-disk,
        never touched this run — sorts oldest so those go first) until within
        cap or nothing droppable remains. Never drops a **pinned** entry or
        one whose ``conversation_id`` is in ``active_conversation_ids`` (a
        Hermes turn is in flight for it — the caller supplies these in the
        registry's own conversation-id space, NOT session-key space).
        ``max_entries`` ``None`` disables the cap. Returns the number
        removed; does not save.
        """
        if max_entries is None or len(self._by_id) <= max_entries:
            return 0
        active = active_conversation_ids or set()
        candidates = sorted(
            (
                r
                for r in self._by_id.values()
                if not r.pinned and r.conversation_id not in active
            ),
            key=lambda r: (r.last_used_at if r.last_used_at is not None else 0.0),
        )
        overflow = len(self._by_id) - max_entries
        to_drop = [r.conversation_id for r in candidates[:overflow]]
        for cid in to_drop:
            del self._by_id[cid]
        return len(to_drop)

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
        except (json.JSONDecodeError, RecursionError) as e:
            # RecursionError: a corrupted / hand-edited file nested past the
            # interpreter limit (#105/M11 review). Treat as unparseable — an
            # empty registry beats crashing adapter construction.
            logger.warning("agent365 conversations: json parse failed for %s: %s", path, e)
            return cls()
        if not isinstance(payload, dict):
            return cls()
        return cls.from_payload(payload)

    def save(self, path: Path) -> None:
        """Serialize the current registry and write it atomically."""
        self.write_payload(path, self.to_payload())

    @staticmethod
    def write_payload(path: Path, payload: dict[str, Any]) -> None:
        """Write a pre-built ``payload`` atomically: tmpfile in same dir →
        fsync → os.replace (same-dir is required so ``os.replace`` is atomic
        on POSIX).

        Split out of :meth:`save` so an async caller can build the payload
        snapshot on the event loop (no concurrent mutation of ``_by_id``) and
        then run this blocking serialize+write in an executor (#105/M11)."""
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
            json.dump(payload, tmp, indent=2, sort_keys=True)
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
