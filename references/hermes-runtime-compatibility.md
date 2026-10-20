# Hermes runtime compatibility

Snapshot date: 2026-10-16.

This note is the installation contract for running Hermes Agent and
`hermes-a365[bridge]` in one environment. v0.9.0 ships against Hermes'
Python 3.14 runtime and keeps `pyjwt[crypto]>=2.15.0`. Issue
[#148](https://github.com/Satscryption/Hermes-A365/issues/148) stays open
to track the upstream pin, not as a reason to lower our floor.

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
them to satisfy Hermes' pins, and do not install with `--no-deps`. The
supported combined install is the uv override below, which forces the
patched versions instead of the pinned ones.

## What Hermes currently declares

Two revisions were read. Neither resolves together with the floors above
on the runtime Hermes actually supports.

### Host checkout #148 inspected

`f60f6d534375ccd6ec78d47ff1b5a97371cd8d60` is the host checkout #148
inspected. It is not a commit on
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
and PyPI `hermes-agent` has no `0.20.0` release (latest public release
`0.19.0`). The installed distribution reported `hermes-agent==0.20.0`;
that version string is the resolver's label for the local tree, not a
revision a reader can open upstream.

That checkout's core dependencies pin `PyJWT[crypto]==2.13.0` with no
Python marker. `uv pip compile --python-version 3.11` of that tree plus
`hermes-a365[bridge]` is unsatisfiable:

```text
hermes-agent==0.20.0 depends on pyjwt[crypto]==2.13.0
hermes-a365[bridge] depends on pyjwt[crypto]>=2.15.0
```

The same checkout's optional `web` extra pins FastAPI 0.133.1, below
`fastapi>=0.141.1`. That FastAPI pin is separate from the PyJWT pin.

### Hermes `main` on 2026-10-16

Commit `e6899c8090da3bd48c829893e4103ce989d3482d`
(`pyproject.toml` blob `5145f275d14a0ab119802821c715a33123b15551`).

Hermes declares `requires-python = ">=3.11,<3.15"` so an older install
can run its updater, and the file states that **3.14 is the supported
runtime**. The PyJWT pin moved behind that runtime marker:

```text
PyJWT[crypto]==2.13.0; python_version >= '3.14'
cryptography==50.0.1; python_version >= '3.14'
fastapi>=0.104.0,<1; python_version >= '3.14'
```

`cryptography==50.0.1` satisfies our floor. The core FastAPI range can
select `>=0.141.1`. `PyJWT[crypto]==2.13.0` does not satisfy
`pyjwt[crypto]>=2.15.0`, so a Python 3.14 resolve of Hermes core plus
`hermes-a365[bridge]` is still unsatisfiable.

The optional `web` extra has no Python marker:

```text
fastapi==0.133.1
starlette==1.3.1
```

`starlette==1.3.1` meets our floor. `fastapi==0.133.1` does not meet
`fastapi>=0.141.1`. Selecting `hermes-agent[web]` (including via
`hermes-agent[all]`) conflicts on every Python version, not only 3.14.
The gateway plugin does not need that extra; the Hermes dashboard does.

AnyIO is not a direct Hermes pin in this revision. The #148 host
observation of AnyIO 4.12.1 was an installed environment, not a second
metadata conflict in current `pyproject.toml`.

## Supported install until the upstream pins move

Ordinary resolution on Python 3.14 is still unsatisfiable. Two pins block
it, and they do not share an upstream exit.

[NousResearch/hermes-agent#134394](https://github.com/NousResearch/hermes-agent/issues/134394)
tracks the PyJWT pin only and is not the FastAPI exit. It asks to raise
`PyJWT[crypto]==2.13.0` to `>=2.14` / `>=2.15`, plus httpx2, urllib3, and
oauthlib. It does not mention FastAPI or the `web` extra. #148 stays open
for the PyJWT conflict until that pin satisfies `pyjwt[crypto]>=2.15.0`.

`fastapi==0.133.1` in `hermes-agent[web]` (including `hermes-agent[all]`)
is a separate pin. It does not satisfy `fastapi>=0.141.1`. No upstream
issue is cited here as its exit. refs #148 until that pin moves on its own.

Our floors do not move in the meantime.

Until then, install this package into the Hermes Python 3.14 environment
with `overrides.txt` from this repository:

```text
pyjwt[crypto]>=2.15.0
fastapi>=0.143
```

```bash
uv pip install --override overrides.txt 'hermes-a365[bridge]'
```

`pyjwt[crypto]>=2.15.0` replaces Hermes' Python 3.14 pin and matches our
floor. `fastapi>=0.143` replaces `fastapi==0.133.1` from
`hermes-agent[web]` (including `hermes-agent[all]`). It is above our
`fastapi>=0.141.1` floor, so it does not weaken the package. If that extra
is not installed, the FastAPI line is unused by the conflict but remains
in the file so one override covers both pins. `starlette==1.3.1` in the
web extra already meets `starlette>=1.3.1`.

If the Hermes venv is not the environment `uv` is using, pass it
explicitly:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python --override overrides.txt 'hermes-a365[bridge]'
```

`--no-deps` is not this path: it would skip the floors entirely. After
install, read back `pyjwt`, `cryptography`, `fastapi`, `starlette`, and
`anyio` before the #123 gateway rows.

This repository's CI runs ruff and the full pytest suite on **Ubuntu** for
Python 3.11 through 3.14, on **macOS** for Python 3.14 only (live macOS
keychain tests are skipped on GitHub-hosted runners), plus dedicated jobs for
Linux libsecret live keychain, wheel fresh-install smoke, Hermes source tag
checks, and **Windows** as an unsupported-host negative probe only. That
proves this package across the declared Python matrix and supported hosts. It
does not, by itself, prove the combined Hermes resolve; the override above is
what makes that resolve succeed while the upstream pins remain.

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
