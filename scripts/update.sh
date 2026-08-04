#!/usr/bin/env bash
# Git pull + docker compose rebuild when remote HEAD changes.
# Intended for systemd timer / cron on the Raspberry Pi.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

if [[ ! -d .git ]]; then
  echo "Not a git repo: ${REPO_DIR}" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Missing .env in ${REPO_DIR} — copy .env.example and edit" >&2
  exit 1
fi

git fetch origin

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse @{u} 2>/dev/null || true)"

if [[ -z "${REMOTE}" ]]; then
  echo "No upstream branch configured (git branch -u origin/main)" >&2
  exit 1
fi

if [[ "${LOCAL}" == "${REMOTE}" ]]; then
  echo "Up to date (${LOCAL:0:8})"
  exit 0
fi

echo "Update available: ${LOCAL:0:8} → ${REMOTE:0:8}"
git pull --ff-only

if command -v docker >/dev/null 2>&1; then
  docker compose up -d --build
else
  echo "docker not found" >&2
  exit 1
fi

echo "Updated and restarted"
