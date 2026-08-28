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
storage (rotation routed *through* the provider, arbitrary backends) is
deferred to post-1.0 enhancement scope; this slice ships the interface and a
keychain-backed default so the #123 walk can validate secrets-at-rest.

Naming note: this module is deliberately **not** ``secrets.py`` — that name
shadows the stdlib ``secrets`` on the path (starlette/fastapi do
``from secrets import token_hex``), which is why slice 19b renamed the
original module to :mod:`hermes_a365.keychain`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class SecretResolution:
    """A value and its diagnostic state from one provider lookup."""

    value: str = field(repr=False)
    reachable: bool
    provider: str


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

    def resolve_with_status(
        self, tenant: str, app_id: str
    ) -> tuple[str, bool]:
        """Resolve one exact key, returning ``(value, reachable)``.

        Reachability must come from the target lookup itself. Probing an
        unrelated sentinel after a target-specific denial can succeed and
        falsely label the target store as empty, which makes the setup wizard
        recommend minting over a credential that may still exist.
        """
        try:
            found = get_secret(tenant, app_id, backend=self._backend)
        except (KeychainTimeoutError, TimeoutExpired):
            logger.warning("secrets provider: keychain lookup timed out")
            return "", False
        except KeychainError as e:
            logger.debug("secrets provider: keychain unavailable (%s)", e)
            return "", False
        except ValueError as e:
            logger.debug("secrets provider: unusable key (%s)", e)
            return "", False
        except Exception:
            logger.warning("secrets provider: unexpected keychain lookup failure")
            return "", False
        if found is None:
            return "", True
        if not isinstance(found, str):
            return "", False
        return found, True

    def probe(self, tenant: str, app_id: str) -> tuple[bool, bool]:
        """Probe one exact key, returning ``(found, reachable)``."""
        value, reachable = self.resolve_with_status(tenant, app_id)
        return bool(value), reachable

    def resolve(self, tenant: str, app_id: str) -> str | None:
        value, _reachable = self.resolve_with_status(tenant, app_id)
        return value or None


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
    state = resolve_secret_state(
        tenant, app_id, existing="", provider=provider
    )
    return bool(state.value), state.reachable, state.provider


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


def resolve_secret_state(
    tenant: str,
    app_id: str,
    *,
    existing: str | None,
    provider: SecretsProvider | None = None,
) -> SecretResolution:
    """Resolve a value and reachability state with one provider lookup.

    The existing plaintext source still wins. A provider miss is reachable;
    an exception, unusable key, or invalid return type is not. The returned
    object keeps diagnostics tied to the exact lookup that produced the
    value, avoiding contradictory follow-up probes against a changing store.
    """
    if existing:
        return SecretResolution(existing, True, "unconsulted")
    active = provider if provider is not None else default_provider()
    label = provider_label(active)
    if not (tenant and app_id):
        return SecretResolution("", False, label)
    # The shipped resolve() delegates to resolve_with_status(), including for
    # subclasses that inherit it. A subclass may legitimately override the
    # documented resolve() seam; routing around an actual override would
    # silently lose its secret and misclassify the store.
    if (
        isinstance(active, KeychainSecretsProvider)
        and type(active).resolve is KeychainSecretsProvider.resolve
    ):
        value, reachable = active.resolve_with_status(tenant, app_id)
        return SecretResolution(value, reachable, label)
    try:
        found = active.resolve(tenant, app_id)
    except Exception:
        logger.warning(
            "secrets provider %r raised while resolving a secret; "
            "falling back to the plaintext source",
            label,
        )
        return SecretResolution("", False, label)
    if found is None:
        # A KeychainSecretsProvider override has only the legacy Optional[str]
        # result, whose None collapses empty and unreachable. Treat that
        # ambiguity conservatively so a delegating/instrumentation override
        # cannot re-enable credential-rotation advice for a locked store.
        reachable = not isinstance(active, KeychainSecretsProvider)
        return SecretResolution("", reachable, label)
    if not isinstance(found, str):
        logger.warning(
            "secrets provider %r returned a %s, not a str; ignoring it",
            label,
            type(found).__name__,
        )
        return SecretResolution("", False, label)
    return SecretResolution(found, True, label)


def resolve_secret(
    tenant: str,
    app_id: str,
    *,
    existing: str | None,
    provider: SecretsProvider | None = None,
) -> str:
    """Return the value from :func:`resolve_secret_state`."""
    return resolve_secret_state(
        tenant, app_id, existing=existing, provider=provider
    ).value
