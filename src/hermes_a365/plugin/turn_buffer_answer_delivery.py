"""Per-turn answer delivery records (#182 state machine)."""

from __future__ import annotations

from typing import Any

_DELIVERY_DEFAULTS: dict[str, Any] = {
    "bound_stream_id": "",
    "state": "PENDING",
    "may_have_landed": False,
    "had_landed": False,
    "answer_final_attempts": 0,
    "not_sent_probes_left": 1,
    "may_have_landed_probes_left": 1,
    "deferral_count": 0,
    "force_plain": False,
    "plain_due": False,
    "plain_attempts": 0,
    "force_plain_exhausted": False,
}


def turn_delivery_key(turn: dict[str, Any]) -> str:
    seq = str(turn.get("seq") or "")
    inbound = turn.get("inbound")
    inbound = inbound if isinstance(inbound, dict) else {}
    activity_id = str(
        turn.get("activity_id") or inbound.get("id") or ""
    )
    return f"{seq}:{activity_id}"


def delivery_map(buf: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = buf.get("delivery_by_turn")
    if not isinstance(raw, dict):
        raw = {}
        buf["delivery_by_turn"] = raw
    return raw


def get_delivery_record(
    buf: dict[str, Any], turn: dict[str, Any]
) -> dict[str, Any]:
    key = turn_delivery_key(turn)
    records = delivery_map(buf)
    record = records.get(key)
    if not isinstance(record, dict):
        inbound = turn.get("inbound")
        inbound = inbound if isinstance(inbound, dict) else {}
        activity_id = str(
            turn.get("activity_id") or inbound.get("id") or ""
        )
        record = {
            "turn_key": key,
            "seq": str(turn.get("seq") or ""),
            "activity_id": activity_id,
            **dict(_DELIVERY_DEFAULTS),
        }
        records[key] = record
    return record


def refresh_segment_ids(record: dict[str, Any], buf: dict[str, Any]) -> None:
    seq = str(record.get("seq") or "")
    record["segment_ids"] = [
        str(segment.get("id") or "")
        for segment in buf.get("segments") or []
        if str(segment.get("turn") or "") == seq
        and str(segment.get("id") or "")
    ]


def purge_done_records(buf: dict[str, Any]) -> None:
    records = delivery_map(buf)
    for key in list(records.keys()):
        record = records[key]
        if not isinstance(record, dict):
            del records[key]
            continue
        if record.get("state") != "DONE":
            continue
        seq = str(record.get("seq") or "")
        has_segments = any(
            str(segment.get("turn") or "") == seq
            for segment in buf.get("segments") or []
        )
        if not has_segments:
            del records[key]


def closed_by_turn_key(closed_by: str) -> str | None:
    prefix = "turn:"
    if not closed_by.startswith(prefix):
        return None
    key = closed_by[len(prefix) :]
    return key if key and key != "other" else None


def set_stream_closed(
    stream: dict[str, Any], closed_by: str, *, clear_id: bool = True
) -> None:
    stream["closed_by"] = closed_by
    if clear_id:
        stream["stream_id"] = ""


def landing_bits(record: dict[str, Any]) -> bool:
    return bool(record.get("may_have_landed") or record.get("had_landed"))


def set_may_have_landed(record: dict[str, Any]) -> None:
    record["may_have_landed"] = True
    record["had_landed"] = True


def set_had_landed(record: dict[str, Any]) -> None:
    record["had_landed"] = True


def user_cancel_phrase(detail: str) -> bool:
    low = detail.casefold()
    return "canceled by user" in low or "cancelled by user" in low
