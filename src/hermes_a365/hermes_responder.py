"""hermes a365 — reference responder for the activity bridge.

Tier 1 (slice 19c): receives the bridge's webhook envelope (per
``references/webhook-contract.md``) and returns canned / echoed
replies. This is the *minimum* useful responder — it lets operators
prove the Teams round-trip end-to-end (tunnel → bridge JWT validation
→ webhook forward → reply via ``serviceUrl`` → Adaptive Card render
in Teams) without bringing Hermes' full agent loop into the loop yet.

Modes:

- ``echo``    — reply with ``"You said: <text>"`` for messages, a
  generic acknowledgement for invokes. Default.
- ``greeting`` — reply with the
  ``templates/adaptive-cards/greeting.json.j2`` Adaptive Card on the
  first message in a conversation; echo on subsequent. Useful for
  demo screencaps.
- ``canned``  — read the response from ``--canned-response-file``
  (JSON in the contract shape) and return it. The file is re-read
  on every request so operators can iterate without restarting.

Operators with their own responder ignore this script entirely;
ours is here to (a) make the bridge testable and (b) document the
contract by example.

CLI use::

    python scripts/hermes_responder.py serve --port 9090 --mode echo
    python scripts/hermes_responder.py serve --mode greeting --slug inbox-helper
    python scripts/hermes_responder.py serve --mode canned \\
        --canned-response-file ./responses.json --debug-endpoints

Wire to the bridge via the operator's environment::

    HERMES_BRIDGE_WEBHOOK=http://127.0.0.1:9090/respond \\
        uv run python scripts/activity_bridge.py serve --slug inbox-helper
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# Lazy-imported to keep this script importable from tests / CI without
# the bridge extras installed. Same pattern slice 19b uses.
try:
    from fastapi import Body as _Body
    from fastapi import FastAPI as _FastAPI
    from fastapi import HTTPException as _HTTPException
    from fastapi.responses import JSONResponse as _JSONResponse
except ImportError:  # pragma: no cover
    _Body = None  # type: ignore[assignment]
    _FastAPI = None  # type: ignore[assignment]
    _HTTPException = None  # type: ignore[assignment]
    _JSONResponse = None  # type: ignore[assignment]

from .emit_card import GreetingInputs, emit_greeting

DEFAULT_PORT = 9090
DEFAULT_HOST = "127.0.0.1"
DEFAULT_HISTORY_MAX = 50

ResponderMode = Literal["echo", "greeting", "canned"]
_MODES: tuple[ResponderMode, ...] = ("echo", "greeting", "canned")


# ---------------------------------------------------------------------------
# Errors / config
# ---------------------------------------------------------------------------


class ResponderConfigError(RuntimeError):
    """Raised when the responder can't start (missing canned file etc.)."""


@dataclass
class ResponderConfig:
    mode: ResponderMode = "echo"
    canned_response_file: Path | None = None
    slug: str | None = None
    debug_endpoints: bool = False
    history_max: int = DEFAULT_HISTORY_MAX
    log_path: Path | None = None  # resolved from slug if given


def resolve_log_path(slug: str | None) -> Path | None:
    if not slug:
        return None
    home = Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    return home / "agents" / slug / "responder.log"


# ---------------------------------------------------------------------------
# In-memory conversation state
# ---------------------------------------------------------------------------


@dataclass
class ConversationStore:
    """Per-conversation ring buffer of recent activity envelopes.

    Keyed by ``activity.conversation.id``. Bounded at
    ``history_max`` entries per conversation; oldest evicted first.
    Single-process only — restart drops state.
    """

    history_max: int = DEFAULT_HISTORY_MAX
    by_conv: dict[str, deque[dict[str, Any]]] = field(default_factory=dict)

    def append(self, conv_id: str, entry: dict[str, Any]) -> None:
        if not conv_id:
            return
        buf = self.by_conv.get(conv_id)
        if buf is None:
            buf = deque(maxlen=self.history_max)
            self.by_conv[conv_id] = buf
        buf.append(entry)

    def for_conv(self, conv_id: str) -> list[dict[str, Any]]:
        return list(self.by_conv.get(conv_id, []))

    @property
    def conversation_count(self) -> int:
        return len(self.by_conv)


# ---------------------------------------------------------------------------
# Reply rendering — pure functions, easy to test without the FastAPI app
# ---------------------------------------------------------------------------


def render_echo_reply(activity: dict[str, Any]) -> dict[str, Any]:
    text = activity.get("text", "") or ""
    return {"text": f"You said: {text}".strip()}


def render_greeting_reply(*, first_in_conv: bool, activity: dict[str, Any]) -> dict[str, Any]:
    """Greeting card on first turn, echo afterwards.

    Falling back to echo on subsequent turns matches operator
    expectation that the agent doesn't re-greet on every message.
    """
    if not first_in_conv:
        return render_echo_reply(activity)
    card = emit_greeting(
        GreetingInputs(
            heading="Hermes agent ready",
            subtitle="Ask me about your inbox, calendar, or M365 data.",
            commands=("Summarise my inbox", "List today's events"),
        )
    )
    return {"text": "Hi from the Hermes responder.", "card": card}


def render_canned_reply(canned_path: Path) -> dict[str, Any]:
    """Read the canned response JSON from disk on every call.

    Hot-reload behaviour is intentional — operators iterate on the
    response file during demos without restarting the responder.
    """
    if not canned_path.exists():
        raise ResponderConfigError(f"canned response file missing: {canned_path}")
    try:
        return json.loads(canned_path.read_text())
    except json.JSONDecodeError as e:
        raise ResponderConfigError(
            f"canned response file is not valid JSON: {canned_path}: {e}"
        ) from e


def render_invoke_response(activity: dict[str, Any]) -> dict[str, Any]:
    """For ``invoke`` activities — a generic synchronous ack.

    Real responders wire `name` and `value` into action handlers; the
    demo just acknowledges so Teams sees a 200.
    """
    name = activity.get("name", "")
    return {
        "invokeResponse": {
            "status": 200,
            "body": {"text": f"Action '{name}' received."},
        }
    }


# ---------------------------------------------------------------------------
# Logging — JSON-line per request to ~/.hermes/agents/<slug>/responder.log
# or stdout
# ---------------------------------------------------------------------------


def log_event(log_path: Path | None, event: dict[str, Any]) -> None:
    line = json.dumps({"ts": datetime.now(UTC).isoformat(), **event})
    if log_path is None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def make_app(cfg: ResponderConfig, *, store: ConversationStore | None = None) -> Any:
    if _FastAPI is None:
        raise ResponderConfigError(
            "fastapi/uvicorn not installed; run `uv sync --extra bridge`"
        )
    if store is None:
        store = ConversationStore(history_max=cfg.history_max)
    if cfg.mode == "canned" and cfg.canned_response_file is None:
        raise ResponderConfigError("--canned-response-file is required for mode=canned")

    app = _FastAPI(title=f"hermes a365 reference responder ({cfg.mode})")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "mode": cfg.mode,
            "conversations": store.conversation_count,
            "slug": cfg.slug,
        }

    @app.post("/respond")
    async def respond(envelope: dict[str, Any] = _Body(...)) -> Any:  # noqa: B008
        activity = envelope.get("activity", {}) or {}
        conv_id = (activity.get("conversation") or {}).get("id") or ""
        activity_type = activity.get("type", "message")

        first_in_conv = conv_id not in store.by_conv

        # Channel-control activities — bridge already filters most of these,
        # but be defensive: if one arrives, ack with empty.
        if activity_type in ("conversationUpdate", "typing", "endOfConversation"):
            store.append(conv_id, {"in": activity, "out": None})
            return _JSONResponse({"text": ""})

        if activity_type == "invoke":
            reply = render_invoke_response(activity)
        elif cfg.mode == "echo":
            reply = render_echo_reply(activity)
        elif cfg.mode == "greeting":
            reply = render_greeting_reply(first_in_conv=first_in_conv, activity=activity)
        elif cfg.mode == "canned":
            assert cfg.canned_response_file is not None  # guarded in make_app
            try:
                reply = render_canned_reply(cfg.canned_response_file)
            except ResponderConfigError as e:
                raise _HTTPException(status_code=500, detail=str(e)) from e
        else:  # pragma: no cover — argparse already restricts
            raise _HTTPException(status_code=500, detail=f"unknown mode {cfg.mode}")

        store.append(conv_id, {"in": activity, "out": reply})
        log_event(
            cfg.log_path,
            {
                "conversation_id": conv_id,
                "channel": activity.get("channelId"),
                "type": activity_type,
                "mode": cfg.mode,
                "text_in": activity.get("text", ""),
                "first_in_conv": first_in_conv,
            },
        )
        return _JSONResponse(reply)

    if cfg.debug_endpoints:

        @app.get("/history/{conversation_id}")
        async def history(conversation_id: str) -> dict[str, Any]:
            return {
                "conversation_id": conversation_id,
                "activities": store.for_conv(conversation_id),
            }

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="hermes a365 — reference responder for the activity bridge.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the responder daemon")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument(
        "--mode", choices=_MODES, default="echo", help="reply strategy (default: echo)"
    )
    serve.add_argument(
        "--canned-response-file",
        type=Path,
        help="JSON file in the contract shape; required for --mode canned. Hot-reloaded.",
    )
    serve.add_argument(
        "--slug",
        help=(
            "agent slug — when set, logs each turn to "
            "~/.hermes/agents/<slug>/responder.log (else stdout)"
        ),
    )
    serve.add_argument(
        "--debug-endpoints",
        action="store_true",
        help="enable GET /history/<conversation-id> for inspection. Off by default.",
    )
    serve.add_argument(
        "--history-max",
        type=int,
        default=DEFAULT_HISTORY_MAX,
        help=f"per-conversation history cap (default: {DEFAULT_HISTORY_MAX})",
    )

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        return cmd_serve(args)

    parser.error(f"unknown command: {args.cmd}")
    return 2


def cmd_serve(args: argparse.Namespace) -> int:
    if _FastAPI is None:
        print(
            "ERROR: fastapi/uvicorn not installed; run `uv sync --extra bridge`",
            file=sys.stderr,
        )
        return 2

    cfg = ResponderConfig(
        mode=args.mode,
        canned_response_file=args.canned_response_file,
        slug=args.slug,
        debug_endpoints=args.debug_endpoints,
        history_max=args.history_max,
        log_path=resolve_log_path(args.slug),
    )

    if cfg.mode == "canned" and cfg.canned_response_file is None:
        print(
            "ERROR: --canned-response-file is required when --mode=canned",
            file=sys.stderr,
        )
        return 2
    if cfg.canned_response_file is not None:
        # Validate at startup so operators learn fast.
        with contextlib.suppress(ResponderConfigError):
            render_canned_reply(cfg.canned_response_file)

    import uvicorn  # type: ignore[import-not-found]

    app = make_app(cfg)
    sys.stdout.write(
        f"hermes a365 reference responder (mode={cfg.mode}) "
        f"on http://{args.host}:{args.port}\n"
        f"  log: {cfg.log_path or '<stdout>'}\n"
        f"  debug endpoints: {'on' if cfg.debug_endpoints else 'off'}\n"
    )
    sys.stdout.flush()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
