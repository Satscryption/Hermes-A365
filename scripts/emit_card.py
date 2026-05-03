"""scripts/emit_card.py — build Adaptive Card v1.6 payloads.

Spec: SPEC.md §6.7. Renders one of three Adaptive Card templates
(``greeting`` / ``confirmation`` / ``error``) for the activity bridge to post
back to A365 channels (Teams, Outlook, M365 Copilot) when responding to
``message`` or ``invoke`` (``adaptiveCard/action``) activities.

Each template lives in ``templates/adaptive-cards/<kind>.json.j2``; the body
shape is inline so operators can tweak the card layout without touching
Python. Builders here are typed dataclasses so the activity bridge gets
compile-time-ish validation of card inputs.

Programmatic use::

    from emit_card import (
        GreetingInputs, ConfirmationInputs, ErrorInputs,
        emit_greeting, emit_confirmation, emit_error,
    )
    payload = emit_greeting(GreetingInputs(commands=("summarise mail",)))

CLI use::

    python scripts/emit_card.py greeting --heading "Hi" --command "summarise mail"
    python scripts/emit_card.py confirmation --action "Reply sent" --fact "thread=42"
    python scripts/emit_card.py error --heading "FIC expired" --message "..."
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from _common import jinja_env

# Card envelope constants — surfaced for activity-bridge tests.
ADAPTIVE_CARDS_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
ADAPTIVE_CARDS_VERSION = "1.6"


# ---------------------------------------------------------------------------
# Inputs (typed dataclasses)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GreetingInputs:
    heading: str = "Hermes agent ready"
    subtitle: str = "Ask me about your inbox, calendar, or M365 data."
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfirmationInputs:
    action: str  # required short verb phrase, e.g. "Reply sent"
    message: str = ""
    facts: tuple[tuple[str, str], ...] = ()  # ordered key/value pairs


@dataclass(frozen=True)
class ErrorInputs:
    heading: str
    message: str
    detail: str | None = None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(template_name: str, **vars: Any) -> dict[str, Any]:
    """Render a card template and parse the result; raise on invalid JSON."""
    env = jinja_env()
    template = env.get_template(f"adaptive-cards/{template_name}")
    text = template.render(**vars)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"adaptive-cards/{template_name} produced invalid JSON: {e}\n"
            f"--- rendered output ---\n{text}\n--- end ---"
        ) from e


def emit_greeting(inputs: GreetingInputs) -> dict[str, Any]:
    return _render(
        "greeting.json.j2",
        heading=inputs.heading,
        subtitle=inputs.subtitle,
        commands=list(inputs.commands),
    )


def emit_confirmation(inputs: ConfirmationInputs) -> dict[str, Any]:
    return _render(
        "confirmation.json.j2",
        action=inputs.action,
        message=inputs.message,
        # Convert ordered tuple-of-pairs to dict; Python preserves insertion order.
        facts=dict(inputs.facts),
    )


def emit_error(inputs: ErrorInputs) -> dict[str, Any]:
    return _render(
        "error.json.j2",
        heading=inputs.heading,
        message=inputs.message,
        detail=inputs.detail,
    )


def emit_to_json(payload: dict[str, Any], *, indent: int = 2) -> str:
    """Canonicalise a card payload as sorted JSON with a trailing newline."""
    return json.dumps(payload, indent=indent, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_facts(raw: list[str]) -> tuple[tuple[str, str], ...]:
    facts: list[tuple[str, str]] = []
    for r in raw:
        if "=" not in r:
            raise ValueError(f"--fact must be key=value, got {r!r}")
        k, _, v = r.partition("=")
        facts.append((k.strip(), v.strip()))
    return tuple(facts)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit an Adaptive Card v1.6 payload to stdout.",
    )
    sub = parser.add_subparsers(dest="kind", required=True)

    p_greeting = sub.add_parser("greeting", help="emit a greeting card")
    p_greeting.add_argument("--heading", default=GreetingInputs.heading)
    p_greeting.add_argument("--subtitle", default=GreetingInputs.subtitle)
    p_greeting.add_argument(
        "--command",
        action="append",
        default=[],
        help="add a quick-action command (repeatable)",
    )

    p_confirm = sub.add_parser("confirmation", help="emit a confirmation card")
    p_confirm.add_argument("--action", required=True)
    p_confirm.add_argument("--message", default="")
    p_confirm.add_argument(
        "--fact",
        action="append",
        default=[],
        help="add a key=value fact (repeatable)",
    )

    p_error = sub.add_parser("error", help="emit an error card")
    p_error.add_argument("--heading", required=True)
    p_error.add_argument("--message", required=True)
    p_error.add_argument("--detail")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.kind == "greeting":
            payload = emit_greeting(
                GreetingInputs(
                    heading=args.heading,
                    subtitle=args.subtitle,
                    commands=tuple(args.command),
                )
            )
        elif args.kind == "confirmation":
            facts = _parse_facts(args.fact)
            payload = emit_confirmation(
                ConfirmationInputs(
                    action=args.action,
                    message=args.message,
                    facts=facts,
                )
            )
        else:  # error
            payload = emit_error(
                ErrorInputs(
                    heading=args.heading,
                    message=args.message,
                    detail=args.detail,
                )
            )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    sys.stdout.write(emit_to_json(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
