"""agent365 plugin — Microsoft Agent 365 gateway adapter for Hermes.

Discovered by the Hermes plugin loader via the ``hermes_agent.plugins``
entry point (see ``pyproject.toml``). On ``register(ctx)`` we wire two
surfaces:

1. The ``agent365`` platform adapter (BF-shaped activities in/out;
   AAD-v2 inbound JWTs; agentic three-stage user-FIC outbound chain).
   Wired via :func:`hermes_a365.plugin.adapter.register`.

2. The ``hermes a365 <verb>`` CLI surface (doctor / license / register
   / consent / instance / publish / status / cleanup / activity-bridge),
   wired via :func:`hermes_a365.plugin.cli.register_cli`. Each verb
   delegates to the matching ``hermes_a365.<module>``'s ``build_parser``
   + ``run`` pair, so flags stay identical to running ``hermes-a365
   <verb>`` standalone.

The ``adapter`` module imports Hermes harness symbols (``gateway.*``),
so it's imported lazily inside :func:`register` — that keeps the
package importable in environments where Hermes isn't installed (pipx
standalone CLI, ``pytest`` in a dev venv without the Hermes harness).
"""

from __future__ import annotations


def register(ctx) -> None:
    """Plugin entry point — invoked once by the Hermes plugin loader."""
    from .adapter import register as _register_adapter
    from .cli import a365_command, register_cli

    _register_adapter(ctx)

    ctx.register_cli_command(
        name="a365",
        help="Microsoft Agent 365 wrapper (setup, status, cleanup, bridge)",
        setup_fn=register_cli,
        handler_fn=a365_command,
        description=(
            "Wraps Microsoft.Agents.A365.DevTools.Cli for Hermes operators "
            "and runs the Bot Framework activity bridge that backs the "
            "agent365 gateway platform. See: hermes a365 doctor"
        ),
    )


__all__ = ["register"]
