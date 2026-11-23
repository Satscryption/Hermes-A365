#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# fmt: off

def format_marker(*, now: datetime | None = None) -> str:
    when = now or datetime.now(UTC)
    return f"soak84 proactive {when.strftime('%Y-%m-%dT%H:%M:%SZ')}"

def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"{path}: expected a JSON object")
    return payload

def platform_extra_from_config(path: Path) -> dict[str, Any]:
    extra = _load_json(path).get("extra")
    if not isinstance(extra, dict):
        raise SystemExit(f"{path}: missing platform extra object")
    raw = extra.get("conversations_prune_max_age_days")
    if raw is None:
        raise SystemExit(f"{path}: conversations_prune_max_age_days required before adapter init")
    try:
        float(raw)
    except (TypeError, ValueError) as err:
        raise SystemExit(f"{path}: conversations_prune_max_age_days is not numeric") from err
    return dict(extra)

def prepare_stopped_registry(
    registry_path: Path, targets: dict[str, Any], *, now: float
) -> dict[str, str]:
    from hermes_a365.plugin.conversations import ConversationRef, ConversationRegistry
    pin_key, stale_key = str(targets["pinned_alias"]), str(targets["unpinned_alias"])
    reg, stale_at = ConversationRegistry.load(registry_path), float(now) - 7200.0
    for alias, pin in ((pin_key, True), (stale_key, False)):
        spec = targets[alias]
        cid, url = str(spec["chat_id"]), str(spec["service_url"])
        ref = ConversationRef.from_activity(
            {
                "type": "message", "id": "soak-seed", "channelId": "msteams", "serviceUrl": url,
                "conversation": {"id": cid, "conversationType": "personal"},
                "from": {"id": "soak-user", "name": "Soak User"},
                "recipient": {"id": "soak-bot", "name": "Soak Bot"}, "text": "soak dormant seed",
            }
        )
        if ref is None:
            raise SystemExit("target activity did not produce a conversation ref")
        reg.upsert(ref)
        reg._by_id[cid].last_used_at = stale_at
        (reg.pin if pin else reg.unpin)(cid)
    reg.save(registry_path)
    return {"pinned_alias": pin_key, "unpinned_alias": stale_key}

async def execute_prune_probe(
    adapter: Any,
    *,
    registry_path: Path,
    pinned_chat: str,
    unpinned_chat: str,
    pinned_alias: str,
    unpinned_alias: str,
    post_counter: Callable[[], int],
) -> dict[str, Any]:
    from hermes_a365.plugin.conversations import ConversationRegistry
    try:
        dropped = await adapter.prune_conversations()
        on_disk = ConversationRegistry.load(registry_path)
        survivor = await adapter.send(pinned_chat, "soak84 probe survivor")
        before_removed = post_counter()
        removed = await adapter.send(unpinned_chat, "soak84 probe removed")
        return {
            "pruned_count": dropped,
            "pinned_alias": pinned_alias,
            "unpinned_alias": unpinned_alias,
            "pinned_still_present": pinned_chat in on_disk,
            "unpinned_still_present": unpinned_chat in on_disk,
            "survivor_send_ok": bool(survivor.success),
            "removed_send_ok": bool(removed.success),
            "removed_target_post_count": post_counter() - before_removed,
        }
    finally:
        await adapter.disconnect()

async def run_prune_probe(platform_config: Path, targets: Path) -> dict[str, Any]:
    from gateway.config import PlatformConfig

    from hermes_a365.plugin.adapter import Agent365Adapter
    target_map, extra = _load_json(targets), platform_extra_from_config(platform_config)
    pin_alias, stale_alias = str(target_map["pinned_alias"]), str(target_map["unpinned_alias"])
    pin_chat = str(target_map[pin_alias]["chat_id"])
    stale_chat = str(target_map[stale_alias]["chat_id"])
    adapter, posts = Agent365Adapter(PlatformConfig(enabled=True, extra=extra)), {"count": 0}
    if not await adapter.connect():
        raise SystemExit("adapter connect failed")
    client = adapter._http_client
    if client is not None:
        real_post = client.post
        async def _counting_post(*args: Any, **kwargs: Any) -> Any:
            posts["count"] += 1
            return await real_post(*args, **kwargs)
        client.post = _counting_post  # type: ignore[method-assign]
    return await execute_prune_probe(
        adapter,
        registry_path=Path(str(extra["conversations_path"])),
        pinned_chat=pin_chat,
        unpinned_chat=stale_chat,
        pinned_alias=pin_alias,
        unpinned_alias=stale_alias,
        post_counter=lambda: posts["count"],
    )

def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        sys.stdout.write(format_marker() + "\n")
        return 0
    parser = argparse.ArgumentParser(description="Path B soak harness (#84)")
    subs = parser.add_subparsers(dest="command", required=True)
    prep = subs.add_parser("prepare-registry")
    prep.add_argument("--registry-path", type=Path, required=True)
    prep.add_argument("--targets", type=Path, required=True)
    prep.add_argument("--now", type=float, default=None)
    probe = subs.add_parser("prune-probe")
    probe.add_argument("--platform-config", type=Path, required=True)
    probe.add_argument("--targets", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare-registry":
        prepare_stopped_registry(
            args.registry_path,
            _load_json(args.targets),
            now=float(args.now if args.now is not None else datetime.now(UTC).timestamp()),
        )
        return 0
    record = asyncio.run(run_prune_probe(args.platform_config, args.targets))
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
