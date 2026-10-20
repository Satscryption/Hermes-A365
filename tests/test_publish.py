"""Tests for hermes_a365.publish — wraps `a365 publish`."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hermes_a365.mutator import AADSTSError, CliInvocationError, RunResult
from hermes_a365.publish import (
    _PUBLISH_APPLY_TIMEOUT_SECONDS,
    ADMIN_CENTRE_URL,
    PublishError,
    PublishInputs,
    PublishPlan,
    PublishResult,
    _extract_package_path,
    build_publish_plan,
)
from hermes_a365.publish import (
    apply_publish_plan as _apply_publish_plan,
)

_TEST_TENANT = "aaaaaaaa-0000-0000-0000-000000000001"
_PUBLISH_STATE_PROFILE = "default"


def _write_publish_state(
    path: Path,
    *,
    manifest_id: str,
    app_version: str,
    hermes_home: Path,
    cea_catalog_id_minted: bool = False,
) -> None:
    from hermes_a365.publish import _write_copilot_chat_publish_state

    _write_copilot_chat_publish_state(
        path,
        manifest_id=manifest_id,
        app_version=app_version,
        hermes_home=hermes_home,
        tenant_id=_TEST_TENANT,
        profile=_PUBLISH_STATE_PROFILE,
        cea_catalog_id_minted=cea_catalog_id_minted,
    )


def _read_publish_state(
    path: Path, *, manifest_id: str, hermes_home: Path
) -> str | None:
    from hermes_a365.publish import _read_copilot_chat_publish_state

    return _read_copilot_chat_publish_state(
        path,
        manifest_id=manifest_id,
        hermes_home=hermes_home,
        tenant_id=_TEST_TENANT,
        profile=_PUBLISH_STATE_PROFILE,
    )

# ---------------------------------------------------------------------------
# FakeMutator
# ---------------------------------------------------------------------------


@dataclass
class FakeMutator:
    available: bool = True
    calls: list[list[str]] = field(default_factory=list)
    call_timeouts: list[float] = field(default_factory=list)
    scripted: list[RunResult | Exception] = field(default_factory=list)

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 60.0,
        stdin_input: str | None = None,
    ) -> RunResult:
        self.calls.append(list(argv))
        self.call_timeouts.append(timeout)
        if argv[:3] == ["az", "account", "show"]:
            if self.scripted:
                candidate = self.scripted[0]
                candidate_argv = getattr(candidate, "argv", [])
                if candidate_argv[:3] == ["az", "account", "show"]:
                    self.scripted.pop(0)
                    if isinstance(candidate, Exception):
                        raise candidate
                    return candidate
            return RunResult(
                argv=list(argv),
                returncode=0,
                stdout='{"tenantId":"aaaaaaaa-0000-0000-0000-000000000001"}',
                stderr="",
            )
        if self.scripted:
            nxt = self.scripted.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return RunResult(argv=list(argv), returncode=0, stdout="", stderr="")


def apply_publish_plan(
    plan: PublishPlan,
    *,
    mutator: FakeMutator,
    hermes_home: Path | None = None,
) -> PublishResult:
    if plan.inputs.tenant_id is None:
        plan.inputs.tenant_id = _TEST_TENANT
    return _apply_publish_plan(
        plan, mutator=mutator, hermes_home=hermes_home
    )


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

    def test_manifest_id_requires_copilot_chat(self) -> None:
        with pytest.raises(ValueError, match="--manifest-id"):
            PublishInputs(agent_name="x", manifest_id="auto")

    def test_manifest_id_guid_validated(self) -> None:
        with pytest.raises(ValueError, match="GUID"):
            PublishInputs(agent_name="x", copilot_chat=True, manifest_id="not-a-guid")

    def test_manifest_id_auto_allowed_with_copilot_chat(self) -> None:
        inp = PublishInputs(agent_name="x", copilot_chat=True, manifest_id="auto")
        assert inp.manifest_id == "auto"

    def test_prompt_starters_require_copilot_chat(self) -> None:
        # #74 — prompt starters only apply to the Copilot Chat (CEA) manifest.
        with pytest.raises(ValueError, match="only meaningful with --copilot-chat"):
            PublishInputs(
                agent_name="x", prompt_starters=({"title": "t", "prompt": "p"},)
            )

    def test_app_version_requires_copilot_chat(self) -> None:
        with pytest.raises(ValueError, match="only meaningful with --copilot-chat"):
            PublishInputs(agent_name="x", app_version="1.0.0")

    def test_app_version_format_validated(self) -> None:
        with pytest.raises(ValueError, match="app version"):
            PublishInputs(agent_name="x", copilot_chat=True, app_version="not-semver")


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
        assert "no canonical pin" in text
        assert "auto-detect" not in text
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

    def test_handles_unquoted_path_with_spaces(self) -> None:
        out = "Package created: /tmp/Hermes A365/manifest.zip"
        assert _extract_package_path(out) == "/tmp/Hermes A365/manifest.zip"

    def test_handles_quoted_path_with_spaces(self) -> None:
        out = 'Wrote zip: "/tmp/Hermes A365/manifest.zip"'
        assert _extract_package_path(out) == "/tmp/Hermes A365/manifest.zip"


# ---------------------------------------------------------------------------
# apply_publish_plan
# ---------------------------------------------------------------------------


class TestApplyPublish:
    def test_calls_mutator_with_planned_argv(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="inbox-helper"))
        mutator = FakeMutator()
        apply_publish_plan(plan, mutator=mutator)
        publish_calls = [argv for argv in mutator.calls if argv[0] == "a365"]
        assert publish_calls == [["a365", "publish", "--agent-name", "inbox-helper"]]

    def test_direct_apply_refuses_unpinned_plan(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="inbox-helper"))
        mutator = FakeMutator()
        with pytest.raises(PublishError, match="no canonical tenant pin"):
            _apply_publish_plan(plan, mutator=mutator)
        assert mutator.calls == []

    def test_missing_a365_cli_is_not_a_tenant_verification_failure(self) -> None:
        plan = build_publish_plan(
            PublishInputs(agent_name="x", tenant_id=_TEST_TENANT)
        )
        mutator = FakeMutator(available=False)
        with pytest.raises(PublishError, match="a365 CLI not found on PATH") as exc:
            _apply_publish_plan(plan, mutator=mutator)
        assert "could not verify the active Azure tenant" not in str(exc.value)
        assert mutator.calls == []

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
# apply_publish_plan timeout — regression for #52
# ---------------------------------------------------------------------------


class TestApplyPublishPlanTimeout:
    """Regression for #52: the 180 s override truncated `a365 publish`'s
    device-code auth fallback. Pin the call site to the named constant
    so future tightening is intentional, not accidental."""

    def test_uses_named_timeout_constant(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x"))
        mutator = FakeMutator()
        apply_publish_plan(plan, mutator=mutator)
        publish_timeouts = [
            timeout
            for argv, timeout in zip(mutator.calls, mutator.call_timeouts, strict=True)
            if argv[0] == "a365"
        ]
        assert publish_timeouts == [_PUBLISH_APPLY_TIMEOUT_SECONDS]

    def test_timeout_constant_is_generous_enough_for_device_code(self) -> None:
        # Microsoft device-code lifetime is 15 min = 900 s. Anything below
        # 600 s risks truncating valid auth mid-flow on fresh-shell walks.
        assert _PUBLISH_APPLY_TIMEOUT_SECONDS >= 600.0


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


class TestNameShortSuffix:
    def test_appends_cc_suffix_when_it_fits(self) -> None:
        from hermes_a365.publish import _with_name_short_suffix

        assert _with_name_short_suffix("Hermes Inbox Helper R8") == (
            "Hermes Inbox Helper R8 CC"
        )

    def test_truncates_base_and_preserves_suffix(self) -> None:
        from hermes_a365.publish import _with_name_short_suffix

        out = _with_name_short_suffix("Production Customer Support Assistant")
        assert len(out) <= 30
        assert out.endswith(" CC")

    def test_does_not_double_suffix(self) -> None:
        from hermes_a365.publish import _with_name_short_suffix

        assert _with_name_short_suffix("Hermes Inbox Helper CC") == (
            "Hermes Inbox Helper CC"
        )


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

        from hermes_a365.publish import build_publish_plan

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

        from hermes_a365.publish import build_publish_plan

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


# ---------------------------------------------------------------------------
# Slice 19u-a (#24): Custom Engine Agent manifest emit for Copilot Chat
# ---------------------------------------------------------------------------


class TestPublishInputsCopilotChat:
    def test_copilot_chat_default_false(self) -> None:
        assert PublishInputs(agent_name="x").copilot_chat is False
        assert PublishInputs(agent_name="x").bot_id is None

    def test_copilot_chat_and_use_blueprint_rejected(self) -> None:
        with pytest.raises(ValueError, match="--copilot-chat"):
            PublishInputs(agent_name="x", copilot_chat=True, use_blueprint=True)

    def test_copilot_chat_with_aiteammate_allowed(self) -> None:
        # Both surfaces requested in one publish — emit both zips.
        inp = PublishInputs(agent_name="x", aiteammate=True, copilot_chat=True)
        assert inp.aiteammate is True
        assert inp.copilot_chat is True

    def test_copilot_chat_alone_allowed(self) -> None:
        inp = PublishInputs(agent_name="x", copilot_chat=True)
        assert inp.copilot_chat is True
        assert inp.aiteammate is False

    def test_manifest_id_override_allowed(self) -> None:
        inp = PublishInputs(
            agent_name="x",
            copilot_chat=True,
            manifest_id="11111111-1111-1111-1111-111111111111",
        )
        assert inp.manifest_id == "11111111-1111-1111-1111-111111111111"


class TestBuildPublishPlanCopilotChat:
    def test_copilot_chat_alone_invokes_cli_with_aiteammate(self) -> None:
        # The GA CLI only emits a starter zip in --aiteammate mode; the
        # Copilot Chat path post-processes that zip. So the CLI argv
        # must include --aiteammate even when only --copilot-chat was
        # requested at our wrapper level.
        plan = build_publish_plan(PublishInputs(agent_name="x", copilot_chat=True))
        assert "--aiteammate" in plan.step.argv

    def test_both_surfaces_invokes_cli_once_with_aiteammate(self) -> None:
        plan = build_publish_plan(
            PublishInputs(agent_name="x", aiteammate=True, copilot_chat=True)
        )
        # Only one --aiteammate flag appears (CLI emits one zip).
        assert plan.step.argv.count("--aiteammate") == 1

    def test_render_human_copilot_chat_alone(self) -> None:
        plan = build_publish_plan(PublishInputs(agent_name="x", copilot_chat=True))
        text = plan.render_human()
        assert "Custom Engine Agent" in text
        assert "Microsoft Admin Portal" in text

    def test_render_human_both_surfaces(self) -> None:
        plan = build_publish_plan(
            PublishInputs(agent_name="x", aiteammate=True, copilot_chat=True)
        )
        text = plan.render_human()
        assert "AI Teammate + Custom Engine Agent" in text
        assert "M365 Admin Centre" in text
        assert "Microsoft Admin Portal" in text
        assert "manifest-id: auto" in text

    def test_render_human_bot_id_override_surfaced(self) -> None:
        plan = build_publish_plan(
            PublishInputs(
                agent_name="x",
                copilot_chat=True,
                bot_id="00000000-0000-0000-0000-000000000bad",
            )
        )
        text = plan.render_human()
        assert "00000000-0000-0000-0000-000000000bad" in text
        assert "override" in text

    def test_render_human_manifest_id_override_surfaced(self) -> None:
        plan = build_publish_plan(
            PublishInputs(
                agent_name="x",
                copilot_chat=True,
                manifest_id="11111111-1111-1111-1111-111111111111",
            )
        )
        assert "11111111-1111-1111-1111-111111111111" in plan.render_human()


class TestCopilotChatAppVersion:
    def test_first_publish_uses_template_version(self) -> None:
        from hermes_a365.publish import _resolve_next_copilot_chat_app_version

        assert (
            _resolve_next_copilot_chat_app_version(
                template_version="1.1.4",
                last_published=None,
                override=None,
            )
            == "1.1.4"
        )

    def test_republish_bumps_patch_when_template_unchanged(self) -> None:
        from hermes_a365.publish import _resolve_next_copilot_chat_app_version

        assert (
            _resolve_next_copilot_chat_app_version(
                template_version="1.1.4",
                last_published="1.1.4",
                override=None,
            )
            == "1.1.5"
        )

    def test_cli_template_jump_forward_is_honoured(self) -> None:
        from hermes_a365.publish import _resolve_next_copilot_chat_app_version

        assert (
            _resolve_next_copilot_chat_app_version(
                template_version="1.2.0",
                last_published="1.1.9",
                override=None,
            )
            == "1.2.0"
        )

    def test_override_must_beat_last_published(self) -> None:
        from hermes_a365.publish import _resolve_next_copilot_chat_app_version

        with pytest.raises(ValueError, match="strictly greater"):
            _resolve_next_copilot_chat_app_version(
                template_version="1.1.4",
                last_published="1.1.6",
                override="1.1.5",
            )

    def test_publish_state_round_trip(self, tmp_path: Path) -> None:
        from hermes_a365.publish import _copilot_chat_publish_state_path

        path = _copilot_chat_publish_state_path(tmp_path, "my-agent")
        manifest_id = "11111111-1111-1111-1111-111111111111"
        _write_publish_state(
            path,
            manifest_id=manifest_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
        )
        assert (
            _read_publish_state(path, manifest_id=manifest_id, hermes_home=tmp_path)
            == "1.1.4"
        )
        assert (
            _read_publish_state(
                path,
                manifest_id="22222222-2222-2222-2222-222222222222",
                hermes_home=tmp_path,
            )
            is None
        )

    def test_unparseable_template_requires_app_version_on_first_publish(self) -> None:
        from hermes_a365.publish import _resolve_next_copilot_chat_app_version

        with pytest.raises(ValueError, match="--app-version"):
            _resolve_next_copilot_chat_app_version(
                template_version=None,
                last_published=None,
                override=None,
            )

    def test_unparseable_template_still_bumps_from_last_published(self) -> None:
        from hermes_a365.publish import _resolve_next_copilot_chat_app_version

        assert (
            _resolve_next_copilot_chat_app_version(
                template_version=None,
                last_published="1.1.5",
                override=None,
            )
            == "1.1.6"
        )

    def test_cea_manifest_id_for_auto_reuse_requires_minted_flag(
        self, tmp_path: Path
    ) -> None:
        from hermes_a365.publish import (
            _cea_manifest_id_for_auto_reuse,
            _copilot_chat_publish_state_path,
            _load_copilot_chat_publish_state_record,
        )

        manifest_id = "11111111-1111-1111-1111-111111111111"
        path = _copilot_chat_publish_state_path(tmp_path, "my-agent")
        _write_publish_state(
            path,
            manifest_id=manifest_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
            cea_catalog_id_minted=False,
        )
        record = _load_copilot_chat_publish_state_record(
            path,
            hermes_home=tmp_path,
            tenant_id=_TEST_TENANT,
            profile=_PUBLISH_STATE_PROFILE,
        )
        assert _cea_manifest_id_for_auto_reuse(record, aiteammate_catalog_id=None) is None
        _write_publish_state(
            path,
            manifest_id=manifest_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
            cea_catalog_id_minted=True,
        )
        record = _load_copilot_chat_publish_state_record(
            path,
            hermes_home=tmp_path,
            tenant_id=_TEST_TENANT,
            profile=_PUBLISH_STATE_PROFILE,
        )
        assert (
            _cea_manifest_id_for_auto_reuse(record, aiteammate_catalog_id=None)
            == manifest_id
        )

    def test_resolve_copilot_chat_manifest_id_reuses_auto_from_state(
        self, tmp_path: Path
    ) -> None:
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            _resolve_copilot_chat_manifest_id_for_apply,
        )

        manifest_id = "22222222-2222-2222-2222-222222222222"
        path = _copilot_chat_publish_state_path(tmp_path, "my-agent")
        _write_publish_state(
            path,
            manifest_id=manifest_id,
            app_version="1.0.0",
            hermes_home=tmp_path,
            cea_catalog_id_minted=True,
        )
        inputs = PublishInputs(
            agent_name="My Agent", copilot_chat=True, manifest_id="auto"
        )
        resolved = _resolve_copilot_chat_manifest_id_for_apply(
            inputs,
            hermes_home=tmp_path,
            tenant_id=_TEST_TENANT,
            profile=_PUBLISH_STATE_PROFILE,
            source_manifest={"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        )
        assert resolved.manifest_id == manifest_id
        assert resolved.reused_from_state is True

    def test_manifest_guid_canonicalization_continues_version_sequence(
        self, tmp_path: Path
    ) -> None:
        from hermes_a365.publish import _copilot_chat_publish_state_path

        manifest_id = "11111111-1111-1111-1111-111111111111"
        path = _copilot_chat_publish_state_path(tmp_path, "my-agent")
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "manifestId": manifest_id.upper(),
                    "lastAppVersion": "1.1.5",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        assert (
            _read_publish_state(path, manifest_id=manifest_id, hermes_home=tmp_path)
            == "1.1.5"
        )


class TestCopilotChatPublishStateIntegrity:
    def _seed_zip(self, tmp_path: Path, manifest: dict) -> Path:
        import zipfile

        zp = tmp_path / "manifest.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        return zp

    def test_corrupt_state_file_fails_apply_and_is_untouched(self, tmp_path: Path):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        manifest_id = "33333333-3333-3333-3333-333333333333"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        state_path.parent.mkdir(parents=True)
        corrupt = "{broken"
        state_path.write_text(corrupt, encoding="utf-8")

        zp = self._seed_zip(
            tmp_path,
            {
                "id": manifest_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        mutator = FakeMutator(
            scripted=[
                RunResult(
                    argv=[],
                    returncode=0,
                    stdout=f"Package created: {zp}",
                    stderr="",
                )
            ]
        )
        with pytest.raises(PublishError, match="not valid JSON"):
            apply_publish_plan(plan, mutator=mutator, hermes_home=tmp_path)
        assert state_path.read_text(encoding="utf-8") == corrupt

    def test_invalid_last_app_version_in_state_fails_apply(self, tmp_path: Path):
        from hermes_a365.publish import _copilot_chat_publish_state_path, build_publish_plan

        manifest_id = "11111111-1111-1111-1111-111111111111"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "manifestId": manifest_id,
                    "lastAppVersion": "not-semver",
                }
            ),
            encoding="utf-8",
        )
        zp = self._seed_zip(
            tmp_path,
            {
                "id": manifest_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
            },
        )
        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        mutator = FakeMutator(
            scripted=[
                RunResult(
                    argv=[],
                    returncode=0,
                    stdout=f"Package created: {zp}",
                    stderr="",
                )
            ]
        )
        with pytest.raises(PublishError, match="invalid v1 fields"):
            apply_publish_plan(plan, mutator=mutator, hermes_home=tmp_path)

    def test_symlinked_agent_dir_refuses_state_write(self, tmp_path: Path):
        from hermes_a365.publish import _copilot_chat_publish_state_path

        hermes = tmp_path / "hermes"
        agents = hermes / "agents"
        agents.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (agents / "agent").symlink_to(outside)
        path = _copilot_chat_publish_state_path(hermes, "agent")
        with pytest.raises(PublishError, match="escapes"):
            _write_publish_state(
                path,
                manifest_id="11111111-1111-1111-1111-111111111111",
                app_version="1.0.0",
                hermes_home=hermes,
            )

    def test_invalid_slug_before_a365_runs(self, tmp_path: Path):
        from hermes_a365.publish import build_publish_plan

        plan = build_publish_plan(
            PublishInputs(agent_name="x", copilot_chat=True, slug="../escape")
        )
        mutator = FakeMutator()
        with pytest.raises(PublishError, match="path separators"):
            apply_publish_plan(plan, mutator=mutator, hermes_home=tmp_path)
        assert not any(call[:2] == ["a365", "publish"] for call in mutator.calls)


class TestTransformManifestToCopilotChat:
    """Pure-function tests for the manifest transform."""

    def test_bumps_manifest_version_to_1_27(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {"manifestVersion": "devPreview"}, bot_id="bid"
        )
        # #74 — 1.27 is the minimum manifest version carrying type:"prompt" starters.
        assert out["manifestVersion"] == "1.27"

    def test_strips_agentic_user_templates(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {"agenticUserTemplates": [{"a": 1}]}, bot_id="bid"
        )
        assert "agenticUserTemplates" not in out

    def test_inserts_bots_block(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat({}, bot_id="bid", scopes=("personal",))
        assert out["bots"] == [
            {
                "botId": "bid",
                "scopes": ["personal"],
                "supportsCalling": False,
                "supportsVideo": False,
                # #76c — Teams file transfer both directions (personal scope).
                "supportsFiles": True,
                "isNotificationOnly": False,
                "commandLists": [
                    {
                        "scopes": ["copilot", "personal"],
                        "commands": [
                            {
                                "title": "What can you do?",
                                "description": "What can you help me with?",
                                "type": "prompt",
                                "prompt": "What can you help me with?",
                            },
                            {
                                "title": "Get started",
                                "description": "How do I get started with you?",
                                "type": "prompt",
                                "prompt": "How do I get started with you?",
                            },
                            {
                                "title": "Show an example",
                                "description": "Show me an example of something you can do.",
                                "type": "prompt",
                                "prompt": "Show me an example of something you can do.",
                            },
                        ],
                    }
                ],
            }
        ]

    def test_prompt_starters_default_are_type_prompt(self) -> None:
        # #74 — default command list emits type:"prompt" starters (title + prompt,
        # no description — description is optional in 1.27).
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat({}, bot_id="bid")
        cmds = out["bots"][0]["commandLists"][0]["commands"]
        assert len(cmds) >= 1
        for c in cmds:
            assert c["type"] == "prompt"
            assert c["title"] and c["prompt"]
            # description drives the card subtitle (walk-observed) — set to prompt.
            assert c["description"] == c["prompt"]

    def test_prompt_starters_operator_override_and_cap(self) -> None:
        from hermes_a365.publish import (
            _PROMPT_STARTER_MAX,
            _transform_manifest_to_copilot_chat,
        )

        supplied = tuple(
            {"title": f"t{i}", "prompt": f"p{i}"} for i in range(_PROMPT_STARTER_MAX + 3)
        )
        out = _transform_manifest_to_copilot_chat(
            {}, bot_id="bid", prompt_starters=supplied
        )
        cmds = out["bots"][0]["commandLists"][0]["commands"]
        assert len(cmds) == _PROMPT_STARTER_MAX  # capped at the schema max
        assert cmds[0] == {
            "title": "t0",
            "description": "p0",
            "type": "prompt",
            "prompt": "p0",
        }

    def test_prompt_starter_scopes_are_copilot_personal(self) -> None:
        # Prompt starters render on copilot + personal (zero-state); the team
        # scope is the @mention command menu, deliberately excluded.
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat({}, bot_id="bid")
        assert out["bots"][0]["commandLists"][0]["scopes"] == ["copilot", "personal"]

    def test_parse_prompt_starter_splits_on_pipe(self) -> None:
        from hermes_a365.publish import _parse_prompt_starter

        assert _parse_prompt_starter("Hi there|Say hello") == {
            "title": "Hi there",
            "prompt": "Say hello",
        }
        # No pipe → the whole string is used for both title and prompt.
        assert _parse_prompt_starter("Just a title") == {
            "title": "Just a title",
            "prompt": "Just a title",
        }

    def test_parse_prompt_starter_rejects_empty_title(self) -> None:
        from hermes_a365.publish import _parse_prompt_starter

        with pytest.raises(ValueError, match="non-empty title"):
            _parse_prompt_starter("|only a prompt")

    def test_default_copilot_chat_bot_scopes_include_copilot(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat({}, bot_id="bid")
        assert out["bots"][0]["scopes"] == ["copilot", "personal", "team"]

    def test_inserts_copilot_agents_block(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat({}, bot_id="bid")
        assert out["copilotAgents"] == {
            "customEngineAgents": [{"type": "bot", "id": "bid"}]
        }

    def test_manifest_id_override_updates_catalog_id_only(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {"id": "blueprint-app-id"},
            bot_id="bot-framework-app-id",
            manifest_id="11111111-1111-1111-1111-111111111111",
        )
        assert out["id"] == "11111111-1111-1111-1111-111111111111"
        assert out["bots"][0]["botId"] == "bot-framework-app-id"

    def test_distinguish_name_short_adds_cc_suffix(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {"name": {"short": "Hermes Inbox Helper R8", "full": "Hermes Inbox Helper"}},
            bot_id="bid",
            distinguish_name_short=True,
        )
        assert out["name"]["short"] == "Hermes Inbox Helper R8 CC"
        assert len(out["name"]["short"]) <= 30

    def test_scopes_propagated(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {}, bot_id="bid", scopes=("personal", "team", "groupChat")
        )
        assert out["bots"][0]["scopes"] == ["personal", "team", "groupChat"]

    def test_stamps_app_version_when_requested(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {"version": "1.1.4"}, bot_id="bid", app_version="1.1.5"
        )
        assert out["version"] == "1.1.5"

    def test_preserves_unrelated_fields(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        src = {
            "name": {"short": "X", "full": "X agent"},
            "description": {"short": "d", "full": "dd"},
            "icons": {"color": "icon.png", "outline": "icon-out.png"},
            "developer": {"name": "Acme"},
            "accentColor": "#0078D4",
            "version": "9.9.9",
        }
        out = _transform_manifest_to_copilot_chat(
            src, bot_id="bid", app_version="1.0.1"
        )
        assert out["name"] == src["name"]
        assert out["version"] == "1.0.1"
        assert out["description"] == src["description"]
        assert out["icons"] == src["icons"]
        assert out["developer"] == src["developer"]
        assert out["accentColor"] == src["accentColor"]

    def test_does_not_mutate_input(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        src = {"manifestVersion": "devPreview", "agenticUserTemplates": [1]}
        _transform_manifest_to_copilot_chat(src, bot_id="bid")
        # Input untouched
        assert src == {"manifestVersion": "devPreview", "agenticUserTemplates": [1]}


class TestExtractBotIdFromManifest:
    def test_from_web_application_info(self) -> None:
        from hermes_a365.publish import _extract_bot_id_from_manifest

        m = {"webApplicationInfo": {"id": "abc-123"}}
        assert _extract_bot_id_from_manifest(m) == "abc-123"

    def test_from_bots_array_fallback(self) -> None:
        from hermes_a365.publish import _extract_bot_id_from_manifest

        m = {"bots": [{"botId": "def-456"}]}
        assert _extract_bot_id_from_manifest(m) == "def-456"

    def test_web_application_info_preferred_over_bots(self) -> None:
        from hermes_a365.publish import _extract_bot_id_from_manifest

        m = {"webApplicationInfo": {"id": "abc"}, "bots": [{"botId": "def"}]}
        assert _extract_bot_id_from_manifest(m) == "abc"

    def test_returns_none_when_missing(self) -> None:
        from hermes_a365.publish import _extract_bot_id_from_manifest

        assert _extract_bot_id_from_manifest({}) is None

    def test_returns_none_on_non_string_id(self) -> None:
        from hermes_a365.publish import _extract_bot_id_from_manifest

        assert _extract_bot_id_from_manifest({"webApplicationInfo": {"id": 42}}) is None

    def test_top_level_id_fallback(self) -> None:
        # GA CLI 1.1.174+ AI Teammate emit has the bot/app id only at
        # the manifest's top-level ``id`` field (no webApplicationInfo,
        # no bots block). Live walkthrough 2026-05-12 surfaced this.
        from hermes_a365.publish import _extract_bot_id_from_manifest

        m = {
            "id": "2e5e2dea-af3c-4707-a6f9-f2a0ee551a7a",
            "agenticUserTemplates": [{"id": "x", "file": "y.json"}],
        }
        assert (
            _extract_bot_id_from_manifest(m)
            == "2e5e2dea-af3c-4707-a6f9-f2a0ee551a7a"
        )

    def test_web_application_info_preferred_over_top_level_id(self) -> None:
        from hermes_a365.publish import _extract_bot_id_from_manifest

        m = {"id": "top-level", "webApplicationInfo": {"id": "wai"}}
        assert _extract_bot_id_from_manifest(m) == "wai"


class TestPatchManifestToCopilotChat:
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

    def test_patches_manifest_extracting_bot_id(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import _patch_manifest_to_copilot_chat

        zp = self._make_zip(
            tmp_path,
            {
                "manifestVersion": "devPreview",
                "webApplicationInfo": {"id": "the-app-id"},
                "agenticUserTemplates": [
                    {"id": "x", "file": "agenticUserTemplateManifest.json"}
                ],
                "name": {"short": "X", "full": "X agent"},
            },
            extra_files={
                "icon.png": b"png-bytes",
                "agenticUserTemplateManifest.json": b"ai-teammate-template",
            },
        )
        result = _patch_manifest_to_copilot_chat(
            str(zp),
            manifest_id="11111111-1111-1111-1111-111111111111",
            distinguish_name_short=True,
        )
        assert result is not None
        bot_id, summary = result
        assert bot_id == "the-app-id"
        assert summary["manifest_version"] == "1.27"
        assert summary["manifest_id"] == "11111111-1111-1111-1111-111111111111"
        assert summary["scopes"] == ["copilot", "personal", "team"]
        assert summary["dropped_agentic_user_templates"] is True
        assert summary["dropped_agentic_template_files"] == [
            "agenticUserTemplateManifest.json"
        ]
        # Re-zip preserves other files
        with zipfile.ZipFile(zp) as zf:
            assert set(zf.namelist()) == {"manifest.json", "icon.png"}
            assert zf.read("icon.png") == b"png-bytes"
            m = json.loads(zf.read("manifest.json"))
            assert m["id"] == "11111111-1111-1111-1111-111111111111"
            assert m["manifestVersion"] == "1.27"
            assert m["bots"][0]["botId"] == "the-app-id"
            assert m["copilotAgents"]["customEngineAgents"] == [
                {"type": "bot", "id": "the-app-id"}
            ]
            assert "agenticUserTemplates" not in m
            assert m["name"] == {"short": "X CC", "full": "X agent"}

    def test_bot_id_override_wins(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import _patch_manifest_to_copilot_chat

        zp = self._make_zip(
            tmp_path,
            {"webApplicationInfo": {"id": "auto"}},
        )
        result = _patch_manifest_to_copilot_chat(str(zp), bot_id_override="manual")
        assert result is not None
        bot_id, _ = result
        assert bot_id == "manual"
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["bots"][0]["botId"] == "manual"

    def test_returns_none_when_no_bot_id_resolvable(self, tmp_path):
        from hermes_a365.publish import _patch_manifest_to_copilot_chat

        # No webApplicationInfo, no bots, no override → no way to determine bot id.
        zp = self._make_zip(tmp_path, {"name": {"short": "X"}})
        assert _patch_manifest_to_copilot_chat(str(zp)) is None

    def test_returns_none_when_zip_missing(self, tmp_path):
        from hermes_a365.publish import _patch_manifest_to_copilot_chat

        assert _patch_manifest_to_copilot_chat(str(tmp_path / "nope.zip")) is None

    def test_returns_none_when_no_manifest_json(self, tmp_path):
        import zipfile

        from hermes_a365.publish import _patch_manifest_to_copilot_chat

        zp = tmp_path / "manifest.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("other.json", b"{}")
        assert _patch_manifest_to_copilot_chat(str(zp)) is None

    def test_returns_none_on_bad_json(self, tmp_path):
        import zipfile

        from hermes_a365.publish import _patch_manifest_to_copilot_chat

        zp = tmp_path / "manifest.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("manifest.json", b"not-json")
        assert _patch_manifest_to_copilot_chat(str(zp)) is None

    def test_dropped_aut_summary_false_when_no_block(self, tmp_path):
        from hermes_a365.publish import _patch_manifest_to_copilot_chat

        zp = self._make_zip(tmp_path, {"webApplicationInfo": {"id": "bid"}})
        result = _patch_manifest_to_copilot_chat(str(zp))
        assert result is not None
        _, summary = result
        assert summary["dropped_agentic_user_templates"] is False


class TestApplyPublishPlanCopilotChat:
    """End-to-end apply tests for the Custom Engine Agent flow."""

    def _scripted_zip_emit(self, zp):
        return FakeMutator(
            scripted=[
                RunResult(
                    argv=[],
                    returncode=0,
                    stdout=f"Package created: {zp}",
                    stderr="",
                )
            ]
        )

    def _seed_manifest_zip(self, tmp_path, manifest: dict, extra_files: dict | None = None):
        import json
        import zipfile

        zp = tmp_path / "manifest.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            for name, blob in (extra_files or {}).items():
                zf.writestr(name, blob)
        return zp

    def test_copilot_chat_alone_transforms_in_place(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "manifestVersion": "devPreview",
                "id": "44444444-4444-4444-4444-444444444444",
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "agenticUserTemplates": [{"a": 1}],
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, app_version="1.1.4")
        )
        result = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

        # Copilot Chat-only mode: no AI Teammate package, single zip
        # transformed in place.
        assert result.package_path is None
        assert result.copilot_chat_package_path == str(zp)
        assert result.copilot_chat_bot_id == "the-bot"
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["version"] == "1.1.4"
            assert m["manifestVersion"] == "1.27"
            assert m["bots"][0]["botId"] == "the-bot"
            assert "agenticUserTemplates" not in m
        # Operator-facing message points at the MAC Agents upload path.
        assert any("Microsoft Admin Portal" in msg for msg in result.messages)
        assert not any("AI Teammate package" in msg for msg in result.messages)

    def test_no_publish_record_fails_without_app_version(self, tmp_path):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        manifest = {
            "manifestVersion": "devPreview",
            "id": "44444444-4444-4444-4444-444444444444",
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        zp = self._seed_manifest_zip(tmp_path, manifest)
        hermes = tmp_path / "hermes"
        state_path = _copilot_chat_publish_state_path(hermes, "x")
        assert not state_path.is_file()

        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        with pytest.raises(PublishError, match="no Copilot Chat publish record"):
            apply_publish_plan(
                plan, mutator=self._scripted_zip_emit(zp), hermes_home=hermes
            )
        assert not state_path.is_file()

    def test_manifest_id_auto_reuses_catalog_id_and_bumps_version(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        manifest = {
            "manifestVersion": "devPreview",
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        hermes = tmp_path / "hermes"
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, manifest_id="auto")
        )
        round1 = tmp_path / "r1"
        round1.mkdir()
        zp1 = self._seed_manifest_zip(round1, manifest)
        first = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp1), hermes_home=hermes
        )
        first_id = first.copilot_chat_manifest_id
        assert first_id is not None
        with zipfile.ZipFile(zp1) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["id"] == first_id
            assert m["version"] == "1.1.4"

        round2 = tmp_path / "r2"
        round2.mkdir()
        zp2 = self._seed_manifest_zip(round2, manifest)
        second = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp2), hermes_home=hermes
        )
        assert second.copilot_chat_manifest_id == first_id
        with zipfile.ZipFile(zp2) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["id"] == first_id
            assert m["version"] == "1.1.5"
        assert any(
            "reusing catalog manifest.id" in m for m in second.messages
        )

    def test_manifest_id_auto_three_consecutive_applies_bump_version(
        self, tmp_path
    ):
        import json
        import zipfile

        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        manifest = {
            "manifestVersion": "devPreview",
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        hermes = tmp_path / "hermes"
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, manifest_id="auto")
        )
        state_path = _copilot_chat_publish_state_path(hermes, "x")
        manifest_id = None
        for expected_version, round_dir in zip(
            ("1.1.4", "1.1.5", "1.1.6"),
            (tmp_path / "r1", tmp_path / "r2", tmp_path / "r3"),
            strict=True,
        ):
            round_dir.mkdir()
            zp = self._seed_manifest_zip(round_dir, manifest)
            result = apply_publish_plan(
                plan, mutator=self._scripted_zip_emit(zp), hermes_home=hermes
            )
            with zipfile.ZipFile(zp) as zf:
                m = json.loads(zf.read("manifest.json"))
                assert m["version"] == expected_version
            if manifest_id is None:
                manifest_id = result.copilot_chat_manifest_id
            else:
                assert result.copilot_chat_manifest_id == manifest_id
        record = json.loads(state_path.read_text(encoding="utf-8"))
        assert record["ceaCatalogIdMinted"] is True
        assert record["lastAppVersion"] == "1.1.6"

    def test_copilot_chat_republish_bumps_app_version(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        manifest = {
            "manifestVersion": "devPreview",
            "id": "44444444-4444-4444-4444-444444444444",
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        zp = self._seed_manifest_zip(tmp_path, manifest)
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, app_version="1.1.4")
        )
        first = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )
        assert first.copilot_chat_package_path is not None
        with zipfile.ZipFile(zp) as zf:
            assert json.loads(zf.read("manifest.json"))["version"] == "1.1.4"

        round2 = tmp_path / "round2"
        round2.mkdir()
        zp2 = self._seed_manifest_zip(round2, manifest)
        plan_second = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True)
        )
        second = apply_publish_plan(
            plan_second, mutator=self._scripted_zip_emit(zp2), hermes_home=tmp_path
        )
        with zipfile.ZipFile(zp2) as zf:
            assert json.loads(zf.read("manifest.json"))["version"] == "1.1.5"
        assert any("recorded last published app version" in m for m in second.messages)

    def _scripted_template_extract(self, manifest_dir):
        # Mimics Microsoft a365 CLI >= 1.1.181: extracts a manifest template
        # to a directory and stops — NO zip path appears in the output.
        return FakeMutator(
            scripted=[
                RunResult(
                    argv=[],
                    returncode=0,
                    stdout=(
                        "Extracting manifest templates...\n"
                        f"Extracted manifest templates to {manifest_dir}\n"
                        f"Manifest updated: {manifest_dir}/manifest.json\n"
                        "Customize before packaging:\n"
                        "  name.short - EXCEEDS 30 chars (32)\n"
                    ),
                    stderr="",
                )
            ]
        )

    def _seed_manifest_dir(self, tmp_path, manifest: dict, extra_files=None):
        import json

        d = tmp_path / "manifest"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2))
        for name, blob in (extra_files or {}).items():
            (d / name).write_bytes(blob)
        return d

    def test_packages_extracted_template_when_cli_emits_no_zip(self, tmp_path):
        # Microsoft a365 CLI >= 1.1.181 extracts a template + stops instead of
        # emitting a zip. The wrapper must package the directory itself and
        # still produce a valid Custom Engine Agent zip (caught live by the
        # v0.7.5 walk — the old flow silently produced nothing).
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        d = self._seed_manifest_dir(
            tmp_path,
            {
                "manifestVersion": "devPreview",
                "id": "2e5e2dea-af3c-4707-a6f9-f2a0ee551a7a",
                "version": "1.0.0",
                "name": {"short": "Hermes Inbox Helper R8 Blueprint", "full": "X"},
                "bots": [],
            },
            extra_files={
                "color.png": b"png",
                "outline.png": b"png",
                "agenticUserTemplateManifest.json": b"ai-teammate",
            },
        )
        plan = build_publish_plan(
            PublishInputs(
                agent_name="Hermes Inbox Helper R8",
                copilot_chat=True,
                bot_id="1c2b61bc-fa6a-4c7b-9656-a82b662dacfe",
                manifest_id="auto",
            )
        )
        result = apply_publish_plan(
            plan,
            mutator=self._scripted_template_extract(str(d)),
            hermes_home=tmp_path / "hermes",
        )

        assert result.copilot_chat_package_path is not None
        assert result.copilot_chat_package_path.endswith("manifest.zip")
        assert result.copilot_chat_bot_id == "1c2b61bc-fa6a-4c7b-9656-a82b662dacfe"
        assert any(
            "packaged extracted manifest template" in m for m in result.messages
        )
        with zipfile.ZipFile(result.copilot_chat_package_path) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert "agenticUserTemplateManifest.json" not in names  # dropped
            assert {"color.png", "outline.png"} <= names  # icons kept
            m = json.loads(zf.read("manifest.json"))
            assert m["manifestVersion"] == "1.27"
            assert m["bots"][0]["botId"] == "1c2b61bc-fa6a-4c7b-9656-a82b662dacfe"
            assert m["copilotAgents"]["customEngineAgents"] == [
                {"type": "bot", "id": "1c2b61bc-fa6a-4c7b-9656-a82b662dacfe"}
            ]
            assert m["id"] != "2e5e2dea-af3c-4707-a6f9-f2a0ee551a7a"  # fresh manifest-id (auto)
            # name.short truncated 32 -> <= 30 at a word boundary.
            assert m["name"]["short"] == "Hermes Inbox Helper R8"

    def test_packages_bounded_nested_regular_manifest_assets(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import _zip_manifest_dir

        d = tmp_path / "manifest"
        nested = d / "assets" / "icons"
        nested.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"manifestVersion": "1.27"}))
        (nested / "color.png").write_bytes(b"png")

        package = _zip_manifest_dir(str(d))

        assert package is not None
        with zipfile.ZipFile(package) as zf:
            assert set(zf.namelist()) == {"manifest.json", "assets/icons/color.png"}

    def test_extracted_manifest_packaging_failure_is_explicit(self, tmp_path):
        d = tmp_path / "manifest"
        d.mkdir()
        (d / "not-a-manifest.txt").write_text("no manifest")
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, bot_id="bot-id")
        )

        with pytest.raises(PublishError, match="could not be packaged safely"):
            apply_publish_plan(
                plan,
                mutator=self._scripted_template_extract(str(d)),
                hermes_home=tmp_path,
            )

    def test_extract_manifest_dir_parses_both_phrasings(self):
        from hermes_a365.publish import _extract_manifest_dir

        assert _extract_manifest_dir(
            "Extracted manifest templates to /tmp/walk/manifest\n"
        ) == "/tmp/walk/manifest"
        # Falls back to the parent of the manifest.json line.
        assert _extract_manifest_dir(
            "Manifest updated: /tmp/walk/manifest/manifest.json\n"
        ) == "/tmp/walk/manifest"
        assert _extract_manifest_dir("no manifest here") is None

    def test_both_surfaces_keeps_aiteammate_zip_and_emits_sibling(self, tmp_path):
        import json
        import uuid
        import zipfile

        from hermes_a365.publish import build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "manifestVersion": "devPreview",
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "version": "1.0.0",
                "webApplicationInfo": {"id": "the-bot"},
                "agenticUserTemplates": [{"a": 1}],
                "name": {"short": "Hermes Inbox Helper R8", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", aiteammate=True, copilot_chat=True)
        )
        result = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

        assert result.package_path == str(zp)
        assert result.copilot_chat_package_path is not None
        assert result.copilot_chat_package_path != str(zp)
        # Sibling name uses the .copilot-chat infix.
        assert result.copilot_chat_package_path.endswith(".copilot-chat.zip")
        assert result.copilot_chat_bot_id == "the-bot"
        assert result.copilot_chat_manifest_id is not None
        uuid.UUID(result.copilot_chat_manifest_id)

        # AI Teammate zip stayed in devPreview shape.
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["manifestVersion"] == "devPreview"
            assert m["id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            assert "agenticUserTemplates" in m

        # Copilot Chat sibling is in 1.27 shape.
        with zipfile.ZipFile(result.copilot_chat_package_path) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["manifestVersion"] == "1.27"
            assert m["id"] == result.copilot_chat_manifest_id
            assert "agenticUserTemplates" not in m
            assert m["bots"][0]["botId"] == "the-bot"
            assert m["name"]["short"] == "Hermes Inbox Helper R8 CC"

        # Both upload destinations surfaced.
        assert any("M365 Admin Centre" in msg for msg in result.messages)
        assert any("Microsoft Admin Portal" in msg for msg in result.messages)

    def test_explicit_manifest_id_used_for_copilot_chat_zip(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "version": "1.0.0",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                manifest_id="11111111-1111-1111-1111-111111111111",
                app_version="1.0.0",
            )
        )
        result = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

        assert result.copilot_chat_manifest_id == (
            "11111111-1111-1111-1111-111111111111"
        )
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["id"] == "11111111-1111-1111-1111-111111111111"
            assert m["bots"][0]["botId"] == "the-bot"

    def test_copilot_chat_alone_without_manifest_id_keeps_catalog_id(
        self, tmp_path
    ):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "version": "1.0.0",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, app_version="1.0.0")
        )
        result = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

        assert result.copilot_chat_manifest_id == (
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        )
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            assert m["name"]["short"] == "X"

    def test_copilot_chat_zip_omits_ai_teammate_template_sidecar(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "version": "1.0.0",
                "webApplicationInfo": {"id": "the-bot"},
                "agenticUserTemplates": [
                    {"id": "x", "file": "agenticUserTemplateManifest.json"}
                ],
                "name": {
                    "short": "Hermes Inbox Helper R8",
                    "full": "Hermes Inbox Helper R8",
                },
            },
            extra_files={
                "agenticUserTemplateManifest.json": b"name.short/name.full echo source",
                "color.png": b"icon",
            },
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, app_version="1.0.0")
        )
        result = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

        with zipfile.ZipFile(result.copilot_chat_package_path) as zf:
            assert "agenticUserTemplateManifest.json" not in zf.namelist()
            assert "color.png" in zf.namelist()
            m = json.loads(zf.read("manifest.json"))
            assert "agenticUserTemplates" not in m

    def test_bot_id_override_used(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "version": "1.0.0",
                "webApplicationInfo": {"id": "auto-id"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                bot_id="override-id",
                app_version="1.0.0",
            )
        )
        result = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

        assert result.copilot_chat_bot_id == "override-id"
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["bots"][0]["botId"] == "override-id"

    def test_name_short_truncated_on_copilot_chat_zip(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "version": "1.0.0",
                "webApplicationInfo": {"id": "bid"},
                "name": {
                    "short": "Hermes Inbox Helper R8 Blueprint",  # 32 chars
                    "full": "Hermes Inbox Helper R8 Blueprint",
                },
            },
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, app_version="1.0.0")
        )
        result = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

        # name.short truncated on the Copilot Chat zip (the only zip kept).
        with zipfile.ZipFile(result.copilot_chat_package_path) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["name"]["short"] == "Hermes Inbox Helper R8"
        assert any("truncated name.short" in msg for msg in result.messages)

    def test_transform_failure_surfaces_warning(self, tmp_path):
        from hermes_a365.publish import build_publish_plan

        # Manifest with no resolvable bot id and no override.
        zp = self._seed_manifest_zip(
            tmp_path,
            {"version": "1.0.0", "name": {"short": "X", "full": "X"}},
        )
        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        result = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

        assert result.copilot_chat_package_path is None
        assert any(
            "Copilot Chat transform failed" in msg for msg in result.messages
        )

    def test_transform_failure_rolls_back_sibling_in_both_mode(self, tmp_path):
        from pathlib import Path

        from hermes_a365.publish import build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {"version": "1.0.0", "name": {"short": "X", "full": "X"}},
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", aiteammate=True, copilot_chat=True)
        )
        result = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

        assert result.copilot_chat_package_path is None
        # AI Teammate zip still present (the seeded zip).
        assert Path(zp).is_file()
        # Sibling was rolled back.
        sibling = Path(zp).with_name(Path(zp).stem + ".copilot-chat.zip")
        assert not sibling.exists()

    def test_combined_default_reuses_distinct_cea_id(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        aiteammate_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        manifest = {
            "id": aiteammate_id,
            "version": "1.0.0",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "Hermes Inbox Helper R8", "full": "X"},
        }
        hermes = tmp_path / "hermes"
        plan = build_publish_plan(
            PublishInputs(agent_name="X", aiteammate=True, copilot_chat=True)
        )
        round1 = tmp_path / "r1"
        round1.mkdir()
        zp1 = self._seed_manifest_zip(round1, manifest)
        first = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp1), hermes_home=hermes
        )
        assert first.copilot_chat_manifest_id != aiteammate_id
        round2 = tmp_path / "r2"
        round2.mkdir()
        zp2 = self._seed_manifest_zip(round2, manifest)
        second = apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp2), hermes_home=hermes
        )
        assert second.copilot_chat_manifest_id == first.copilot_chat_manifest_id
        with zipfile.ZipFile(zp2.with_name(zp2.stem + ".copilot-chat.zip")) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["id"] == first.copilot_chat_manifest_id
            assert m["version"] == "1.0.1"

    def test_auto_does_not_reuse_path_b_template_catalog_id(self, tmp_path):
        import uuid

        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            _resolve_copilot_chat_manifest_id_for_apply,
        )

        template_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_publish_state(
            path,
            manifest_id=template_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
            cea_catalog_id_minted=False,
        )
        resolved = _resolve_copilot_chat_manifest_id_for_apply(
            PublishInputs(agent_name="X", copilot_chat=True, manifest_id="auto"),
            hermes_home=tmp_path,
            tenant_id=_TEST_TENANT,
            profile=_PUBLISH_STATE_PROFILE,
            source_manifest={"id": template_id},
        )
        assert resolved.manifest_id != template_id
        uuid.UUID(resolved.manifest_id)
        assert resolved.freshly_minted is True

    def test_mismatched_manifest_id_in_state_fails_apply(self, tmp_path):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        stored_id = "11111111-1111-1111-1111-111111111111"
        zip_id = "22222222-2222-2222-2222-222222222222"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_publish_state(
            state_path,
            manifest_id=stored_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
        )
        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": zip_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, app_version="1.1.5")
        )
        with pytest.raises(PublishError, match="records manifestId"):
            apply_publish_plan(
                plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
            )
        record = json.loads(state_path.read_text(encoding="utf-8"))
        assert record["manifestId"] == stored_id
        assert record["lastAppVersion"] == "1.1.4"

    def test_new_manifest_id_with_app_version_replaces_publish_state(
        self, tmp_path
    ):
        import json
        import zipfile

        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        stored_id = "11111111-1111-1111-1111-111111111111"
        new_id = "33333333-3333-3333-3333-333333333333"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_publish_state(
            state_path,
            manifest_id=stored_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
        )
        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": stored_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                manifest_id=new_id,
                app_version="2.0.0",
            )
        )
        apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )
        record = json.loads(state_path.read_text(encoding="utf-8"))
        assert record["manifestId"] == new_id
        assert record["lastAppVersion"] == "2.0.0"
        with zipfile.ZipFile(zp) as zf:
            assert json.loads(zf.read("manifest.json"))["id"] == new_id

    def test_path_b_state_then_manifest_id_auto_mints_new_catalog_entry(
        self, tmp_path
    ):
        import json
        import zipfile

        from hermes_a365.publish import build_publish_plan

        template_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        manifest = {
            "manifestVersion": "devPreview",
            "id": template_id,
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        hermes = tmp_path / "hermes"
        path_b = build_publish_plan(
            PublishInputs(
                agent_name="X", copilot_chat=True, app_version="1.1.4"
            )
        )
        round1 = tmp_path / "r1"
        round1.mkdir()
        zp1 = self._seed_manifest_zip(round1, manifest)
        apply_publish_plan(
            path_b, mutator=self._scripted_zip_emit(zp1), hermes_home=hermes
        )
        auto_plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, manifest_id="auto")
        )
        round2 = tmp_path / "r2"
        round2.mkdir()
        zp2 = self._seed_manifest_zip(round2, manifest)
        second = apply_publish_plan(
            auto_plan, mutator=self._scripted_zip_emit(zp2), hermes_home=hermes
        )
        assert second.copilot_chat_manifest_id != template_id
        with zipfile.ZipFile(zp2) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["id"] == second.copilot_chat_manifest_id
            assert m["version"] == "1.1.4"

    def test_combined_default_after_path_b_mints_distinct_cea_id(self, tmp_path):
        import json
        import zipfile
        from pathlib import Path

        from hermes_a365.publish import build_publish_plan

        template_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        manifest = {
            "manifestVersion": "devPreview",
            "id": template_id,
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        hermes = tmp_path / "hermes"
        path_b = build_publish_plan(
            PublishInputs(
                agent_name="X", copilot_chat=True, app_version="1.1.4"
            )
        )
        round1 = tmp_path / "r1"
        round1.mkdir()
        zp1 = self._seed_manifest_zip(round1, manifest)
        apply_publish_plan(
            path_b, mutator=self._scripted_zip_emit(zp1), hermes_home=hermes
        )
        combined = build_publish_plan(
            PublishInputs(agent_name="X", aiteammate=True, copilot_chat=True)
        )
        round2 = tmp_path / "r2"
        round2.mkdir()
        zp2 = self._seed_manifest_zip(round2, manifest)
        second = apply_publish_plan(
            combined, mutator=self._scripted_zip_emit(zp2), hermes_home=hermes
        )
        assert second.copilot_chat_manifest_id != template_id
        sibling = Path(zp2).with_name(Path(zp2).stem + ".copilot-chat.zip")
        with zipfile.ZipFile(sibling) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["id"] == second.copilot_chat_manifest_id
            assert m["id"] != template_id
        with zipfile.ZipFile(zp2) as zf:
            assert json.loads(zf.read("manifest.json"))["id"] == template_id

    def test_combined_manifest_id_matching_aiteammate_refused(self, tmp_path):
        from hermes_a365.publish import build_publish_plan

        template_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        manifest = {
            "id": template_id,
            "version": "1.0.0",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        zp = self._seed_manifest_zip(tmp_path, manifest)
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                aiteammate=True,
                copilot_chat=True,
                manifest_id=template_id,
            )
        )
        with pytest.raises(PublishError, match="matches the AI Teammate catalog id"):
            apply_publish_plan(
                plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
            )

    def test_path_b_manifest_id_mismatch_suggests_pass_stored_id(
        self, tmp_path
    ):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        template_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        other_id = "22222222-2222-2222-2222-222222222222"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_publish_state(
            state_path,
            manifest_id=template_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
            cea_catalog_id_minted=False,
        )
        manifest = {
            "id": template_id,
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        zp = self._seed_manifest_zip(tmp_path, manifest)
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                manifest_id=other_id,
            )
        )
        with pytest.raises(PublishError) as exc:
            apply_publish_plan(
                plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
            )
        message = str(exc.value)
        assert f"Pass --manifest-id {template_id}" in message
        assert "do not pass --manifest-id" not in message
        assert "mint a distinct CEA catalog id" not in message

    def test_mismatch_hint_never_suggests_aiteammate_catalog_id(
        self, tmp_path
    ):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        template_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        other_id = "22222222-2222-2222-2222-222222222222"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_publish_state(
            state_path,
            manifest_id=template_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
        )
        manifest = {
            "id": template_id,
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        zp = self._seed_manifest_zip(tmp_path, manifest)
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                aiteammate=True,
                copilot_chat=True,
                manifest_id=other_id,
            )
        )
        with pytest.raises(PublishError) as exc:
            apply_publish_plan(
                plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
            )
        message = str(exc.value)
        assert template_id not in message or "do not pass --manifest-id" in message
        assert f"Pass --manifest-id {template_id}" not in message

    def test_legacy_v1_publish_state_requires_app_version_to_apply(
        self, tmp_path
    ):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        manifest_id = "44444444-4444-4444-4444-444444444444"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "manifestId": manifest_id,
                    "lastAppVersion": "1.1.4",
                }
            ),
            encoding="utf-8",
        )
        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": manifest_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        with pytest.raises(PublishError, match="legacy schema v1"):
            apply_publish_plan(
                plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
            )

    def test_legacy_v1_cross_tenant_refused_without_app_version(
        self, tmp_path
    ):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        manifest_id = "44444444-4444-4444-4444-444444444444"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "manifestId": manifest_id,
                    "lastAppVersion": "1.1.4",
                }
            ),
            encoding="utf-8",
        )
        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": manifest_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        other_tenant = "bbbbbbbb-0000-0000-0000-000000000002"
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                tenant_id=other_tenant,
            )
        )
        mutator = FakeMutator(
            scripted=[
                RunResult(
                    argv=["az", "account", "show"],
                    returncode=0,
                    stdout=json.dumps({"tenantId": other_tenant}),
                    stderr="",
                ),
                RunResult(
                    argv=[],
                    returncode=0,
                    stdout=f"Package created: {zp}",
                    stderr="",
                ),
            ]
        )
        with pytest.raises(PublishError, match="legacy schema v1"):
            apply_publish_plan(plan, mutator=mutator, hermes_home=tmp_path)

    def test_publish_cli_profile_partitions_state(self, tmp_path, monkeypatch):
        import json

        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_PROFILE", raising=False)
        manifest_id = "44444444-4444-4444-4444-444444444444"
        manifest = {
            "id": manifest_id,
            "version": "1.1.4",
            "webApplicationInfo": {"id": "the-bot"},
            "name": {"short": "X", "full": "X"},
        }
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_publish_state(
            state_path,
            manifest_id=manifest_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
        )
        record = json.loads(state_path.read_text(encoding="utf-8"))
        record["profile"] = "gateway-profile"
        state_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        zp = self._seed_manifest_zip(tmp_path, manifest)
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                profile="gateway-profile",
                app_version="1.1.5",
            )
        )
        apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )
        updated = json.loads(state_path.read_text(encoding="utf-8"))
        assert updated["profile"] == "gateway-profile"
        assert updated["lastAppVersion"] == "1.1.5"

    def test_publish_state_tenant_isolation(self, tmp_path):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        manifest_id = "44444444-4444-4444-4444-444444444444"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_publish_state(
            state_path,
            manifest_id=manifest_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
            cea_catalog_id_minted=True,
        )
        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": manifest_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        other_tenant = "bbbbbbbb-0000-0000-0000-000000000002"
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                tenant_id=other_tenant,
                manifest_id="auto",
            )
        )
        mutator = FakeMutator(
            scripted=[
                RunResult(
                    argv=["az", "account", "show"],
                    returncode=0,
                    stdout=json.dumps({"tenantId": other_tenant}),
                    stderr="",
                ),
                RunResult(
                    argv=[],
                    returncode=0,
                    stdout=f"Package created: {zp}",
                    stderr="",
                ),
            ]
        )
        with pytest.raises(PublishError, match="belongs to tenant"):
            apply_publish_plan(plan, mutator=mutator, hermes_home=tmp_path)

    def test_publish_state_tenant_guid_spelling_variants_accepted(
        self, tmp_path
    ):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        manifest_id = "44444444-4444-4444-4444-444444444444"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_publish_state(
            state_path,
            manifest_id=manifest_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
        )
        record = json.loads(state_path.read_text(encoding="utf-8"))
        record["tenantId"] = "{AAAAAAAA-0000-0000-0000-000000000001}"
        state_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": manifest_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        braced_pin = "{aaaaaaaa-0000-0000-0000-000000000001}"
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                tenant_id=braced_pin,
                app_version="1.1.5",
            )
        )
        apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )
        updated = json.loads(state_path.read_text(encoding="utf-8"))
        assert updated["tenantId"] == _TEST_TENANT
        assert updated["lastAppVersion"] == "1.1.5"

    def test_publish_state_tenant_guid_hyphenless_pin_accepted(self, tmp_path):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            build_publish_plan,
        )

        manifest_id = "44444444-4444-4444-4444-444444444444"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_publish_state(
            state_path,
            manifest_id=manifest_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
        )
        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": manifest_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        hyphenless = "aaaaaaaa000000000000000000000001"
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                tenant_id=hyphenless,
                app_version="1.1.5",
            )
        )
        apply_publish_plan(
            plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
        )

    def test_publish_state_profile_isolation(self, tmp_path, monkeypatch):
        from hermes_a365.publish import (
            _copilot_chat_publish_state_path,
            _write_copilot_chat_publish_state,
            build_publish_plan,
        )

        manifest_id = "44444444-4444-4444-4444-444444444444"
        state_path = _copilot_chat_publish_state_path(tmp_path, "x")
        _write_copilot_chat_publish_state(
            state_path,
            manifest_id=manifest_id,
            app_version="1.1.4",
            hermes_home=tmp_path,
            tenant_id=_TEST_TENANT,
            profile="profile-a",
            cea_catalog_id_minted=True,
        )
        monkeypatch.setenv("HERMES_PROFILE", "profile-b")
        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": manifest_id,
                "version": "1.1.4",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, manifest_id="auto")
        )
        with pytest.raises(PublishError, match="belongs to Hermes profile"):
            apply_publish_plan(
                plan, mutator=self._scripted_zip_emit(zp), hermes_home=tmp_path
            )


class TestSecurityManifestArchives:
    def test_manifest_rewrite_refuses_symlink_archive(self, tmp_path) -> None:
        import zipfile

        from hermes_a365.publish import _rewrite_manifest_zip

        victim = tmp_path / "victim.zip"
        with zipfile.ZipFile(victim, "w") as zf:
            zf.writestr("manifest.json", b"{}")
        link = tmp_path / "manifest.zip"
        link.symlink_to(victim)

        with pytest.raises(OSError, match="non-symlink"):
            _rewrite_manifest_zip(link, b"{}")
        with zipfile.ZipFile(victim) as zf:
            assert zf.read("manifest.json") == b"{}"

    def test_manifest_rewrite_refuses_high_ratio_zip_bomb(self, tmp_path) -> None:
        import zipfile

        from hermes_a365.publish import _rewrite_manifest_zip

        archive = tmp_path / "manifest.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", b"{}")
            zf.writestr("payload.bin", b"A" * (1024 * 1024))

        with pytest.raises(ValueError, match="compression-ratio"):
            _rewrite_manifest_zip(archive, b"{}")

    def test_manifest_rewrite_refuses_traversal_member(self, tmp_path) -> None:
        import zipfile

        from hermes_a365.publish import _rewrite_manifest_zip

        archive = tmp_path / "manifest.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("manifest.json", b"{}")
            zf.writestr("../outside.txt", b"owned")

        with pytest.raises(ValueError, match="unsafe member path"):
            _rewrite_manifest_zip(archive, b"{}")
        assert not (tmp_path.parent / "outside.txt").exists()
