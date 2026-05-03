"""Tests for the shared helpers in scripts/_common.py.

Coverage focus:
- ``deep_diff`` — used by reconcilers, so its semantics matter.
- ``render_diff_human`` — readability.

The other helpers (``safe_run``, ``tcp_reachable``, ``parse_env``) are
exercised through the doctor tests (``tests/test_doctor.py``).
"""

from __future__ import annotations

from _common import deep_diff, render_diff_human


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
