# Hermes runtime compatibility

Snapshot date: 2026-10-25.

This note is the installation contract for running Hermes Agent and
`hermes-a365[bridge]` in one environment. v0.9.0+ ships against Hermes'
Python 3.14 runtime and keeps `pyjwt[crypto]>=2.15.0`. Issue
[#148](https://github.com/Satscryption/Hermes-A365/issues/148) tracked the
upstream PyJWT pin; Hermes **v0.22.5** (`56b6a31fededdf7364e1a0a79548d144e7fd81a3`)
pins `PyJWT[crypto]==2.15.1` on Python 3.14, which satisfies our floor. Core
combined resolve no longer needs a PyJWT override. The optional `web` extra still
pins `fastapi==0.133.1`; refs #148 for that remaining gap.

## Floors this package will not lower

`bridge` and `dev` both require:

| Package | Floor |
|---|---|
| `pyjwt[crypto]` | `>=2.15.0` |
| `cryptography` | `>=50.0.0` |
| `anyio` | `>=4.14.2` |
| `fastapi` | `>=0.141.1` |
| `starlette` | `>=1.3.1` |
| `idna` | `>=3.15` |

These are security minimums from the #125 baseline and the #123
preflight (PyJWT advisories 18–30, AnyIO alerts 16–17). Do not lower
them to satisfy Hermes' pins, and do not install with `--no-deps`.

## Hermes v0.22.5 (current 3.14 gateway line)

Tag `v0.22.5` on
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) pins:

```text
PyJWT[crypto]==2.15.1; python_version >= '3.14'
cryptography==50.0.1; python_version >= '3.14'
fastapi>=0.104.0,<1; python_version >= '3.14'   # core only
```

`PyJWT[crypto]==2.15.1` and `cryptography==50.0.1` satisfy our floors. A
plain `uv pip install` of Hermes core (no `[web]` extra) together with
`hermes-a365[bridge]` resolves without `overrides.txt` once
`hermes-agent==0.22.5` is available in the install environment (Hermes PM /
lock on the tag, or PyPI when that release ships).

Verified 2026-10-25 on Python 3.14: combined core install selected
`PyJWT==2.15.1` and `fastapi>=0.141.1` with no PyJWT line in
`overrides.txt`.

The optional `web` extra (including via `hermes-agent[all]`) still declares:

```text
fastapi==0.133.1
starlette==1.3.1
```

`starlette==1.3.1` meets our floor. `fastapi==0.133.1` does not meet
`fastapi>=0.141.1`. Selecting `hermes-agent[web]` together with
`hermes-a365[bridge]` remains unsatisfiable without an override. The gateway
plugin does not need that extra; the Hermes dashboard does.

[NousResearch/hermes-agent#134394](https://github.com/NousResearch/hermes-agent/issues/134394)
closed 2026-10-22 with the PyJWT bump. It is not the FastAPI exit and did not
mention the `web` extra.

## FastAPI override when `[web]` is installed

When `hermes-agent[web]` (or `[all]`) is in the same environment as
`hermes-a365[bridge]`, install with `overrides.txt` from this repository:

```text
fastapi>=0.143
```

```bash
uv pip install --override overrides.txt 'hermes-a365[bridge]'
```

`fastapi>=0.143` replaces `fastapi==0.133.1` from `hermes-agent[web]`. It is
above our `fastapi>=0.141.1` floor, so it does not weaken the package. When the
`web` extra is not installed, the override file is unused but harmless.

If the Hermes venv is not the environment `uv` is using, pass it
explicitly:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python --override overrides.txt 'hermes-a365[bridge]'
```

`--no-deps` is not this path: it would skip the floors entirely. After
install, read back `pyjwt`, `cryptography`, `fastapi`, `starlette`, and
`anyio` before the #123 gateway rows.

## Historical pins (pre-v0.22.5)

Older Hermes revisions pinned `PyJWT[crypto]==2.13.0` on Python 3.14 and
conflicted with `pyjwt[crypto]>=2.15.0`. Host checkout
`f60f6d534375ccd6ec78d47ff1b5a97371cd8d60` and `main` at
`e6899c8090da3bd48c829893e4103ce989d3482d` documented that conflict. Prior
combined installs used a two-line `overrides.txt` (`pyjwt[crypto]>=2.15.0` plus
`fastapi>=0.143`); only the FastAPI line remains after v0.22.5.

## CI and evidence

This repository's CI runs ruff and the full pytest suite on **Ubuntu** for
Python 3.11 through 3.14, on **macOS** for Python 3.14 only (live macOS
keychain tests are skipped on GitHub-hosted runners), plus dedicated jobs for
Linux libsecret live keychain, wheel fresh-install smoke, Hermes source tag
checks (including `v0.22.5`), and **Windows** as an unsupported-host negative
probe only. That proves this package across the declared Python matrix and
supported hosts. Combined Hermes resolve on v0.22.5 core is operator-verified;
the FastAPI override above still applies when `hermes-agent[web]` is present.

## Copilot→Teams session pin (#82)

Continuation tokens rebind a Teams 1:1 onto the Copilot Hermes session by
emitting an internal `MessageEvent` whose `metadata["gateway_session_id"]`
is that session id. Hermes then calls `session_store.switch_session`. The
adapter resolves the id with `SessionDB.find_session_by_origin` and
refuses the pin unless `get_session` still shows that same live row.

`switch_session` reopens its target. This package therefore does not
dispatch when the row is missing, has `ended_at` set, or carries an
explicit end reason (`session_reset`, `user_exit`, `session_switch`,
`new_session`). It does not call `reopen_session`.

Two Hermes floors apply.

**Safety floor.** Fail closed, and skip the pin, when
`hermes_state_registry` is absent. That is safe on the commit that
refuses to inject `gateway_session_id` into an ended session:

`75efd73961a88a531276d0e2e1cd30d1ec18019e` (2026-07-08,
`fix(gateway): never resurrect ended sessions for delegation completions`),
first tagged in `v2026.7.20`.

That commit sits on top of:

| Piece | Commit |
|---|---|
| `session_store.switch_session` | `00ce5f04d9cfad2e30d5e08e7a6bf135f6e53030` |
| `MessageEvent` `metadata["gateway_session_id"]` | `d39c62409b5f00da6f7bd31dd3fb437d8c2daad6` |
| `SessionDB.find_session_by_origin` | present by `d39c62409` (then in `hermes_state.py`; later `hermes_state_sessions.py`) |

`v2026.7.20` contains the pin symbols in that table and predates
`hermes_state_registry`. This adapter imports
`hermes_state_registry.acquire` and, on `ImportError`, stores no session
id. The continuation link can still be minted. The pin stays skipped
until the resolution floor below.

**Resolution floor.** The adapter can open the shared `SessionDB` and
resolve the origin row. `hermes_state_registry.acquire` lands in
`db339f0051e76733923d3d8c4e4319e3e45444a3` (2026-09-01,
`fix(state): consolidate gateway SessionDB writers via process-wide shared registry`).
First tagged Hermes release that contains that module: `v2026.9.7`.
Current `main` still honors `gateway_session_id` from
`gateway/run_turn_prepare.py`. Idle-ended pins on newer Hermes can
retarget to the destination's current row; this adapter does not dispatch
those, so the retarget does not run.

The upstream surface is not a stable third-party adapter API. A live
Copilot Chat → Teams 1:1 round trip is residual for #116 / #89.
