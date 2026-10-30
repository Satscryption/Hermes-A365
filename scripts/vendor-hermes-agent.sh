#!/usr/bin/env bash
# Fetch the pinned Hermes Agent revision used by gateway integration tests.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "${ROOT}/scripts/vendor_hermes_agent.py"
