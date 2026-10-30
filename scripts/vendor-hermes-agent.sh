#!/usr/bin/env bash
# Fetch the pinned Hermes Agent revision used by gateway integration tests.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="${ROOT}/.vendor/hermes-agent"
PIN="bf0203a794a07aac3ff764972ab9087baa4e4f4e"
REPO="https://github.com/NousResearch/hermes-agent.git"
if [[ -d "${VENDOR}/.git" ]]; then
  git -C "${VENDOR}" fetch --depth 1 origin "${PIN}" 2>/dev/null || true
  git -C "${VENDOR}" checkout --detach "${PIN}"
else
  mkdir -p "${ROOT}/.vendor"
  git clone --depth 1 "${REPO}" "${VENDOR}"
  git -C "${VENDOR}" checkout --detach "${PIN}"
fi
test "$(git -C "${VENDOR}" rev-parse HEAD)" = "${PIN}"
