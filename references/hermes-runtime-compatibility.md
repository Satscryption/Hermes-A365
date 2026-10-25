# Hermes runtime compatibility

Snapshot date: 2026-10-25.

This note is the installation contract for running Hermes Agent and
`hermes-a365[bridge]` in one environment. v0.9.0+ ships against Hermes'
Python 3.14 runtime and keeps `pyjwt[crypto]>=2.15.0`. Issue
[#148](https://github.com/Satscryption/Hermes-A365/issues/148) tracks the
remaining `hermes-agent[web]` FastAPI pin; refs #148.

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

## Hermes v0.22.3+ (current 3.14 gateway line)

Upstream pins `PyJWT[crypto]==2.15.1` on Python 3.14 starting at tag
**v0.22.3**. **v0.22.5** (`56b6a31fededdf7364e1a0a79548d144e7fd81a3`) is the
recommended gateway line. On that tag, `pyproject.toml` declares:

```text
PyJWT[crypto]==2.15.1; python_version >= '3.14'
cryptography==50.0.1; python_version >= '3.14'
fastapi>=0.104.0,<1; python_version >= '3.14'   # core metadata only
```

Core metadata is a **range**, not a frozen pin: the tag's `uv.lock` may still
resolve `fastapi==0.133.1` for the locked environment even though core does not
pin `0.133.1` in metadata. That is separate from the optional `web` extra below.

`PyJWT[crypto]==2.15.1` and `cryptography==50.0.1` satisfy our floors.

**Prerequisite:** install Hermes from the **git tag** (for example `v0.22.5`) or
the Hermes shell installer / `hermes update`. **PyPI `hermes-agent` has no
0.22.x release** (latest public release is `0.19.0`, which pins
`PyJWT[crypto]==2.13.0` and `cryptography==46.0.7` and does not satisfy these
floors).

When Hermes is **>= v0.22.3** (core only, no `[web]` / `[all]`), a combined
install with `hermes-a365[bridge]` can resolve **without** `overrides.txt`:

```bash
uv pip install 'hermes-a365[bridge]'
```

Operator-verified 2026-10-25 on Python 3.14 with tag `v0.22.5`: selected
`PyJWT==2.15.1` and `fastapi==0.162.0` (not merely the `>=0.141.1` floor).

Every **older supported** Hermes ref still pins `PyJWT[crypto]==2.13.0` on
Python 3.14, including **v0.22.2**, plugin floor **0.12.0** / `v2026.4.30`,
multiplex floor **0.17.0** / `v2026.6.19`, and session-pin floor **0.21.1** /
`v2026.9.7`. Use `overrides.txt` for those installs (see below).

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
landed the PyJWT bump (closed 2026-10-22). It is not the FastAPI exit and did
not mention the `web` extra.

## `overrides.txt` (older Hermes and `[web]` / `[all]`)

Keep this file for **PyJWT** on Hermes **< v0.22.3** and for **FastAPI** when
`hermes-agent[web]` or `[all]` is present. On Hermes **>= v0.22.3**, the PyJWT
line is a **no-op** (upstream already satisfies `>=2.15.0`) but remains so one
file covers every supported ref.

```text
pyjwt[crypto]>=2.15.0
fastapi>=0.143
```

```bash
uv pip install --override overrides.txt 'hermes-a365[bridge]'
```

`pyjwt[crypto]>=2.15.0` replaces Hermes' `PyJWT[crypto]==2.13.0` on older tags.
`fastapi>=0.143` replaces `fastapi==0.133.1` from `hermes-agent[web]`. Both
lines stay above our published floors.

If the Hermes venv is not the environment `uv` is using, pass it
explicitly:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python --override overrides.txt 'hermes-a365[bridge]'
```

`--no-deps` is not this path: it would skip the floors entirely. After
install, read back `pyjwt`, `cryptography`, `fastapi`, `starlette`, and
`anyio` before the #123 gateway rows.

## Historical pins (pre-v0.22.3)

Older Hermes revisions pinned `PyJWT[crypto]==2.13.0` on Python 3.14 and
conflicted with `pyjwt[crypto]>=2.15.0`. Host checkout
`f60f6d534375ccd6ec78d47ff1b5a97371cd8d60` and `main` at
`e6899c8090da3bd48c829893e4103ce989d3482d` documented that conflict. From
v0.22.3 onward the upstream PyJWT pin satisfies our floor; `overrides.txt` still
lists `pyjwt[crypto]>=2.15.0` for backward compatibility with older supported
refs and for operators who standardize on one override file.

## CI and evidence

This repository's CI runs ruff and the full pytest suite on **Ubuntu** for
Python 3.11 through 3.14, on **macOS** for Python 3.14 only (live macOS
keychain tests are skipped on GitHub-hosted runners), plus dedicated jobs for
Linux libsecret live keychain, wheel fresh-install smoke, Hermes source tag
checks (including `v0.22.5`), and **Windows** as an unsupported-host negative
probe only. That proves this package across the declared Python matrix and
supported hosts. Hermes source smoke checks the plugin adapter contract and
multiplex source tests only — it does **not** prove PyJWT pins or combined
`uv pip` resolve. Combined Hermes + bridge resolve on v0.22.5 core is
**operator-verified**; use `overrides.txt` for older Hermes refs and when
`hermes-agent[web]` is present.

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
