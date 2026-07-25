"""Secrets-at-rest provider seam (#19).

Both A365 client secrets — ``A365_BLUEPRINT_CLIENT_SECRET`` (Path A
blueprint) and ``A365_BF_CLIENT_SECRET`` (the separate non-agentic Path B
Bot Framework app, #36) — currently live in plaintext on disk: the operator
``~/.hermes/.env``, the per-agent ``~/.hermes/agents/<slug>/.env``, and the
GA CLI's ``a365.generated.config.json``. :mod:`hermes_a365.keychain` has
shipped an OS-keychain store for a while, but **nothing read from it** —
no runtime path consulted the keychain, so an operator who stored a secret
there got no benefit. This module is the missing retrieval seam.

Precedence (deliberate, #19 v0.9.0 minimum scope)::

    existing plaintext source  ->  provider (OS keychain)  ->  absent

The **existing source always wins**; the provider fills only a *miss*. Two
reasons, both load-bearing:

* **No behaviour change.** Every operator today resolves secrets from
  env/.env/generated-config. Those values keep winning byte-for-byte, so
  wiring this in cannot shift which credential a running deployment uses.
* **Rotation safety.** ``register --apply`` rotates the blueprint secret
  into ``a365.generated.config.json``. If the keychain outranked it, a
  stale keychain entry would silently defeat the rotation. Filling a miss
  can never do that.

To *use* the keychain, an operator stores the secret and removes it from
the plaintext file — the keychain then fills the resulting miss. Putting it
back in ``.env`` restores the documented fallback. Full keychain-primary
storage (rotation routed *through* the provider, arbitrary backends) is the
remaining stretch scope on #19; this slice ships the interface and a
keychain-backed default so the #123 walk can validate secrets-at-rest.

Naming note: this module is deliberately **not** ``secrets.py`` — that name
shadows the stdlib ``secrets`` on the path (starlette/fastapi do
``from secrets import token_hex``), which is why slice 19b renamed the
original module to :mod:`hermes_a365.keychain`.
"""

from __future__ import annotations

import logging
from subprocess import TimeoutExpired
from typing import Protocol, runtime_checkable

from .keychain import (
    KeychainBackend,
    KeychainError,
    KeychainTimeoutError,
    get_secret,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class SecretsProvider(Protocol):
    """A source of secrets-at-rest, keyed by ``(tenant, app_id)``.

    The key shape is the one :func:`hermes_a365.keychain.account_name`
    already uses, so it distinguishes the Path A blueprint app from the
    Path B Bot Framework app without a schema change.

    Implementations return ``None`` for "I don't have it" and should not
    raise for that case — an absent secret is normal, not an error.
    """

    name: str

    def resolve(self, tenant: str, app_id: str) -> str | None: ...


class KeychainSecretsProvider:
    """Default provider: the OS keychain via :mod:`hermes_a365.keychain`.

    macOS (``security``) and Linux (``secret-tool``) only — Windows stays
    deferred per the note at ``keychain.py``. An unavailable backend is a
    *miss*, not a failure: a Linux box without libsecret, or any platform
    the keychain module doesn't support, simply resolves nothing and the
    caller keeps its plaintext value.
    """

    name = "os-keychain"

    def __init__(self, backend: KeychainBackend | None = None) -> None:
        self._backend = backend

    def probe(self, tenant: str, app_id: str) -> tuple[bool, bool]:
        """Probe one exact key, returning ``(found, reachable)``.

        Reachability must come from the target lookup itself. Probing an
        unrelated sentinel after a target-specific denial can succeed and
        falsely label the target store as empty, which makes the setup wizard
        recommend minting over a credential that may still exist.
        """
        try:
            found = get_secret(tenant, app_id, backend=self._backend)
        except (KeychainError, ValueError):
            return False, False
        except Exception:
            return False, False
        if found is None:
            return False, True
        if not isinstance(found, str):
            return False, False
        return bool(found), True

    def resolve(self, tenant: str, app_id: str) -> str | None:
        try:
            return get_secret(tenant, app_id, backend=self._backend)
        except (KeychainTimeoutError, TimeoutExpired):
            # The backend boundary has already replaced TimeoutExpired with a
            # sanitized exception, so no secret-bearing argv can escape.
            logger.warning("secrets provider: keychain lookup timed out")
            return None
        except KeychainError as e:
            # No backend binary / unsupported platform. Expected on plenty
            # of hosts — debug, not warn, and never the secret value.
            logger.debug("secrets provider: keychain unavailable (%s)", e)
            return None
        except ValueError as e:
            # account_name() rejects an empty/`/`-bearing tenant or app id.
            # Path-A-only operators legitimately have bf_app_id="", so this
            # must degrade to a miss rather than crash a runtime read.
            logger.debug("secrets provider: unusable key (%s)", e)
            return None


_default_provider: SecretsProvider = KeychainSecretsProvider()


def default_provider() -> SecretsProvider:
    """The provider used when a call site passes none."""
    return _default_provider


def set_default_provider(provider: SecretsProvider) -> None:
    """Swap the process-wide default (the pluggability seam for #19).

    Lets an operator or an embedding application register a Vault / AWS
    Secrets Manager / Azure Key Vault backend without this package
    depending on any of them.
    """
    global _default_provider
    _default_provider = provider


def probe_provider(
    tenant: str,
    app_id: str,
    *,
    provider: SecretsProvider | None = None,
) -> tuple[bool, bool, str]:
    """Ask a provider about a key, distinguishing *empty* from *unreachable*.

    Returns ``(found, reachable, label)``. ``resolve_secret`` deliberately
    collapses every failure to ``""`` — a runtime credential read only cares
    whether it got a value. Diagnostics care about more: telling an operator
    their store is empty, when really the keychain was locked, sends them to
    ``register --apply --auto-recover-secret``, which mints a fresh secret
    that then outranks the one they had stored all along.

    Never raises, and never returns the secret — only whether one is there.
    """
    active = provider if provider is not None else default_provider()
    label = provider_label(active)
    if not (tenant and app_id):
        # resolve_secret short-circuits on an empty key, so the provider was
        # not consulted; that is not evidence about the store either way.
        return False, False, label
    if isinstance(active, KeychainSecretsProvider):
        found, reachable = active.probe(tenant, app_id)
        return found, reachable, label
    try:
        found = active.resolve(tenant, app_id)
    except Exception:
        logger.warning("secrets provider %r raised while probing", label)
        return False, False, label
    if found is None:
        return False, True, label
    if not isinstance(found, str):
        logger.warning(
            "secrets provider %r returned a %s while probing, not a str",
            label,
            type(found).__name__,
        )
        return False, False, label
    return bool(found), True, label


def provider_label(provider: object | None = None) -> str:
    """A display name for ``provider`` (the active default when omitted).

    ``name`` is part of the Protocol, but a third-party provider written to
    the narrower ``resolve(tenant, app_id)`` contract may not define it, and
    ``set_default_provider`` does not validate. A *label* — used only in logs
    and operator messages — must never be the thing that raises on a
    credential path, so fall back to the class name, and treat an empty or
    non-string ``name`` as absent.

    The lookup is wrapped rather than left to ``getattr``'s default, which
    only swallows ``AttributeError``: ``name`` may be a property, and one
    that does a lazy backend handshake can raise anything at all.
    """
    active = provider if provider is not None else default_provider()
    try:
        name = getattr(active, "name", None)
        if isinstance(name, str) and name:
            return name
    except Exception:
        # `name` may be a property that does a lazy backend handshake; a
        # label is never worth an exception on a credential path. Fall
        # through to the class name, which is still useful.
        pass
    try:
        return type(active).__name__
    except Exception:
        return "unknown-provider"


def resolve_secret(
    tenant: str,
    app_id: str,
    *,
    existing: str | None,
    provider: SecretsProvider | None = None,
) -> str:
    """Return ``existing`` if it holds a value, else ask the provider.

    ``existing`` is whatever the call site already resolved from its
    plaintext sources — it always wins (see the module docstring). Returns
    ``""`` when neither tier has the secret, matching the empty-string
    convention the call sites already use for "not configured".

    Never raises: a provider that misbehaves must not take down a runtime
    credential read, so a failing third-party implementation degrades to
    the plaintext tier. That is a deliberate boundary around *pluggable*
    code, not blanket exception-swallowing — the failure is logged at
    warning level (without the secret) so it stays visible.
    """
    if existing:
        return existing
    if not (tenant and app_id):
        return ""
    active = provider if provider is not None else default_provider()
    label = provider_label(active)
    try:
        found = active.resolve(tenant, app_id)
    except Exception:
        logger.warning(
            "secrets provider %r raised while resolving a secret; "
            "falling back to the plaintext source",
            label,
        )
        return ""
    if found is None:
        return ""
    if not isinstance(found, str):
        # Belt-and-braces at the pluggable boundary: a third-party provider
        # returning bytes/int would otherwise be carried into an OAuth POST
        # body as-is. Refuse rather than guess an encoding.
        logger.warning(
            "secrets provider %r returned a %s, not a str; ignoring it",
            label,
            type(found).__name__,
        )
        return ""
    return found
