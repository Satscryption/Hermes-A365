from __future__ import annotations

import json
from pathlib import Path

import pytest
from render_blueprint import BlueprintInputs, render_blueprint_json

GOLDEN_DIR = Path(__file__).parent / "golden" / "blueprint"


def _check_golden(name: str, actual: str, *, update: bool) -> None:
    path = GOLDEN_DIR / name
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    expected = path.read_text()
    assert actual == expected, (
        f"golden mismatch: {name}\n--- expected ---\n{expected}\n--- actual ---\n{actual}"
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


def test_render_blueprint_minimal(update_golden: bool) -> None:
    inputs = BlueprintInputs(
        slug="inbox-helper",
        description="Summarises unread mail and surfaces commitments",
        purpose="productivity",
    )
    actual = render_blueprint_json(inputs)
    _check_golden("minimal.json", actual, update=update_golden)


def test_render_blueprint_with_workiq(update_golden: bool) -> None:
    inputs = BlueprintInputs(
        slug="inbox-helper",
        description="Summarises unread mail and surfaces commitments",
        purpose="productivity",
        workiq_tools=["mail", "calendar"],
        functions=["summarise-mail", "list-events"],
    )
    actual = render_blueprint_json(inputs)
    _check_golden("with_workiq.json", actual, update=update_golden)


def test_render_blueprint_default_display_name() -> None:
    inputs = BlueprintInputs(slug="x", description="d", purpose="p")
    payload = json.loads(render_blueprint_json(inputs))
    assert payload["displayName"] == "x"


def test_render_blueprint_explicit_display_name() -> None:
    inputs = BlueprintInputs(slug="x", description="d", purpose="p", display_name="Pretty")
    payload = json.loads(render_blueprint_json(inputs))
    assert payload["displayName"] == "Pretty"


def test_render_blueprint_rejects_unknown_workiq() -> None:
    with pytest.raises(ValueError, match="unknown workiq tools"):
        BlueprintInputs(
            slug="x",
            description="d",
            purpose="p",
            workiq_tools=["mail", "bogus"],
        )


def test_render_blueprint_output_is_canonical_json() -> None:
    """Re-parsing the output must yield identical structure (round-trip safe)."""
    inputs = BlueprintInputs(
        slug="round-trip",
        description="d",
        purpose="p",
        workiq_tools=["mail"],
    )
    text = render_blueprint_json(inputs)
    parsed = json.loads(text)
    assert parsed["agentIdentity"]["slug"] == "round-trip"
    assert parsed["workIqTools"] == ["mail"]
    # Stable when re-rendered
    assert render_blueprint_json(inputs) == text
