# Hermes runtime compatibility

Snapshot date: 2026-10-16.

This note is the installation contract for running Hermes Agent and
`hermes-a365[bridge]` in one environment. It exists because issue
[#148](https://github.com/Satscryption/Hermes-A365/issues/148) blocks the
v0.9.0 gateway walk (#123) until that combined install resolves with
ordinary dependency resolution.

## Floors this package will not lower

`bridge` and `dev` both require:

| Package | Floor |
|---|---|
| `pyjwt[crypto]` | `>=2.15.0` |
| `cryptography` | `>=50.0.0` |
| `anyio` | `>=4.14.2` |
| `fastapi` | `>=0.141.1` |
| `starlette` | `>=1.3.1` |

These are security minimums from the #125 baseline and the #123
preflight (PyJWT advisories 18–30, AnyIO alerts 16–17). A combined
install that needs `--no-deps`, a uv `override-dependencies` entry, or a
lower floor is not a supported installation and is not evidence that
#148 or the #123 gateway rows passed.

## What Hermes currently declares

Two revisions were read. Neither resolves together with the floors above
on the runtime Hermes actually supports.

### Hermes Agent 0.20.0

Commit `f60f6d534375ccd6ec78d47ff1b5a97371cd8d60`, the revision #148
inspected. Core dependencies pin `PyJWT[crypto]==2.13.0` with no Python
marker. `uv pip compile --python-version 3.11` of `hermes-agent` plus
`hermes-a365[bridge]` is unsatisfiable:

```text
hermes-agent==0.20.0 depends on pyjwt[crypto]==2.13.0
hermes-a365[bridge] depends on pyjwt[crypto]>=2.15.0
```

That release also ships an optional `web` extra pinned to FastAPI
0.133.1, below `fastapi>=0.141.1`.

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

## Compatible path

There is no Hermes revision identified here that installs next to this
package under ordinary resolution on Python 3.14. The repair is upstream,
in Hermes' exact-pin style:

1. Change the core marker line from `PyJWT[crypto]==2.13.0` to
   `PyJWT[crypto]==2.15.0` (or a newer exact release) and regenerate
   Hermes' lock. Our floor stays `>=2.15.0`.
2. If the walk or the operator also installs `hermes-agent[web]`, move
   `fastapi==0.133.1` to an exact release that satisfies
   `fastapi>=0.141.1`, keeping `starlette` at a release that satisfies
   `>=1.3.1`.
3. Resolve and install with a normal installer. Read back `pyjwt`,
   `cryptography`, `fastapi`, `starlette`, and `anyio`. Then run plugin
   discovery and the gateway checks in #123 section 2.

Do not close #148, and do not mark #123 rows 2a–2d passed, on the basis
of a uv override or `--no-deps` install. Those hide the conflict instead
of satisfying it. Azure-only #123 probes do not need this install and
can proceed on their own.

Python 3.11 is what this repository's CI runs. It does not prove the
combined Python 3.14 install, because current Hermes applies the PyJWT
pin only on `>=3.14` and does not support 3.11 as the gateway runtime.
