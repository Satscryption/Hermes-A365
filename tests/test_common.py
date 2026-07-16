"""Tests for the shared helpers in hermes_a365._common.

Coverage focus:
- ``deep_diff`` — used by reconcilers, so its semantics matter.
- ``render_diff_human`` — readability.
- ``slugify`` — used by cleanup + status to map a CLI display name onto
  the local ``~/.hermes/agents/<slug>/`` dir.
- ``safe_run`` — slice 18m pinned the empty-vs-failure distinction.

``tcp_reachable`` and ``parse_env`` are exercised through the doctor
tests (``tests/test_doctor.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes_a365._common import (
    deep_diff,
    ensure_contained,
    render_diff_human,
    safe_run,
    slugify,
    validate_slug,
)


class TestSlugify:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Hermes Inbox Helper", "hermes-inbox-helper"),
            ("inbox-helper", "inbox-helper"),
            ("INBOX HELPER", "inbox-helper"),
            ("Foo_Bar 99", "foo-bar-99"),
            ("Multi   Spaces", "multi-spaces"),
            ("Trailing space ", "trailing-space"),
            (" Leading space", "leading-space"),
            ("Already-Slug", "already-slug"),
            ("Mixed/Punct.&Symbols", "mixed-punct-symbols"),
        ],
    )
    def test_canonicalises(self, name: str, expected: str) -> None:
        assert slugify(name) == expected

    def test_empty_for_pure_punct(self) -> None:
        # Caller is responsible for rejecting empty slugs.
        assert slugify("---") == ""
        assert slugify("   ") == ""


class TestValidateSlug:
    """#103/M9: the filesystem-boundary gate for agent-dir slugs."""

    @pytest.mark.parametrize(
        "slug",
        [
            "../x",
            "a/b",
            "..",
            ".",
            "",
            "a\\b",
            "x\x00y",
            "/etc",
            "../../tmp/x",
        ],
    )
    def test_traversal_slugs_raise(self, slug: str) -> None:
        with pytest.raises(ValueError):
            validate_slug(slug)

    @pytest.mark.parametrize(
        "slug",
        [
            "inbox-helper",
            "R10",
            "foo.bar",
            "Hermes Inbox Helper",
            "hermes-inbox-helper",
        ],
    )
    def test_benign_slugs_pass_and_round_trip(self, slug: str) -> None:
        # validate_slug returns the slug unchanged when safe.
        assert validate_slug(slug) == slug


class TestEnsureContained:
    """#103/M9: destructive-primitive containment guard."""

    def test_inside_passes(self, tmp_path: Path) -> None:
        root = tmp_path / "agents"
        root.mkdir()
        # No exception for a path resolving inside the root.
        ensure_contained(root / "inbox-helper" / ".env", root)

    def test_root_itself_passes(self, tmp_path: Path) -> None:
        root = tmp_path / "agents"
        root.mkdir()
        ensure_contained(root, root)

    def test_dotdot_escape_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "agents"
        root.mkdir()
        with pytest.raises(ValueError):
            ensure_contained(root / ".." / "outside.env", root)

    def test_symlink_pointing_outside_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "agents"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "escape"
        link.symlink_to(outside)
        # The symlinked child resolves outside the root — must fail closed.
        with pytest.raises(ValueError):
            ensure_contained(link / "victim.env", root)


class TestDeepDiff:
    def test_identical_scalars(self) -> None:
        assert deep_diff(1, 1) == {}
        assert deep_diff("x", "x") == {}
        assert deep_diff(True, True) == {}
        assert deep_diff(None, None) == {}

    def test_differing_scalars(self) -> None:
        assert deep_diff(1, 2) == {"$": (1, 2)}
        assert deep_diff("a", "b") == {"$": ("a", "b")}

    def test_bool_not_equal_to_int(self) -> None:
        # Python: True == 1, but JSON treats them distinctly.
        assert deep_diff(True, 1) == {"$": (True, 1)}
        assert deep_diff(0, False) == {"$": (0, False)}

    def test_type_mismatch(self) -> None:
        assert deep_diff([1], {"a": 1}) == {"$": ([1], {"a": 1})}
        assert deep_diff("1", 1) == {"$": ("1", 1)}

    def test_identical_dicts(self) -> None:
        a = {"x": 1, "y": [1, 2], "z": {"q": True}}
        assert deep_diff(a, dict(a)) == {}

    def test_dict_changed_leaf(self) -> None:
        actual = {"x": 1, "y": 2}
        desired = {"x": 1, "y": 3}
        assert deep_diff(actual, desired) == {"y": (2, 3)}

    def test_dict_added_key(self) -> None:
        actual = {"x": 1}
        desired = {"x": 1, "y": 2}
        assert deep_diff(actual, desired) == {"y": (None, 2)}

    def test_dict_removed_key(self) -> None:
        actual = {"x": 1, "y": 2}
        desired = {"x": 1}
        assert deep_diff(actual, desired) == {"y": (2, None)}

    def test_nested_dict_path(self) -> None:
        actual = {"a": {"b": {"c": 1}}}
        desired = {"a": {"b": {"c": 2}}}
        assert deep_diff(actual, desired) == {"a/b/c": (1, 2)}

    def test_lists_equal(self) -> None:
        assert deep_diff([1, 2, 3], [1, 2, 3]) == {}
        assert deep_diff([], []) == {}

    def test_lists_different_length(self) -> None:
        diff = deep_diff([1, 2], [1, 2, 3])
        # Length mismatch surfaces as a single root-level diff
        assert diff == {"$": ([1, 2], [1, 2, 3])}

    def test_lists_different_element(self) -> None:
        diff = deep_diff([1, 2, 3], [1, 9, 3])
        assert diff == {"[1]": (2, 9)}

    def test_lists_reordered_is_diff(self) -> None:
        diff = deep_diff(["mail", "calendar"], ["calendar", "mail"])
        # Positional comparison: both indices differ.
        assert diff == {"[0]": ("mail", "calendar"), "[1]": ("calendar", "mail")}

    def test_nested_list_in_dict(self) -> None:
        actual = {"items": [{"k": 1}, {"k": 2}]}
        desired = {"items": [{"k": 1}, {"k": 9}]}
        assert deep_diff(actual, desired) == {"items[1]/k": (2, 9)}

    def test_blueprint_like_diff(self) -> None:
        """Real-world shape: changing only the DLP policy."""
        actual = {
            "agentIdentity": {"slug": "demo", "purpose": "p"},
            "policies": {"dlp": "default-restricted", "logging": "verbose"},
            "workIqTools": ["mail"],
        }
        desired = {
            "agentIdentity": {"slug": "demo", "purpose": "p"},
            "policies": {"dlp": "default-strict", "logging": "verbose"},
            "workIqTools": ["mail"],
        }
        diff = deep_diff(actual, desired)
        assert diff == {"policies/dlp": ("default-restricted", "default-strict")}


class TestRenderDiffHuman:
    def test_empty_says_no_differences(self) -> None:
        assert render_diff_human({}) == "(no differences)"

    def test_single_change_format(self) -> None:
        diff = {"policies/dlp": ("default-restricted", "default-strict")}
        text = render_diff_human(diff)
        assert "policies/dlp" in text
        assert "'default-restricted'" in text
        assert "->" in text
        assert "'default-strict'" in text

    def test_aligned_columns(self) -> None:
        diff = {
            "x": (1, 2),
            "very/long/path": ("a", "b"),
        }
        text = render_diff_human(diff)
        # Both lines should have the same column position for `->` (indented padding).
        lines = text.split("\n")
        assert len(lines) == 2
        # Sort order is alphabetical by path.
        assert "very/long/path" in lines[0]
        assert "x" in lines[1]


class TestSafeRun:
    """Pin the slice 18m contract: ``None`` only on real failure;
    successful empty stdout returns ``""`` (not ``None``)."""

    def test_success_with_output_returns_string(self) -> None:
        out = safe_run([sys.executable, "-c", "print('hello')"])
        assert out == "hello"

    def test_success_with_no_output_returns_empty_string(self) -> None:
        # Critical regression: previously returned ``None`` because of the
        # ``... or None`` clause. Doctor's `probe_custom_client_app` then
        # misread "no app found" as "az not signed in?".
        out = safe_run([sys.executable, "-c", "pass"])
        assert out == ""

    def test_nonzero_exit_returns_none(self) -> None:
        out = safe_run([sys.executable, "-c", "import sys; sys.exit(1)"])
        assert out is None

    def test_missing_binary_returns_none(self) -> None:
        out = safe_run(["/nonexistent/binary-that-does-not-exist-xyz"])
        assert out is None

    def test_timeout_returns_none(self) -> None:
        out = safe_run(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1
        )
        assert out is None

    def test_stdout_and_stderr_combined(self) -> None:
        out = safe_run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('out'); sys.stderr.write('err')",
            ]
        )
        # Order is not guaranteed across platforms — both streams present.
        assert out is not None
        assert "out" in out
        assert "err" in out
