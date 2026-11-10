"""Regression checks for Path B quickstart --confirm-bot-target examples (#309 S01)."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
QUICKSTART_PATH = REPO_ROOT / "docs" / "quickstart-path-b.md"

ALLOWED_CHANGESET_PATHS = frozenset(
    {
        "docs/quickstart-path-b.md",
        "tests/test_quickstart_path_b_confirm_bot_target.py",
    }
)

PINNED_ACTIVITY_BRIDGE_COMMAND = (
    "hermes a365 activity-bridge update-endpoint \\\n"
    '  --agent-name "<display-name>" \\\n'
    '  --url "<new-tunnel-url>/api/messages" \\\n'
    "  --apply"
)

FENCE_PATTERN = re.compile(r"^```[\w-]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def read_quickstart() -> str:
    return QUICKSTART_PATH.read_text(encoding="utf-8")


def _is_comment_only_line(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def parse_complete_commands(markdown: str) -> list[str]:
    commands: list[str] = []
    for match in FENCE_PATTERN.finditer(markdown):
        block = match.group(1)
        current_parts: list[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if _is_comment_only_line(line):
                if current_parts:
                    commands.append(" ".join(current_parts))
                    current_parts = []
                continue
            stripped = line.strip()
            if stripped.endswith("\\"):
                current_parts.append(stripped[:-1].strip())
            else:
                current_parts.append(stripped)
                commands.append(" ".join(current_parts))
                current_parts = []
        if current_parts:
            commands.append(" ".join(current_parts))
    return commands


def find_apply_command(commands: list[str], *tokens: str) -> str | None:
    token_set = set(tokens)
    for command in commands:
        if "--apply" not in command:
            continue
        parts = shlex.split(command)
        if token_set.issubset(parts):
            return command
    return None


def option_values(command: str) -> dict[str, list[str]]:
    parts = shlex.split(command)
    values: dict[str, list[str]] = {}
    i = 0
    while i < len(parts):
        token = parts[i]
        if token.startswith("--") and "=" in token:
            name, value = token.split("=", 1)
            values.setdefault(name, []).append(value)
            i += 1
            continue
        if token.startswith("--"):
            if i + 1 < len(parts) and not parts[i + 1].startswith("--"):
                values.setdefault(token, []).append(parts[i + 1])
                i += 2
                continue
            values.setdefault(token, []).append("")
            i += 1
            continue
        i += 1
    return values


def _section_endpoint_changes(markdown: str) -> str:
    marker = "## Endpoint changes; stop or clean up (C2.2)"
    start = markdown.find(marker)
    assert start != -1, "Endpoint changes section missing"
    return markdown[start:]


def _assert_bot_service_apply_commands(markdown: str) -> tuple[str, str]:
    commands = parse_complete_commands(markdown)
    update_cmd = find_apply_command(
        commands, "hermes", "a365", "bot-service", "update-endpoint"
    )
    cleanup_cmd = find_apply_command(
        commands, "hermes", "a365", "bot-service", "cleanup"
    )
    assert update_cmd is not None, "apply-mode bot-service update-endpoint command missing"
    assert cleanup_cmd is not None, "apply-mode bot-service cleanup command missing"

    update_opts = option_values(update_cmd)
    cleanup_opts = option_values(cleanup_cmd)

    assert update_opts.get("--confirm-bot-target") == ["<arm-resource-id>"]
    assert cleanup_opts.get("--confirm-bot-target") == ["<arm-resource-id>"]
    assert cleanup_opts.get("--confirm") == ["<display-name>"]

    assert "--confirm-legacy-binding" not in update_cmd
    assert "--confirm-legacy-binding" not in cleanup_cmd

    activity_cmd = find_apply_command(
        commands, "hermes", "a365", "activity-bridge", "update-endpoint"
    )
    if activity_cmd:
        assert "--confirm-bot-target" not in activity_cmd

    return update_cmd, cleanup_cmd


def test_t001_bot_service_apply_commands_have_exact_target_flag() -> None:
    _assert_bot_service_apply_commands(read_quickstart())


def test_t001_fails_if_target_flag_removed_from_update_endpoint() -> None:
    md = read_quickstart()
    mutated = md.replace(
        "  --confirm-bot-target <arm-resource-id> \\\n  --apply\n\n# Optional",
        "  --apply\n\n# Optional",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_bot_service_apply_commands(mutated)


def test_t001_fails_if_target_flag_removed_from_cleanup() -> None:
    md = read_quickstart()
    mutated = md.replace(
        '  --confirm "<display-name>" \\\n  --confirm-bot-target <arm-resource-id>',
        '  --confirm "<display-name>"',
        1,
    )
    with pytest.raises(AssertionError):
        _assert_bot_service_apply_commands(mutated)


def test_t001_fails_if_expected_apply_command_removed() -> None:
    md = read_quickstart()
    mutated = re.sub(
        r"hermes a365 bot-service cleanup \\\n.*?\n```",
        "```",
        md,
        count=1,
        flags=re.DOTALL,
    )
    with pytest.raises(AssertionError):
        _assert_bot_service_apply_commands(mutated)


def test_t001_fails_if_target_flag_moved_onto_activity_bridge() -> None:
    md = read_quickstart()
    mutated = md.replace(
        "  --confirm-bot-target <arm-resource-id> \\\n  --apply\n\n# Optional",
        "  --apply\n\n# Optional",
        1,
    )
    mutated = mutated.replace(
        "hermes a365 activity-bridge update-endpoint \\\n"
        '  --agent-name "<display-name>" \\\n'
        '  --url "<new-tunnel-url>/api/messages" \\\n'
        "  --apply",
        "hermes a365 activity-bridge update-endpoint \\\n"
        '  --agent-name "<display-name>" \\\n'
        '  --url "<new-tunnel-url>/api/messages" \\\n'
        "  --confirm-bot-target <arm-resource-id> \\\n"
        "  --apply",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_bot_service_apply_commands(mutated)


def test_t002_placeholder_table_defines_arm_resource_id() -> None:
    md = read_quickstart()
    assert "<arm-resource-id>" in md
    assert "Microsoft.BotService/botServices" in md


def test_t003_adjacent_prose_covers_match_dry_run_and_non_substitution() -> None:
    section = _section_endpoint_changes(read_quickstart())
    prose = section.split("```", 1)[0]
    lower = prose.lower()
    assert "exactly match" in lower
    assert "--confirm-bot-target" in prose
    assert "without" in lower and "--apply" in prose
    assert "re-run with" in lower
    assert "does not replace" in lower or "does not substitute" in lower
    assert '--confirm "<display-name>"' in prose or "--confirm" in prose


def test_t004_activity_bridge_command_unchanged_and_without_target_flag() -> None:
    commands = parse_complete_commands(read_quickstart())
    activity_cmd = find_apply_command(
        commands, "hermes", "a365", "activity-bridge", "update-endpoint"
    )
    assert activity_cmd is not None
    assert "--confirm-bot-target" not in activity_cmd
    pinned_single_line = re.sub(r"\\\s*\n\s*", " ", PINNED_ACTIVITY_BRIDGE_COMMAND)
    assert shlex.split(activity_cmd) == shlex.split(pinned_single_line)


def _union_changeset_paths(base: str) -> set[str]:
    paths: set[str] = set()
    for args in (
        ["git", "diff", "--name-only", base, "HEAD"],
        ["git", "diff", "--name-only", base],
    ):
        proc = subprocess.run(
            args,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        for line in proc.stdout.splitlines():
            if line.strip():
                paths.add(line.strip())
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    for line in proc.stdout.splitlines():
        if line.strip():
            paths.add(line.strip())
    return paths


def _assert_changeset_allowlist(paths: set[str]) -> None:
    disallowed = sorted(p for p in paths if p not in ALLOWED_CHANGESET_PATHS)
    assert not disallowed, f"Disallowed paths in change set: {disallowed}"


def test_t005_changeset_touches_only_allowed_paths() -> None:
    base = os.environ.get("HERMES_A365_CHANGESET_BASE")
    if not base:
        pytest.skip(
            "HERMES_A365_CHANGESET_BASE is unset; set it to the implementation "
            "starting commit SHA to run the change-set allowlist gate."
        )
    paths = _union_changeset_paths(base)
    _assert_changeset_allowlist(paths)

    # Negative: untracked disallowed path must fail the same check.
    rogue = REPO_ROOT / "src" / "_t005_rogue_untracked_marker.py"
    rogue.write_text("# temporary\n", encoding="utf-8")
    try:
        paths_with_rogue = _union_changeset_paths(base)
        assert str(rogue.relative_to(REPO_ROOT)).replace("\\", "/") in paths_with_rogue
        with pytest.raises(AssertionError):
            _assert_changeset_allowlist(paths_with_rogue)
    finally:
        rogue.unlink(missing_ok=True)

    # Allowed new test module is accepted even when untracked (already tracked here).
    assert "tests/test_quickstart_path_b_confirm_bot_target.py" in ALLOWED_CHANGESET_PATHS
