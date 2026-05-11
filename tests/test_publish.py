"""Tests for hermes_a365.publish — wraps `a365 publish`."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from hermes_a365.mutator import AADSTSError, CliInvocationError, RunResult
from hermes_a365.publish import (
    ADMIN_CENTRE_URL,
    PublishInputs,
    PublishPlan,
    PublishResult,
    _extract_package_path,
    apply_publish_plan,
    build_publish_plan,
)

# ---------------------------------------------------------------------------
# FakeMutator
# ---------------------------------------------------------------------------


@dataclass
class FakeMutator:
    available: bool = True
    calls: list[list[str]] = field(default_factory=list)
    scripted: list[RunResult | Exception] = field(default_factory=list)

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 60.0,
        stdin_input: str | None = None,
    ) -> RunResult:
        self.calls.append(list(argv))
        if self.scripted:
            nxt = self.scripted.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return RunResult(argv=list(argv), returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# PublishInputs validation
# ---------------------------------------------------------------------------


class TestPublishInputs:
    def test_minimal_valid(self) -> None:
        inp = PublishInputs(agent_name="x")
        assert inp.aiteammate is False
        assert inp.use_blueprint is False

    def test_empty_agent_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="agent_name"):
            PublishInputs(agent_name="")

    def test_use_blueprint_with_aiteammate_rejected(self) -> None:
        # Per CLI help: "--use-blueprint only meaningful with --aiteammate false".
        with pytest.raises(ValueError, match="--use-blueprint"):
            PublishInputs(agent_name="x", aiteammate=True, use_blueprint=True)


# ---------------------------------------------------------------------------
# build_publish_plan — argv shapes
# ---------------------------------------------------------------------------


class TestBuildPublishPlan:
    def test_argv_minimal(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="inbox-helper"))
        assert plan.step.argv == ["a365", "publish", "--agent-name", "inbox-helper"]

    def test_argv_with_tenant(self) -> None:
        plan = build_publish_plan(
            PublishInputs(agent_name="x", tenant_id="contoso.onmicrosoft.com")
        )
        assert plan.step.argv == [
            "a365",
            "publish",
            "--agent-name",
            "x",
            "--tenant-id",
            "contoso.onmicrosoft.com",
        ]

    def test_argv_with_aiteammate(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", aiteammate=True))
        assert "--aiteammate" in plan.step.argv

    def test_argv_with_use_blueprint(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", use_blueprint=True))
        assert "--use-blueprint" in plan.step.argv

    def test_argv_with_verbose(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", verbose=True))
        assert "--verbose" in plan.step.argv


# ---------------------------------------------------------------------------
# Plan rendering
# ---------------------------------------------------------------------------


class TestPlanRender:
    def test_human_blueprint_only_default(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="inbox-helper"))
        text = plan.render_human()
        assert "[plan] hermes a365 publish inbox-helper" in text
        assert "blueprint-only" in text
        assert "auto-detect" in text
        assert "$ a365 publish --agent-name inbox-helper" in text

    def test_human_aiteammate_flavour(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", aiteammate=True))
        text = plan.render_human()
        assert "AI Teammate" in text
        # Slice 18t (bug #14): AI Teammate output line points at the zip.
        assert "manifest zip for M365 Admin Centre upload" in text

    def test_human_blueprint_only_output_line(self) -> None:
        # Slice 18t (bug #14): blueprint-only output line is honest about
        # the Graph-API flow — no zip, nothing to upload.
        plan = build_publish_plan(PublishInputs(agent_name="x"))
        text = plan.render_human()
        assert "Graph API instance registration (no zip)" in text

    def test_human_use_blueprint_flow(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", use_blueprint=True))
        assert "blueprint-based non-DW" in plan.render_human()


# ---------------------------------------------------------------------------
# _extract_package_path
# ---------------------------------------------------------------------------


class TestExtractPackagePath:
    @pytest.mark.parametrize(
        "line",
        [
            "Created package: /tmp/inbox-helper-manifest.zip",
            "Wrote zip: ./build/agent.zip",
            "package created: /var/folders/x/agent-pkg.zip",
            "Package: /tmp/foo.zip",
        ],
    )
    def test_recognises_common_phrasings(self, line: str) -> None:
        assert _extract_package_path(line) is not None

    def test_returns_none_when_no_zip_in_output(self) -> None:
        assert _extract_package_path("Random success message with no zip path") is None

    def test_picks_first_zip_when_multiple(self) -> None:
        out = "Created package: /tmp/first.zip\nlater unrelated /tmp/other.zip mention"
        assert _extract_package_path(out) == "/tmp/first.zip"


# ---------------------------------------------------------------------------
# apply_publish_plan
# ---------------------------------------------------------------------------


class TestApplyPublish:
    def test_calls_mutator_with_planned_argv(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="inbox-helper"))
        mutator = FakeMutator()
        apply_publish_plan(plan, mutator=mutator)
        assert mutator.calls == [["a365", "publish", "--agent-name", "inbox-helper"]]

    def test_surfaces_package_path_when_visible_aiteammate(self) -> None:
        # Slice 18t (bug #14): zip extraction only runs in AI Teammate flow.
        plan = build_publish_plan(PublishInputs(agent_name="x", aiteammate=True))
        mutator = FakeMutator(
            scripted=[
                RunResult(
                    argv=["a365"],
                    returncode=0,
                    stdout="…\nCreated package: /tmp/x.zip\n",
                    stderr="",
                )
            ]
        )
        result = apply_publish_plan(plan, mutator=mutator)
        assert isinstance(result, PublishResult)
        assert result.package_path == "/tmp/x.zip"
        assert result.instance_id is None
        assert any("/tmp/x.zip" in m for m in result.messages)

    def test_aiteammate_messages_include_admin_centre_url(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", aiteammate=True))
        result = apply_publish_plan(plan, mutator=FakeMutator())
        assert any(ADMIN_CENTRE_URL in m for m in result.messages)

    def test_blueprint_only_extracts_instance_id(self) -> None:
        # Slice 18t (bug #14): blueprint-only flow registers via Graph and
        # prints "Agent instance registered: <guid>".
        plan = build_publish_plan(PublishInputs(agent_name="x"))  # default = blueprint-only
        mutator = FakeMutator(
            scripted=[
                RunResult(
                    argv=["a365"],
                    returncode=0,
                    stdout="POST /beta/agentRegistry/agentInstances\n"
                    "Agent instance registered: 8549283b-0e24-438c-993c-3bd1753a6c2b\n",
                    stderr="",
                )
            ]
        )
        result = apply_publish_plan(plan, mutator=mutator)
        assert result.instance_id == "8549283b-0e24-438c-993c-3bd1753a6c2b"
        assert result.package_path is None
        # Blueprint-only must NOT prompt the operator to upload anything.
        assert not any(ADMIN_CENTRE_URL in m for m in result.messages)
        assert any("no upload needed" in m for m in result.messages)

    def test_no_package_path_when_cli_silent(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", aiteammate=True))
        result = apply_publish_plan(plan, mutator=FakeMutator())
        assert result.package_path is None

    def test_aadsts_error_propagates(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x"))
        mutator = FakeMutator(scripted=[AADSTSError("AADSTS65001", "no perms")])
        with pytest.raises(AADSTSError) as excinfo:
            apply_publish_plan(plan, mutator=mutator)
        assert excinfo.value.code == "AADSTS65001"

    def test_cli_invocation_error_propagates(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x"))
        mutator = FakeMutator(scripted=[CliInvocationError(["a365"], 7, "boom")])
        with pytest.raises(CliInvocationError):
            apply_publish_plan(plan, mutator=mutator)


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_publish_plan_dataclass_basic_blueprint_only() -> None:
    # Slice 18t (bug #14): default flavour describes the Graph-API flow.
    plan = build_publish_plan(PublishInputs(agent_name="x"))
    assert isinstance(plan, PublishPlan)
    assert "Graph" in plan.step.description
    assert "no zip" in plan.step.description


def test_publish_plan_dataclass_basic_aiteammate() -> None:
    plan = build_publish_plan(PublishInputs(agent_name="x", aiteammate=True))
    assert plan.step.description.startswith("package")


def test_admin_centre_url_pinned() -> None:
    # Pin the URL we surface to the operator after a successful publish.
    assert ADMIN_CENTRE_URL == "https://admin.microsoft.com/"


# ---------------------------------------------------------------------------
# Slice 19r-c: name.short auto-truncation
# ---------------------------------------------------------------------------


class TestTruncateNameShort:
    """Pure-function tests for the truncation strategy."""

    def test_short_value_returned_unchanged(self) -> None:
        from hermes_a365.publish import _truncate_name_short

        assert _truncate_name_short("Inbox Helper") == "Inbox Helper"

    def test_exact_30_chars_returned_unchanged(self) -> None:
        from hermes_a365.publish import _truncate_name_short

        v = "A" * 30
        assert _truncate_name_short(v) == v

    def test_blueprint_suffix_stripped_when_over_30(self) -> None:
        # The common case surfaced in round-8: agent name + " Blueprint"
        # pushes over 30 chars; dropping the suffix brings it back.
        from hermes_a365.publish import _truncate_name_short

        v = "Hermes Inbox Helper R8 Blueprint"  # 32 chars
        assert _truncate_name_short(v) == "Hermes Inbox Helper R8"

    def test_blueprint_suffix_not_stripped_if_result_too_short(self) -> None:
        # If stripping " Blueprint" leaves an empty / 0-len result,
        # fall back to word-boundary truncation.
        from hermes_a365.publish import _truncate_name_short

        v = " Blueprint" * 4  # very long, but stripping one occurrence is fine
        out = _truncate_name_short(v)
        assert 1 <= len(out) <= 30

    def test_word_boundary_truncation_when_no_blueprint_suffix(self) -> None:
        from hermes_a365.publish import _truncate_name_short

        # 39 chars, no " Blueprint" suffix → word-boundary truncation
        v = "Production Customer Support Assistant 1"
        out = _truncate_name_short(v)
        assert len(out) <= 30
        # Doesn't cut mid-word
        assert not out.endswith(" ")
        for word in out.split(" "):
            assert word in v.split(" ")

    def test_single_long_word_hard_truncated(self) -> None:
        # Pathological case: one 50-char word with no spaces. Falls back
        # to slice + rstrip.
        from hermes_a365.publish import _truncate_name_short

        v = "X" * 50
        out = _truncate_name_short(v)
        assert len(out) <= 30


class TestPatchManifestNameShort:
    """Integration tests for the zip-rewrite path."""

    def _make_zip(self, tmp_path, manifest: dict, extra_files: dict | None = None):
        import json
        import zipfile

        zp = tmp_path / "manifest.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for name, blob in (extra_files or {}).items():
                zf.writestr(name, blob)
        return zp

    def test_returns_none_when_name_short_already_ok(self, tmp_path):
        from hermes_a365.publish import _patch_manifest_name_short

        zp = self._make_zip(tmp_path, {"name": {"short": "Inbox Helper", "full": "Full"}})
        assert _patch_manifest_name_short(str(zp)) is None

    def test_patches_blueprint_suffix(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import _patch_manifest_name_short

        zp = self._make_zip(
            tmp_path,
            {"name": {"short": "Hermes Inbox Helper R8 Blueprint", "full": "X"}},
            extra_files={"icon.png": b"png-bytes"},
        )
        result = _patch_manifest_name_short(str(zp))
        assert result is not None
        old, new = result
        assert old == "Hermes Inbox Helper R8 Blueprint"
        assert new == "Hermes Inbox Helper R8"
        # Re-zip preserves other files
        with zipfile.ZipFile(zp) as zf:
            assert set(zf.namelist()) == {"manifest.json", "icon.png"}
            assert zf.read("icon.png") == b"png-bytes"
            m = json.loads(zf.read("manifest.json"))
            assert m["name"]["short"] == "Hermes Inbox Helper R8"
            assert m["name"]["full"] == "X"  # unchanged

    def test_returns_none_when_zip_missing(self, tmp_path):
        from hermes_a365.publish import _patch_manifest_name_short

        assert _patch_manifest_name_short(str(tmp_path / "nope.zip")) is None

    def test_returns_none_when_no_manifest_json(self, tmp_path):
        import zipfile

        from hermes_a365.publish import _patch_manifest_name_short

        zp = tmp_path / "manifest.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("other.json", b"{}")
        assert _patch_manifest_name_short(str(zp)) is None

    def test_returns_none_on_bad_json(self, tmp_path):
        import zipfile

        from hermes_a365.publish import _patch_manifest_name_short

        zp = tmp_path / "manifest.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("manifest.json", b"not-json")
        assert _patch_manifest_name_short(str(zp)) is None


class TestApplyPublishPlanIntegration:
    """Slice 19r-c: apply_publish_plan calls truncation when applicable."""

    def test_apply_emits_truncation_message_when_patched(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        # Build a real-shaped zip the FakeMutator will "produce".
        zp = tmp_path / "manifest.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {"name": {"short": "Hermes Inbox Helper R8 Blueprint", "full": "X"}},
                ),
            )
        fm = FakeMutator(
            scripted=[
                RunResult(argv=[], returncode=0, stdout=f"Package created: {zp}", stderr="")
            ]
        )
        plan = build_publish_plan(PublishInputs(agent_name="X", aiteammate=True))
        result = apply_publish_plan(plan, mutator=fm)
        assert result.package_path == str(zp)
        # The truncation message is in messages
        assert any("truncated name.short" in m for m in result.messages)
        # Zip on disk has the patched name
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["name"]["short"] == "Hermes Inbox Helper R8"

    def test_apply_skips_truncation_message_when_not_needed(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        zp = tmp_path / "manifest.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr(
                "manifest.json",
                json.dumps({"name": {"short": "Short Name", "full": "X"}}),
            )
        fm = FakeMutator(
            scripted=[
                RunResult(argv=[], returncode=0, stdout=f"Package created: {zp}", stderr="")
            ]
        )
        plan = build_publish_plan(PublishInputs(agent_name="X", aiteammate=True))
        result = apply_publish_plan(plan, mutator=fm)
        assert not any("truncated name.short" in m for m in result.messages)
