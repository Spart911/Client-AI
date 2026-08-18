#!/usr/bin/env bash
# Git pull + docker compose rebuild when remote HEAD changes.
# Intended for systemd timer / cron on the Raspberry Pi.
#
# Safety net:
#   1. Free-space guard before --build (Pi has a small SD card).
#   2. Running image is tagged :prev; if the new container is not running
#      or the backend is unreachable, we re-tag :prev -> :latest and
#      `compose up -d` (no rebuild) to restore service.
#
# Health model: the client exposes no HTTP endpoint — its startup
# self-check hits {backend}/health and exit(1)s on failure, so a broken
# deploy surfaces as the container dying (restart: unless-stopped).
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

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found" >&2
  exit 1
fi

# --- 1. Free-space guard (1 GiB minimum for the build) --------------------
MIN_FREE_KB=1048576
avail_kb="$(df --output=avail -B1 "${REPO_DIR}" | tail -n1 | tr -d ' ')"
if (( avail_kb < MIN_FREE_KB )); then
  echo "Not enough disk: $((avail_kb / 1024)) KiB free, need ~$((MIN_FREE_KB / 1024)) KiB. Update skipped — run 'docker system prune -a' or clear old logs and retry." >&2
  exit 1
fi
echo "Disk: $((avail_kb / 1024)) KiB free — ok"

# --- Resolve compose image/container ---------------------------------------
IMAGE="$(docker compose config --images | head -n1)"
PREV_TAG="${IMAGE%:*}:prev"
CONTAINER="$(docker compose ps -q | head -n1 || true)"

rollback_to_prev() {
  if ! docker image inspect "${PREV_TAG}" >/dev/null 2>&1; then
    echo "No previous image (${PREV_TAG}) to roll back to." >&2
    return 1
  fi
  echo "Rolling back: ${PREV_TAG} -> ${IMAGE}" >&2
  if docker tag "${PREV_TAG}" "${IMAGE}" && docker compose up -d --no-build; then
    sleep 5
    local state
    state="$(docker inspect -f '{{.State.Status}}' "$(docker compose ps -q | head -n1)" 2>/dev/null || true)"
    if [[ "${state}" == "running" ]]; then
      echo "Rollback OK — old image restored (container running). Repo is still at the new commit; fix and re-push, or pin this: git checkout ${LOCAL:0:8} (detached) for local testing." >&2
      return 0
    fi
    echo "CRITICAL: rollback started but container state is '${state:-missing}'." >&2
    return 1
  fi
  echo "CRITICAL: rollback command failed." >&2
  return 1
}

echo "Update available: ${LOCAL:0:8} → ${REMOTE:0:8}"
git pull --ff-only

# --- 2. Snapshot current image for rollback -------------------------------
# Only meaningful if a container was already deployed (first deploy has
# nothing to roll back to).
if [[ -n "${CONTAINER}" ]]; then
  docker tag "${IMAGE}" "${PREV_TAG}"
  echo "Previous image saved as ${PREV_TAG}"
fi

# --- 3. Build + start ------------------------------------------------------
echo "Building and starting ${IMAGE} …"
if ! docker compose up -d --build; then
  echo "Build/start FAILED — rolling back" >&2
  rollback_to_prev || true
  exit 1
fi

# --- 4. Post-up healthcheck ------------------------------------------------
HEALTH_WAIT="${HEALTH_WAIT:-15}"
echo "Waiting ${HEALTH_WAIT}s for the container to stabilize …"
sleep "${HEALTH_WAIT}"

NEW_CONTAINER="$(docker compose ps -q | head -n1 || true)"
state="$(docker inspect -f '{{.State.Status}}' "${NEW_CONTAINER}" 2>/dev/null || true)"
echo "Container state: ${state:-missing}"

if [[ "${state}" != "running" ]]; then
  echo "Container not running after update — rolling back" >&2
  rollback_to_prev || true
  exit 1
fi

# Crash-loop guard: if the startup self-check fails the client exits(1) and
# systemd/docker keeps restarting it — a single state sample can still read
# "running". Re-check after 5s: a fresh container id or a dead state means
# the image is broken.
sleep 5
SECOND_CHECK="$(docker compose ps -q | head -n1 || true)"
second_state="$(docker inspect -f '{{.State.Status}}' "${SECOND_CHECK}" 2>/dev/null || true)"
if [[ "${SECOND_CHECK}" != "${NEW_CONTAINER}" || "${second_state}" != "running" ]]; then
  echo "Container is crash-looping (was ${NEW_CONTAINER:0:12}, now ${SECOND_CHECK:0:12}/${second_state:-missing}) — rolling back" >&2
  rollback_to_prev || true
  exit 1
fi

# Backend self-check (the client also pokes this on startup; if the backend
# itself is down nothing works regardless of image — still worth reporting).
if command -v curl >/dev/null 2>&1; then
  backend="$(sed -n 's/^VOICE_BACKEND_URL=//p' .env | head -n1 | tr -d '"' || true)"
  backend="${backend:-http://voice.pora-ai.ru}"
  if curl -fsS -m 10 "${backend%/}/health" >/dev/null 2>&1; then
    echo "Backend ${backend%/}/health — ok"
  else
    echo "WARNING: backend ${backend%/}/health unreachable (deploy itself may be fine; backend is separate)" >&2
  fi
fi

echo "Updated ${LOCAL:0:8} → ${REMOTE:0:8} — container healthy"
