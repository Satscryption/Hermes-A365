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

This repository's CI runs ruff and the full pytest suite on Python 3.11
and Python 3.14. That proves this package on the Hermes runtime. It does
not, by itself, prove the combined Hermes resolve; the override above is
what makes that resolve succeed while the upstream pins remain.
