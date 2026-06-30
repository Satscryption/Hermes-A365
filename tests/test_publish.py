"""Tests for hermes_a365.publish — wraps `a365 publish`."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from hermes_a365.mutator import AADSTSError, CliInvocationError, RunResult
from hermes_a365.publish import (
    _PUBLISH_APPLY_TIMEOUT_SECONDS,
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

    def test_manifest_id_requires_copilot_chat(self) -> None:
        with pytest.raises(ValueError, match="--manifest-id"):
            PublishInputs(agent_name="x", manifest_id="auto")

    def test_manifest_id_guid_validated(self) -> None:
        with pytest.raises(ValueError, match="GUID"):
            PublishInputs(agent_name="x", copilot_chat=True, manifest_id="not-a-guid")

    def test_manifest_id_auto_allowed_with_copilot_chat(self) -> None:
        inp = PublishInputs(agent_name="x", copilot_chat=True, manifest_id="auto")
        assert inp.manifest_id == "auto"


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
        assert mutator.call_timeouts == [_PUBLISH_APPLY_TIMEOUT_SECONDS]

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

    def test_prompt_starters_require_copilot_chat(self) -> None:
        with pytest.raises(ValueError, match="--prompt-starter"):
            PublishInputs(agent_name="x", prompt_starters=(("t", "p"),))

    def test_prompt_starters_allowed_with_copilot_chat(self) -> None:
        inp = PublishInputs(
            agent_name="x", copilot_chat=True, prompt_starters=(("t", "p"),)
        )
        assert inp.prompt_starters == (("t", "p"),)


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


class TestTransformManifestToCopilotChat:
    """Pure-function tests for the manifest transform."""

    def test_bumps_manifest_version_to_1_21(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {"manifestVersion": "devPreview"}, bot_id="bid"
        )
        assert out["manifestVersion"] == "1.21"

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
                "supportsFiles": False,
                "isNotificationOnly": False,
                "commandLists": [
                    {
                        # #74: command-list scopes track the bot scopes.
                        "scopes": ["personal"],
                        "commands": [
                            {
                                "title": "How can you help me?",
                                "description": "How can you help me?",
                                "type": "prompt",
                                "prompt": "How can you help me?",
                            }
                        ],
                    }
                ],
            }
        ]

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

    def test_preserves_unrelated_fields(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        src = {
            "name": {"short": "X", "full": "X agent"},
            "description": {"short": "d", "full": "dd"},
            "icons": {"color": "icon.png", "outline": "icon-out.png"},
            "developer": {"name": "Acme"},
            "accentColor": "#0078D4",
        }
        out = _transform_manifest_to_copilot_chat(src, bot_id="bid")
        assert out["name"] == src["name"]
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

    # ── #74: prompt starters ──────────────────────────────────────────

    @staticmethod
    def _commands(out: dict) -> list[dict]:
        return out["bots"][0]["commandLists"][0]["commands"]

    def test_default_starters_are_prompt_type(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat({}, bot_id="bid")
        commands = self._commands(out)
        assert commands  # never empty
        for cmd in commands:
            assert cmd["type"] == "prompt"
            assert cmd["prompt"]  # non-empty

    def test_default_starters_derive_from_name(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {"name": {"short": "Hermes Inbox Helper"}}, bot_id="bid"
        )
        prompts = " ".join(c["prompt"] for c in self._commands(out))
        assert "Hermes Inbox Helper" in prompts

    def test_operator_starters_emitted_verbatim(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {},
            bot_id="bid",
            prompt_starters=(("Draft a reply", "Draft a reply to the latest email"),),
        )
        assert self._commands(out) == [
            {
                "title": "Draft a reply",
                "description": "Draft a reply",
                "type": "prompt",
                "prompt": "Draft a reply to the latest email",
            }
        ]

    def test_command_list_scopes_track_bot_scopes(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {}, bot_id="bid", scopes=("copilot", "personal", "team")
        )
        # The consistency fix: team is no longer dropped from the list.
        assert out["bots"][0]["commandLists"][0]["scopes"] == [
            "copilot",
            "personal",
            "team",
        ]

    def test_caps_to_ten_commands(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        starters = tuple((f"t{i}", f"prompt {i}") for i in range(15))
        out = _transform_manifest_to_copilot_chat(
            {}, bot_id="bid", prompt_starters=starters
        )
        assert len(self._commands(out)) == 10

    def test_prompt_truncated_to_4000_chars(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {}, bot_id="bid", prompt_starters=(("t", "x" * 5000),)
        )
        assert len(self._commands(out)[0]["prompt"]) == 4000

    def test_empty_prompt_dropped_then_falls_back(self) -> None:
        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        # A starter with a blank prompt is dropped; never emit empty commands.
        out = _transform_manifest_to_copilot_chat(
            {}, bot_id="bid", prompt_starters=(("t", "   "),)
        )
        commands = self._commands(out)
        assert len(commands) == 1
        assert commands[0]["prompt"] == "How can you help me?"

    def test_prompt_with_quote_round_trips_through_zip(self) -> None:
        import json

        from hermes_a365.publish import _transform_manifest_to_copilot_chat

        out = _transform_manifest_to_copilot_chat(
            {}, bot_id="bid", prompt_starters=(('Say "hi"', 'Reply with "hello"'),)
        )
        # json round-trip is lossless for embedded quotes.
        assert json.loads(json.dumps(out)) == out
        assert self._commands(out)[0]["prompt"] == 'Reply with "hello"'


class TestParsePromptStarters:
    def test_well_formed_pair(self) -> None:
        from hermes_a365.publish import _parse_prompt_starters

        assert _parse_prompt_starters(["title=Foo,prompt=Bar"]) == (("Foo", "Bar"),)

    def test_title_may_contain_commas(self) -> None:
        from hermes_a365.publish import _parse_prompt_starters

        assert _parse_prompt_starters(["title=Hi, there,prompt=Say hi"]) == (
            ("Hi, there", "Say hi"),
        )

    def test_empty_list_yields_empty_tuple(self) -> None:
        from hermes_a365.publish import _parse_prompt_starters

        assert _parse_prompt_starters([]) == ()
        assert _parse_prompt_starters(None) == ()

    def test_missing_prompt_raises(self) -> None:
        from hermes_a365.publish import _parse_prompt_starters

        with pytest.raises(ValueError, match="prompt-starter must be"):
            _parse_prompt_starters(["title=Foo"])

    def test_missing_title_prefix_raises(self) -> None:
        from hermes_a365.publish import _parse_prompt_starters

        with pytest.raises(ValueError, match="start with 'title='"):
            _parse_prompt_starters(["Foo,prompt=Bar"])

    def test_empty_prompt_raises(self) -> None:
        from hermes_a365.publish import _parse_prompt_starters

        with pytest.raises(ValueError, match="empty prompt"):
            _parse_prompt_starters(["title=Foo,prompt="])

    def test_cli_flag_appends_and_defaults_empty(self) -> None:
        from hermes_a365.publish import build_parser

        ns = build_parser().parse_args(["--agent-name", "x"])
        assert ns.prompt_starter == []
        ns2 = build_parser().parse_args(
            ["--agent-name", "x", "--prompt-starter", "title=A,prompt=B"]
        )
        assert ns2.prompt_starter == ["title=A,prompt=B"]


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
        assert summary["manifest_version"] == "1.21"
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
            assert m["manifestVersion"] == "1.21"
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

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "manifestVersion": "devPreview",
                "webApplicationInfo": {"id": "the-bot"},
                "agenticUserTemplates": [{"a": 1}],
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        result = apply_publish_plan(plan, mutator=self._scripted_zip_emit(zp))

        # Copilot Chat-only mode: no AI Teammate package, single zip
        # transformed in place.
        assert result.package_path is None
        assert result.copilot_chat_package_path == str(zp)
        assert result.copilot_chat_bot_id == "the-bot"
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["manifestVersion"] == "1.21"
            assert m["bots"][0]["botId"] == "the-bot"
            assert "agenticUserTemplates" not in m
        # Operator-facing message points at the MAC Agents upload path.
        assert any("Microsoft Admin Portal" in msg for msg in result.messages)
        assert not any("AI Teammate package" in msg for msg in result.messages)

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

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        d = self._seed_manifest_dir(
            tmp_path,
            {
                "manifestVersion": "devPreview",
                "id": "2e5e2dea-blueprint",
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
            plan, mutator=self._scripted_template_extract(str(d))
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
            assert m["manifestVersion"] == "1.21"
            assert m["bots"][0]["botId"] == "1c2b61bc-fa6a-4c7b-9656-a82b662dacfe"
            assert m["copilotAgents"]["customEngineAgents"] == [
                {"type": "bot", "id": "1c2b61bc-fa6a-4c7b-9656-a82b662dacfe"}
            ]
            assert m["id"] != "2e5e2dea-blueprint"  # fresh manifest-id (auto)
            # name.short truncated 32 -> <= 30 at a word boundary.
            assert m["name"]["short"] == "Hermes Inbox Helper R8"

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

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "manifestVersion": "devPreview",
                "id": "blueprint-catalog-id",
                "webApplicationInfo": {"id": "the-bot"},
                "agenticUserTemplates": [{"a": 1}],
                "name": {"short": "Hermes Inbox Helper R8", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", aiteammate=True, copilot_chat=True)
        )
        result = apply_publish_plan(plan, mutator=self._scripted_zip_emit(zp))

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
            assert m["id"] == "blueprint-catalog-id"
            assert "agenticUserTemplates" in m

        # Copilot Chat sibling is in 1.21 shape.
        with zipfile.ZipFile(result.copilot_chat_package_path) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["manifestVersion"] == "1.21"
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

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": "blueprint-catalog-id",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(
            PublishInputs(
                agent_name="X",
                copilot_chat=True,
                manifest_id="11111111-1111-1111-1111-111111111111",
            )
        )
        result = apply_publish_plan(plan, mutator=self._scripted_zip_emit(zp))

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

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": "blueprint-catalog-id",
                "webApplicationInfo": {"id": "the-bot"},
                "name": {"short": "X", "full": "X"},
            },
        )
        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        result = apply_publish_plan(plan, mutator=self._scripted_zip_emit(zp))

        assert result.copilot_chat_manifest_id == "blueprint-catalog-id"
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["id"] == "blueprint-catalog-id"
            assert m["name"]["short"] == "X"

    def test_copilot_chat_zip_omits_ai_teammate_template_sidecar(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "id": "blueprint-catalog-id",
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
        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        result = apply_publish_plan(plan, mutator=self._scripted_zip_emit(zp))

        with zipfile.ZipFile(result.copilot_chat_package_path) as zf:
            assert "agenticUserTemplateManifest.json" not in zf.namelist()
            assert "color.png" in zf.namelist()
            m = json.loads(zf.read("manifest.json"))
            assert "agenticUserTemplates" not in m

    def test_bot_id_override_used(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {"webApplicationInfo": {"id": "auto-id"}, "name": {"short": "X", "full": "X"}},
        )
        plan = build_publish_plan(
            PublishInputs(agent_name="X", copilot_chat=True, bot_id="override-id")
        )
        result = apply_publish_plan(plan, mutator=self._scripted_zip_emit(zp))

        assert result.copilot_chat_bot_id == "override-id"
        with zipfile.ZipFile(zp) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["bots"][0]["botId"] == "override-id"

    def test_name_short_truncated_on_copilot_chat_zip(self, tmp_path):
        import json
        import zipfile

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        zp = self._seed_manifest_zip(
            tmp_path,
            {
                "webApplicationInfo": {"id": "bid"},
                "name": {
                    "short": "Hermes Inbox Helper R8 Blueprint",  # 32 chars
                    "full": "Hermes Inbox Helper R8 Blueprint",
                },
            },
        )
        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        result = apply_publish_plan(plan, mutator=self._scripted_zip_emit(zp))

        # name.short truncated on the Copilot Chat zip (the only zip kept).
        with zipfile.ZipFile(result.copilot_chat_package_path) as zf:
            m = json.loads(zf.read("manifest.json"))
            assert m["name"]["short"] == "Hermes Inbox Helper R8"
        assert any("truncated name.short" in msg for msg in result.messages)

    def test_transform_failure_surfaces_warning(self, tmp_path):
        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        # Manifest with no resolvable bot id and no override.
        zp = self._seed_manifest_zip(tmp_path, {"name": {"short": "X", "full": "X"}})
        plan = build_publish_plan(PublishInputs(agent_name="X", copilot_chat=True))
        result = apply_publish_plan(plan, mutator=self._scripted_zip_emit(zp))

        assert result.copilot_chat_package_path is None
        assert any(
            "Copilot Chat transform failed" in msg for msg in result.messages
        )

    def test_transform_failure_rolls_back_sibling_in_both_mode(self, tmp_path):
        from pathlib import Path

        from hermes_a365.publish import apply_publish_plan, build_publish_plan

        zp = self._seed_manifest_zip(tmp_path, {"name": {"short": "X", "full": "X"}})
        plan = build_publish_plan(
            PublishInputs(agent_name="X", aiteammate=True, copilot_chat=True)
        )
        result = apply_publish_plan(plan, mutator=self._scripted_zip_emit(zp))

        assert result.copilot_chat_package_path is None
        # AI Teammate zip still present (the seeded zip).
        assert Path(zp).is_file()
        # Sibling was rolled back.
        sibling = Path(zp).with_name(Path(zp).stem + ".copilot-chat.zip")
        assert not sibling.exists()
