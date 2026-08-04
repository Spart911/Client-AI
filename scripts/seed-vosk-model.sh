#!/usr/bin/env bash
# Seed Vosk model into Docker volume (when download hangs inside the container).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

URL="https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
ZIP="/tmp/vosk-model-small-ru-0.22.zip"
DIRNAME="vosk-model-small-ru-0.22"

if [[ ! -f "${ZIP}" ]] || [[ "$(stat -c%s "${ZIP}" 2>/dev/null || stat -f%z "${ZIP}")" -lt 1000000 ]]; then
  echo "==> Downloading ${URL}"
  echo "    (If this stalls on Pi: download on Mac and scp to ${ZIP})"
  curl -fL --retry 8 --retry-all-errors --connect-timeout 20 --max-time 600 \
    -o "${ZIP}" "${URL}"
fi

echo "==> Stopping container"
sudo docker compose stop || true

VOL="$(sudo docker volume inspect client-ai_vosk-cache -f '{{.Mountpoint}}' 2>/dev/null || true)"
if [[ -z "${VOL}" ]]; then
  VOL_NAME="$(sudo docker volume ls -q | grep -E 'vosk-cache$' | head -1 || true)"
  if [[ -n "${VOL_NAME}" ]]; then
    VOL="$(sudo docker volume inspect "${VOL_NAME}" -f '{{.Mountpoint}}')"
  fi
fi
if [[ -z "${VOL}" ]]; then
  echo "vosk-cache volume not found — run: sudo docker compose up -d" >&2
  exit 1
fi

echo "==> Extracting into ${VOL}"
sudo mkdir -p "${VOL}"
sudo unzip -o "${ZIP}" -d "${VOL}"
sudo chown -R 1000:1000 "${VOL}"

if [[ ! -d "${VOL}/${DIRNAME}" ]]; then
  echo "Extract failed, expected ${VOL}/${DIRNAME}" >&2
  exit 1
fi

echo "==> Starting container"
sudo docker compose up -d
echo "Done. Logs: sudo docker compose logs -f"
