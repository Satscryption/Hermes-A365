"""Harness tests for scripts/soak_driver.py (T7)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import test_agent365_plugin as tap

ROOT = Path(__file__).resolve().parents[1]


def _load_soak_driver() -> Any:
    path = ROOT / "scripts" / "soak_driver.py"
    spec = importlib.util.spec_from_file_location("soak_driver", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["soak_driver"] = mod
    spec.loader.exec_module(mod)
    return mod


soak = _load_soak_driver()


def _targets(tmp_path: Path) -> dict[str, Any]:
    return {
        "pinned_alias": "dormant_pinned",
        "unpinned_alias": "dormant_unpinned",
        "dormant_pinned": {
            "chat_id": "19:soak-pinned@thread.v2",
            "service_url": "https://smba.trafficmanager.net/amer/x/",
        },
        "dormant_unpinned": {
            "chat_id": "19:soak-stale@thread.v2",
            "service_url": "https://smba.trafficmanager.net/amer/x/",
        },
    }


def _platform_extra(conv_path: Path, *, include_threshold: bool = True) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "slug": "soak-probe",
        "port": 0,
        "conversations_path": str(conv_path),
    }
    if include_threshold:
        extra["conversations_prune_max_age_days"] = 1.0 / 24.0
    return extra


class TestSoakMarker:
    def test_unique_utc_marker(self) -> None:
        fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        line = soak.format_marker(now=fixed)
        assert line == "soak84 proactive 2026-01-02T03:04:05Z"
        assert soak.format_marker(now=fixed) != soak.format_marker(
            now=datetime(2026, 1, 2, 3, 4, 6, tzinfo=UTC)
        )

    def test_default_main_prints_marker(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert soak.main([]) == 0
        out = capsys.readouterr().out.strip()
        assert out.startswith("soak84 proactive ")
        assert "T" in out and out.endswith("Z")


class TestSoakPruneProbeConfig:
    def test_threshold_must_be_present_before_construct(self, tmp_path: Path) -> None:
        cfg = tmp_path / "platform.json"
        cfg.write_text(
            json.dumps({"extra": _platform_extra(tmp_path / "c.json", include_threshold=False)}),
            encoding="utf-8",
        )
        with pytest.raises(SystemExit):
            soak.platform_extra_from_config(cfg)


class TestPrepareStoppedRegistry:
    def test_pin_unpin_save_round_trip(self, tmp_path: Path) -> None:
        reg_path = tmp_path / "conversations.json"
        now = 1_700_000_000.0
        aliases = soak.prepare_stopped_registry(reg_path, _targets(tmp_path), now=now)
        from hermes_a365.plugin.conversations import ConversationRegistry

        reg = ConversationRegistry.load(reg_path)
        pinned = _targets(tmp_path)[aliases["pinned_alias"]]["chat_id"]
        unpinned = _targets(tmp_path)[aliases["unpinned_alias"]]["chat_id"]
        assert pinned in reg
        assert unpinned in reg
        assert reg._by_id[pinned].pinned is True
        assert reg._by_id[unpinned].pinned is False
        assert reg._by_id[pinned].last_used_at == now - 7200.0


class TestExecutePruneProbe:
    @pytest.mark.asyncio
    async def test_probe_prune_persist_send_and_zero_post_removed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        conv_path = tmp_path / "conversations.json"
        targets = _targets(tmp_path)
        soak.prepare_stopped_registry(conv_path, targets, now=1_700_000_000.0)
        extra = _platform_extra(conv_path)
        a = tap._make_adapter(monkeypatch, **extra)
        a._mark_connected()
        a._http_client = MagicMock()
        a._bridge_cfg = MagicMock()
        a._fmi_cache = MagicMock()
        a._user_cache = MagicMock()
        a._bf_token_cache = MagicMock()
        bridge = tap.adapter_mod._import_bridge()
        monkeypatch.setattr(bridge, "acquire_reply_token", AsyncMock(return_value=("tok", "B")))
        posts = {"count": 0}

        async def _post(*_a: Any, **_k: Any) -> Any:
            posts["count"] += 1
            resp = MagicMock()
            resp.status_code = 201
            resp.json = MagicMock(return_value={"id": "new-id"})
            return resp

        a._http_client.post = _post
        monkeypatch.setattr(a, "_cached_inbound_for", lambda _cid: None)
        pinned = targets["dormant_pinned"]["chat_id"]
        unpinned = targets["dormant_unpinned"]["chat_id"]
        record = await soak.execute_prune_probe(
            a,
            registry_path=conv_path,
            pinned_chat=pinned,
            unpinned_chat=unpinned,
            pinned_alias="dormant_pinned",
            unpinned_alias="dormant_unpinned",
            post_counter=lambda: posts["count"],
        )
        assert record["pruned_count"] == 1
        assert record["pinned_still_present"] is True
        assert record["unpinned_still_present"] is False
        assert record["survivor_send_ok"] is True
        assert record["removed_send_ok"] is False
        assert record["removed_target_post_count"] == 0
        exported = json.dumps(record)
        assert "dormant_pinned" in exported
        assert "19:soak" not in exported

    @pytest.mark.asyncio
    async def test_disconnect_runs_on_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        conv_path = tmp_path / "conversations.json"
        targets = _targets(tmp_path)
        soak.prepare_stopped_registry(conv_path, targets, now=1_700_000_000.0)
        extra = _platform_extra(conv_path)
        a = tap._make_adapter(monkeypatch, **extra)
        a._mark_connected()
        seen: dict[str, bool] = {"disconnect": False}
        real_disconnect = a.disconnect

        async def _disconnect() -> None:
            seen["disconnect"] = True
            await real_disconnect()

        monkeypatch.setattr(a, "disconnect", _disconnect)
        monkeypatch.setattr(a, "prune_conversations", AsyncMock(side_effect=RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            await soak.execute_prune_probe(
                a,
                registry_path=conv_path,
                pinned_chat=targets["dormant_pinned"]["chat_id"],
                unpinned_chat=targets["dormant_unpinned"]["chat_id"],
                pinned_alias="dormant_pinned",
                unpinned_alias="dormant_unpinned",
                post_counter=lambda: 0,
            )
        assert seen["disconnect"] is True

    @pytest.mark.asyncio
    async def test_threshold_captured_at_init_not_mutated_extra(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        conv_path = tmp_path / "conversations.json"
        extra = _platform_extra(conv_path)
        a = tap._make_adapter(monkeypatch, **extra)
        assert a._conversations_prune_max_age_days == pytest.approx(1.0 / 24.0)
        a.config.extra["conversations_prune_max_age_days"] = 30.0
        assert a._conversations_prune_max_age_days == pytest.approx(1.0 / 24.0)
