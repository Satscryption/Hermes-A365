"""Tests for hermes_a365.emit_card and the adaptive-cards/ templates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_a365.emit_card import (
    ADAPTIVE_CARDS_SCHEMA,
    ADAPTIVE_CARDS_VERSION,
    ConfirmationInputs,
    ErrorInputs,
    GreetingInputs,
    emit_confirmation,
    emit_error,
    emit_greeting,
    emit_to_json,
    main,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "adaptive-cards"


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


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


# ---------------------------------------------------------------------------
# Envelope invariants — every card must satisfy these
# ---------------------------------------------------------------------------


class TestEnvelope:
    @pytest.mark.parametrize(
        "payload",
        [
            emit_greeting(GreetingInputs()),
            emit_confirmation(ConfirmationInputs(action="x")),
            emit_error(ErrorInputs(heading="x", message="y")),
        ],
    )
    def test_envelope_shape(self, payload: dict[str, object]) -> None:
        assert payload["type"] == "AdaptiveCard"
        assert payload["$schema"] == ADAPTIVE_CARDS_SCHEMA
        assert payload["version"] == ADAPTIVE_CARDS_VERSION
        assert isinstance(payload["body"], list)
        assert len(payload["body"]) >= 1

    @pytest.mark.parametrize(
        "payload",
        [
            emit_greeting(GreetingInputs(commands=("a", "b"))),
            emit_confirmation(
                ConfirmationInputs(
                    action="x",
                    message="m",
                    facts=(("k", "v"),),
                )
            ),
            emit_error(ErrorInputs(heading="x", message="y", detail="d")),
        ],
    )
    def test_round_trip_json(self, payload: dict[str, object]) -> None:
        # Re-parsing the canonical render must succeed.
        text = emit_to_json(payload)
        round_tripped = json.loads(text)
        assert round_tripped == payload


# ---------------------------------------------------------------------------
# Greeting
# ---------------------------------------------------------------------------


class TestEmitGreeting:
    def test_minimal_golden(self, update_golden: bool) -> None:
        text = emit_to_json(emit_greeting(GreetingInputs()))
        _check_golden("greeting_minimal.json", text, update=update_golden)

    def test_with_commands_golden(self, update_golden: bool) -> None:
        inputs = GreetingInputs(
            heading="Hermes here",
            subtitle="Pick a quick action.",
            commands=("Summarise unread mail", "List today's events"),
        )
        text = emit_to_json(emit_greeting(inputs))
        _check_golden("greeting_with_commands.json", text, update=update_golden)

    def test_no_commands_means_two_textblocks(self) -> None:
        payload = emit_greeting(GreetingInputs())
        body = payload["body"]
        assert len(body) == 2  # heading + subtitle, no "Try one of:" header
        assert body[0]["text"] == GreetingInputs.heading
        assert body[1]["text"] == GreetingInputs.subtitle

    def test_with_commands_includes_bullet_lines(self) -> None:
        payload = emit_greeting(GreetingInputs(commands=("alpha", "beta")))
        body = payload["body"]
        # heading + subtitle + "Try one of:" + 2 commands = 5
        assert len(body) == 5
        assert body[2]["text"] == "Try one of:"
        assert body[3]["text"] == "• alpha"
        assert body[4]["text"] == "• beta"

    def test_quotes_in_inputs_are_escaped(self) -> None:
        # Bytes that would break naive interpolation.
        inputs = GreetingInputs(heading='He said "hi"', subtitle="path/to\\file")
        payload = emit_greeting(inputs)
        # Round-trip-safe — emit_greeting parses the rendered template.
        assert payload["body"][0]["text"] == 'He said "hi"'
        assert payload["body"][1]["text"] == "path/to\\file"


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


class TestEmitConfirmation:
    def test_action_only_golden(self, update_golden: bool) -> None:
        text = emit_to_json(emit_confirmation(ConfirmationInputs(action="Reply sent")))
        _check_golden("confirmation_action_only.json", text, update=update_golden)

    def test_with_facts_golden(self, update_golden: bool) -> None:
        inputs = ConfirmationInputs(
            action="Reply sent",
            message="Your reply has been delivered.",
            facts=(
                ("Recipient", "team@contoso.com"),
                ("Subject", "Q2 plan"),
            ),
        )
        text = emit_to_json(emit_confirmation(inputs))
        _check_golden("confirmation_with_facts.json", text, update=update_golden)

    def test_action_only_has_color_good(self) -> None:
        payload = emit_confirmation(ConfirmationInputs(action="x"))
        assert payload["body"][0]["color"] == "Good"

    def test_facts_render_as_factset(self) -> None:
        payload = emit_confirmation(
            ConfirmationInputs(
                action="x",
                facts=(("k1", "v1"), ("k2", "v2")),
            )
        )
        # Last body block should be the FactSet
        last = payload["body"][-1]
        assert last["type"] == "FactSet"
        assert last["facts"] == [
            {"title": "k1", "value": "v1"},
            {"title": "k2", "value": "v2"},
        ]

    def test_facts_preserve_order(self) -> None:
        # Insertion order matters — Adaptive Cards display facts in given order.
        inputs = ConfirmationInputs(
            action="x",
            facts=(("zebra", "1"), ("alpha", "2"), ("mike", "3")),
        )
        payload = emit_confirmation(inputs)
        titles = [f["title"] for f in payload["body"][-1]["facts"]]
        assert titles == ["zebra", "alpha", "mike"]


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestEmitError:
    def test_minimal_golden(self, update_golden: bool) -> None:
        text = emit_to_json(
            emit_error(ErrorInputs(heading="FIC expired", message="Token is past expiry."))
        )
        _check_golden("error_minimal.json", text, update=update_golden)

    def test_with_detail_golden(self, update_golden: bool) -> None:
        inputs = ErrorInputs(
            heading="FIC expired",
            message="Token is past expiry.",
            detail="AADSTS70043: refresh token has expired.",
        )
        text = emit_to_json(emit_error(inputs))
        _check_golden("error_with_detail.json", text, update=update_golden)

    def test_color_attention(self) -> None:
        payload = emit_error(ErrorInputs(heading="x", message="y"))
        assert payload["body"][0]["color"] == "Attention"

    def test_no_detail_means_two_blocks(self) -> None:
        payload = emit_error(ErrorInputs(heading="x", message="y"))
        assert len(payload["body"]) == 2

    def test_detail_appends_subtle_block(self) -> None:
        payload = emit_error(ErrorInputs(heading="x", message="y", detail="d"))
        assert len(payload["body"]) == 3
        assert payload["body"][2]["text"] == "d"
        assert payload["body"][2]["isSubtle"] is True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_greeting_no_args(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["greeting"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["type"] == "AdaptiveCard"
        assert len(payload["body"]) == 2

    def test_greeting_with_commands(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["greeting", "--command", "x", "--command", "y"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        # heading + subtitle + try-one-of + 2 commands = 5
        assert len(payload["body"]) == 5

    def test_confirmation_with_fact(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "confirmation",
                "--action",
                "Sent",
                "--fact",
                "k=v",
                "--fact",
                "n=42",
            ]
        )
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        facts = payload["body"][-1]["facts"]
        assert facts == [
            {"title": "k", "value": "v"},
            {"title": "n", "value": "42"},
        ]

    def test_confirmation_bad_fact(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["confirmation", "--action", "x", "--fact", "no-equals"])
        assert rc == 2
        assert "must be key=value" in capsys.readouterr().err

    def test_error_minimal(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["error", "--heading", "h", "--message", "m"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["body"][0]["color"] == "Attention"
