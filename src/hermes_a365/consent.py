"""hermes a365 consent — admin-consent URL rendering and grant polling.

Three steps:

1. Render the admin-consent URL from ``templates/consent-url.txt.j2`` using
   ``A365_TENANT_ID`` and ``A365_APP_ID`` (the blueprint Entra app id)
   from ``~/.hermes/.env``.
2. Open the URL in the default browser unless ``--no-open``.
3. Poll ``a365 query-entra blueprint-scopes --agent-name <name>`` every
   ``interval`` seconds until the scopes are reported as consented or
   the timeout (default 5 min) elapses.

Polling is delegated to the ``QuerySource`` from ``status.py`` — slice
18k aligned this with the v0.2 protocol after the live walkthrough
caught the previous implementation calling a non-existent
``query_consent`` method (bug #8 in ``references/live-tenant-test.md``).
Tests substitute a stub and ``time.sleep`` is monkeypatched so the
suite stays hermetic and fast. Re-running this command after consent
is already granted is a no-op (returns ``True`` immediately).

In normal v0.2 usage admin consent is already granted by the second
device-code + browser flow that ``a365 setup blueprint`` triggers; this
command stays useful for re-granting after a revocation or for
verifying current state without re-running setup.

CLI use::

    python scripts/consent.py "<agent-name>"                  # full flow
    python scripts/consent.py "<agent-name>" --no-open        # don't launch browser
    python scripts/consent.py "<agent-name>" --print-url-only # just emit the URL
    python scripts/consent.py "<agent-name>" --timeout 60     # custom poll timeout
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from pathlib import Path

from ._common import jinja_env, parse_env
from .status import QuerySource, _classify_scopes_output, get_query_source

DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 300.0
_HERMES_HOME_ENV = "HERMES_HOME"
_HERMES_HOME_DEFAULT = "~/.hermes"


# ---------------------------------------------------------------------------
# URL rendering
# ---------------------------------------------------------------------------


def render_consent_url(tenant: str, app_id: str) -> str:
    """Render the admin-consent URL from ``templates/consent-url.txt.j2``."""
    if not tenant or not app_id:
        raise ValueError("tenant and app_id must both be non-empty")
    env = jinja_env()
    template = env.get_template("consent-url.txt.j2")
    return template.render(tenant=tenant, app_id=app_id).strip()


# ---------------------------------------------------------------------------
# ~/.hermes/.env loading
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    raw = os.environ.get(_HERMES_HOME_ENV) or _HERMES_HOME_DEFAULT
    return Path(os.path.expanduser(raw))


def load_tenant_and_app(hermes_home: Path | None = None) -> tuple[str, str]:
    """Read ``A365_TENANT_ID`` and ``A365_APP_ID`` from ``~/.hermes/.env``.

    Raises ``FileNotFoundError`` if the env file is missing (skill not yet
    bootstrapped) or ``KeyError`` if the required keys aren't set.
    """
    if hermes_home is None:
        hermes_home = _resolve_hermes_home()
    env_file = hermes_home / ".env"
    if not env_file.exists():
        raise FileNotFoundError(f"{env_file} does not exist; run `hermes a365 register` first")
    env = parse_env(env_file.read_text())
    tenant = env.get("A365_TENANT_ID", "")
    app_id = env.get("A365_APP_ID", "")
    if not tenant or not app_id:
        missing = [k for k, v in (("A365_TENANT_ID", tenant), ("A365_APP_ID", app_id)) if not v]
        raise KeyError(f"missing keys in {env_file}: {missing}")
    return tenant, app_id


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def _is_granted(text: str | None) -> bool:
    """True iff ``text`` is non-empty and classifies as ``ok`` (scopes consented).

    Reuses the same heuristic as ``status.py`` so consent.py and status.py
    agree on what "granted" means for a given CLI output.
    """
    if text is None:
        return False
    state, _detail = _classify_scopes_output(text)
    return state == "ok"


def poll_for_consent(
    qs: QuerySource,
    *,
    agent_name: str,
    tenant_id: str | None = None,
    interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Poll ``qs.query_blueprint_scopes`` until granted or timeout elapses.

    Returns True if consent is detected within the window, False otherwise.
    Raises ``RuntimeError`` if the query source isn't available — callers
    can't usefully wait on it, so we surface the condition immediately.
    """
    if not qs.available:
        raise RuntimeError(
            "a365 CLI unavailable; cannot poll for consent. Install the A365 CLI and re-run."
        )
    if interval <= 0:
        raise ValueError("interval must be > 0")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")

    deadline = time.monotonic() + timeout
    while True:
        text = qs.query_blueprint_scopes(agent_name=agent_name, tenant_id=tenant_id)
        if _is_granted(text):
            return True
        if time.monotonic() + interval >= deadline:
            break
        time.sleep(interval)
    # Final check after the loop, in case the last sleep landed near the deadline.
    return _is_granted(qs.query_blueprint_scopes(agent_name=agent_name, tenant_id=tenant_id))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    if parser is None:
        parser = argparse.ArgumentParser(
            description="hermes a365 consent — render admin-consent URL and poll for grant.",
        )
    parser.add_argument(
        "agent_name",
        nargs="?",
        help="agent base name (required for polling; optional with --print-url-only)",
    )
    parser.add_argument(
        "--tenant-id",
        help="override tenant id for query-entra calls (default: from ~/.hermes/.env)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="skip launching the default browser",
    )
    parser.add_argument(
        "--print-url-only",
        action="store_true",
        help="emit only the URL to stdout; do not open or poll",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="poll timeout in seconds (default 300)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="poll interval in seconds (default 5)",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    try:
        tenant, app_id = load_tenant_and_app()
    except (FileNotFoundError, KeyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        url = render_consent_url(tenant, app_id)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.print_url_only:
        sys.stdout.write(url + "\n")
        return 0

    if not args.agent_name:
        print(
            "ERROR: agent_name is required for polling. "
            "Pass it as the first positional argument, "
            "or use --print-url-only to emit the URL without polling.",
            file=sys.stderr,
        )
        return 2

    sys.stdout.write(f"Admin consent URL:\n  {url}\n\n")

    if not args.no_open:
        try:
            webbrowser.open(url)
            sys.stdout.write("Opened in default browser.\n")
        except webbrowser.Error as e:
            print(f"WARN: could not launch browser: {e}", file=sys.stderr)

    qs = get_query_source()
    if not qs.available:
        print(
            "WARN: a365 CLI unavailable; cannot poll for consent automatically.\n"
            "      After granting consent, run `hermes a365 status` to confirm.",
            file=sys.stderr,
        )
        return 1

    sys.stdout.write(f"Polling for consent every {args.interval:g}s (up to {args.timeout:g}s)...\n")
    try:
        granted = poll_for_consent(
            qs,
            agent_name=args.agent_name,
            tenant_id=args.tenant_id,
            interval=args.interval,
            timeout=args.timeout,
        )
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if granted:
        sys.stdout.write("Consent granted.\n")
        return 0
    sys.stdout.write("Consent NOT granted within timeout. Re-run when ready.\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
