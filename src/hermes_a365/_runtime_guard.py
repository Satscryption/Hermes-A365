"""Startup compatibility warnings for the standalone CLI (#126).

Doctor is the authoritative probe surface; this module emits early
``UserWarning``s so scripted installs do not mistake an unsupported host
for a healthy one before probes run.
"""

from __future__ import annotations

import sys
import warnings

from .compatibility import (
    PYTHON_VERSIONS_CI,
    REQUIRES_PYTHON,
    host_platform_supported,
    python_in_ci_matrix,
    python_in_supported_range,
    python_version_tuple,
)


def emit_runtime_warnings() -> None:
    """Warn on unsupported or unverified host/Python combinations."""

    ver = python_version_tuple()
    label = f"{ver[0]}.{ver[1]}.{ver[2]}"

    if not python_in_supported_range(ver):
        warnings.warn(
            f"Python {label} is below the v1 minimum; supported range is {REQUIRES_PYTHON}",
            UserWarning,
            stacklevel=2,
        )
    elif not python_in_ci_matrix(ver):
        warnings.warn(
            f"Python {label} meets {REQUIRES_PYTHON} but is outside the CI-tested "
            f"range ({', '.join(PYTHON_VERSIONS_CI)}); not continuously verified",
            UserWarning,
            stacklevel=2,
        )

    if sys.platform == "win32":
        warnings.warn(
            "Windows is unsupported for Hermes-A365 v1 (no OS secret-store backend). "
            "Use macOS or Linux for end-to-end operator flows; see "
            "references/v1-compatibility-matrix.md",
            UserWarning,
            stacklevel=2,
        )
    elif not host_platform_supported():
        warnings.warn(
            f"Host platform {sys.platform} is outside the v1 macOS/Linux support "
            "matrix; see references/v1-compatibility-matrix.md",
            UserWarning,
            stacklevel=2,
        )
