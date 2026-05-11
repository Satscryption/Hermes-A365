"""Tests for hermes_a365.a365_config — read/write/merge of a365.config.json."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_a365.a365_config import (
    CONFIG_FILENAME,
    A365Config,
    merge,
    read,
    write_atomic,
)

# ---------------------------------------------------------------------------
# A365Config.from_json_text / to_json_text
# ---------------------------------------------------------------------------


class TestA365ConfigSerialisation:
    def test_round_trip_preserves_known_fields(self) -> None:
        cfg = A365Config(
            tenantId="t",
            clientAppId="c",
            agentBlueprintDisplayName="My Agent Blueprint",
        )
        text = cfg.to_json_text()
        rebuilt = A365Config.from_json_text(text)
        assert rebuilt.tenantId == "t"
        assert rebuilt.clientAppId == "c"
        assert rebuilt.agentBlueprintDisplayName == "My Agent Blueprint"

    def test_unknown_fields_land_in_extra(self) -> None:
        cfg = A365Config.from_json_text('{"tenantId": "t", "futureField": 42}')
        assert cfg.tenantId == "t"
        assert cfg.extra == {"futureField": 42}

    def test_unknown_fields_round_trip(self) -> None:
        cfg = A365Config.from_json_text(
            '{"tenantId": "t", "futureField": 42, "nestedThing": {"a": 1}}'
        )
        text = cfg.to_json_text()
        merged = json.loads(text)
        assert merged["tenantId"] == "t"
        assert merged["futureField"] == 42
        assert merged["nestedThing"] == {"a": 1}
        # And `extra` itself is NOT a separate top-level key in the output —
        # the wire format is flat per Microsoft's example.
        assert "extra" not in merged

    def test_empty_text_yields_empty_config(self) -> None:
        cfg = A365Config.from_json_text("")
        assert cfg.tenantId == ""
        assert cfg.extra == {}

    def test_defaults_match_microsoft_example(self) -> None:
        cfg = A365Config()
        # Spot-check defaults that come from a365.config.example.json:
        assert cfg.location == "westus"
        assert cfg.environment == "preprod"
        assert cfg.appServicePlanSku == "B1"
        assert cfg.agentUserUsageLocation == "US"


# ---------------------------------------------------------------------------
# read / write_atomic
# ---------------------------------------------------------------------------


class TestReadWrite:
    def test_read_returns_empty_when_absent(self, tmp_path: Path) -> None:
        cfg = read(tmp_path / "missing.json")
        assert cfg == A365Config()

    def test_write_atomic_creates_parents_and_no_tmp_residue(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / CONFIG_FILENAME
        cfg = A365Config(tenantId="abc", clientAppId="xyz")
        write_atomic(target, cfg)
        assert target.exists()
        assert json.loads(target.read_text())["tenantId"] == "abc"
        assert not (target.parent / (CONFIG_FILENAME + ".tmp")).exists()

    def test_write_then_read_round_trip(self, tmp_path: Path) -> None:
        cfg = A365Config(tenantId="t", clientAppId="c", agentBlueprintDisplayName="My Blueprint")
        write_atomic(tmp_path / CONFIG_FILENAME, cfg)
        rebuilt = read(tmp_path / CONFIG_FILENAME)
        assert rebuilt.tenantId == "t"
        assert rebuilt.clientAppId == "c"
        assert rebuilt.agentBlueprintDisplayName == "My Blueprint"


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


class TestMerge:
    def test_merges_known_fields(self) -> None:
        base = A365Config(tenantId="t", clientAppId="old")
        merged = merge(base, {"clientAppId": "new", "subscriptionId": "sub"})
        assert merged.tenantId == "t"
        assert merged.clientAppId == "new"
        assert merged.subscriptionId == "sub"

    def test_merges_unknown_fields_into_extra(self) -> None:
        base = A365Config(tenantId="t")
        merged = merge(base, {"experimentalFeature": True})
        assert merged.extra == {"experimentalFeature": True}

    def test_skips_none_and_empty_values(self) -> None:
        base = A365Config(tenantId="t", clientAppId="keep")
        merged = merge(base, {"clientAppId": None, "subscriptionId": ""})
        assert merged.clientAppId == "keep"  # None ignored
        assert merged.subscriptionId == ""  # base default preserved

    def test_extras_accumulate(self) -> None:
        base = A365Config(tenantId="t", extra={"a": 1})
        merged = merge(base, {"b": 2})
        assert merged.extra == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


def test_config_filename_pinned() -> None:
    # The CLI looks for this exact filename next to its working dir.
    assert CONFIG_FILENAME == "a365.config.json"


# Make sure we can construct a config purely from kwargs (no extra inferred).
def test_construct_with_only_required() -> None:
    cfg = A365Config(tenantId="t", clientAppId="c")
    assert cfg.tenantId == "t"
    assert cfg.clientAppId == "c"
    assert cfg.extra == {}
