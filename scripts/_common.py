"""Shared helpers for hermes-a365 scripts.

Path conventions:
- Repo root contains ``scripts/``, ``templates/``, ``references/`` as siblings.
- When running from the installed Hermes skill location
  (``~/.hermes/hermes-agent/optional-skills/cloud-platforms/hermes-a365/``)
  the layout is the same.
- ``skill_root()`` resolves to the parent of this file's ``scripts/`` directory.
"""

from __future__ import annotations

from pathlib import Path

import jinja2


def skill_root() -> Path:
    """Return the directory that contains scripts/, templates/, references/."""
    return Path(__file__).resolve().parent.parent


def templates_dir() -> Path:
    return skill_root() / "templates"


def jinja_env(*, extra_searchpaths: list[Path] | None = None) -> jinja2.Environment:
    """Construct a Jinja environment rooted at ``templates/``.

    StrictUndefined: any unset variable raises rather than rendering empty.
    autoescape=False: we render JSON/.env/text, not HTML.
    keep_trailing_newline=True: deterministic output for golden-file tests.
    """
    searchpaths = [str(templates_dir())]
    if extra_searchpaths:
        searchpaths.extend(str(p) for p in extra_searchpaths)
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpaths),
        autoescape=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
