#!/usr/bin/env bash
# Stop the voice client, collect USB «Джарвис» clips, then start it again.
#
# Usage on the Pi:
#   bash scripts/collect-jarvis.sh              # positives → record/usb
#   bash scripts/collect-jarvis.sh --negatives  # room/TV noise → record/usb-negatives
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
mkdir -p record/usb record/usb-negatives

COOKIE="${HOME}/.config/pulse/cookie"
if [[ ! -f "${COOKIE}" ]]; then
  echo "Pulse cookie not found: ${COOKIE}" >&2
  exit 1
fi

echo "Stopping voice-client-pi (needs the USB mic)…"
docker stop voice-client-pi >/dev/null || true

HOST_SUBDIR="record/usb"
for arg in "$@"; do
  if [[ "${arg}" == "--negatives" ]]; then
    HOST_SUBDIR="record/usb-negatives"
  fi
done
HOST_RECORD_DIR="${ROOT}/${HOST_SUBDIR}"
mkdir -p "${HOST_RECORD_DIR}"

set +e
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --network host \
  --device /dev/snd \
  --group-add 29 \
  --env-file "${ROOT}/.env" \
  -e PULSE_SERVER=unix:/run/user/1000/pulse/native \
  -e XDG_RUNTIME_DIR=/run/user/1000 \
  -e HOME=/home/pi \
  -e HOST_RECORD_DIR="${HOST_RECORD_DIR}" \
  -v /run/user/1000/pulse:/run/user/1000/pulse \
  -v "${COOKIE}:/home/pi/.config/pulse/cookie:ro" \
  -v "${ROOT}/record:/app/record" \
  -v "${ROOT}/scripts/collect-jarvis.py:/app/scripts/collect-jarvis.py:ro" \
  voice-client-pi:latest \
  python /app/scripts/collect-jarvis.py "$@"
status=$?
set -e

echo
echo "Файлы на Pi: ${HOST_RECORD_DIR}"
ls -1 "${HOST_RECORD_DIR}" 2>/dev/null | tail -n 20 || true
echo "Starting voice-client-pi…"
docker start voice-client-pi >/dev/null
exit "${status}"
