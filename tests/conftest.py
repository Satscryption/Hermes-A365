"""Pytest configuration and shared fixtures.

Adds ``scripts/`` to ``sys.path`` so tests can ``from render_blueprint import ...``
without packaging tricks. Also wires up ``--update-golden`` for regenerating
golden fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def pytest_addoption(parser):  # type: ignore[no-untyped-def]
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite golden fixtures from current rendering output",
    )
