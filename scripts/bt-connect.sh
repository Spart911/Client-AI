#!/usr/bin/env bash
# Connect a paired Bluetooth speaker/headset and route Pulse/PipeWire to it.
#
# Config from repo .env (or environment):
#   BT_DEVICE_MAC=AA:BB:CC:DD:EE:FF     required
#   BT_PROFILE=handsfree_head_unit     HFP mic+speaker (default)
#                                      use a2dp_sink for speaker-only
#   BT_CONNECT_INTERVAL=15             seconds between reconnect attempts
#
# Usage:
#   BT_DEVICE_MAC=AA:BB:CC:DD:EE:FF bash scripts/bt-connect.sh
#   # or once .env has BT_DEVICE_MAC:
#   bash scripts/bt-connect.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${SCRIPT_DIR}/.env}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  # Only pull BT_* / PULSE_* lines — avoid eval surprises from unrelated values.
  while IFS= read -r line || [[ -n "${line}" ]]; do
    case "${line}" in
      BT_*|PULSE_*)
        # strip CR, skip comments/empty already filtered by case
        line="${line%$'\r'}"
        key="${line%%=*}"
        val="${line#*=}"
        export "${key}=${val}"
        ;;
    esac
  done < <(grep -E '^(BT_|PULSE_)' "${ENV_FILE}" || true)
  set +a
fi

MAC="${BT_DEVICE_MAC:-${BT_MAC:-}}"
PROFILE="${BT_PROFILE:-handsfree_head_unit}"
INTERVAL="${BT_CONNECT_INTERVAL:-15}"
ONCE="${BT_CONNECT_ONCE:-false}"

if [[ -z "${MAC}" ]]; then
  echo "BT_DEVICE_MAC is not set. Put it in .env, e.g.:" >&2
  echo "  BT_DEVICE_MAC=AA:BB:CC:DD:EE:FF" >&2
  echo "Find MAC: bluetoothctl devices" >&2
  exit 1
fi

# Normalize MAC / card id: AA:BB:CC:DD:EE:FF → AA_BB_CC_DD_EE_FF
mac_norm="$(echo "${MAC}" | tr '[:lower:]' '[:upper:]' | tr -d ' ')"
card_id="bluez_card.$(echo "${mac_norm}" | tr ':' '_')"

log() {
  echo "$(date -Iseconds) bt-connect: $*"
}

bt_connected() {
  bluetoothctl info "${mac_norm}" 2>/dev/null | grep -q "Connected: yes"
}

ensure_adapter() {
  bluetoothctl power on >/dev/null 2>&1 || true
  bluetoothctl agent on >/dev/null 2>&1 || true
  bluetoothctl default-agent >/dev/null 2>&1 || true
}

trust_device() {
  bluetoothctl trust "${mac_norm}" >/dev/null 2>&1 || true
}

try_connect() {
  ensure_adapter
  trust_device
  if bt_connected; then
    return 0
  fi
  log "connecting ${mac_norm} …"
  bluetoothctl connect "${mac_norm}" >/dev/null 2>&1 || true
  # BlueZ / Pulse often need a few seconds after HCI connect.
  for _ in 1 2 3 4 5 6; do
    sleep 1
    if bt_connected; then
      return 0
    fi
  done
  return 1
}

wait_card() {
  local i
  for i in $(seq 1 20); do
    if pactl list cards short 2>/dev/null | grep -q "${card_id}"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

apply_pulse() {
  if ! command -v pactl >/dev/null 2>&1; then
    log "pactl not found — skip Pulse routing"
    return 0
  fi
  if ! wait_card; then
    log "Pulse card ${card_id} not ready yet"
    return 1
  fi

  log "profile ${card_id} → ${PROFILE}"
  pactl set-card-profile "${card_id}" "${PROFILE}" 2>/dev/null || {
    log "failed to set profile ${PROFILE} (is the device HFP-capable?)"
    return 1
  }
  sleep 1

  # Prefer sinks/sources that belong to this card + profile.
  local sink source
  sink="$(pactl list short sinks 2>/dev/null | awk -v id="${card_id#bluez_card.}" '
    $2 ~ id { print $2; exit }
  ' || true)"
  # Fallback: any bluez sink for this MAC
  if [[ -z "${sink}" ]]; then
    sink="$(pactl list short sinks 2>/dev/null | awk -v m="$(echo "${mac_norm}" | tr ':' '_')" '
      $2 ~ m { print $2; exit }
    ' || true)"
  fi
  source="$(pactl list short sources 2>/dev/null | awk -v m="$(echo "${mac_norm}" | tr ':' '_')" '
    $2 ~ m && $2 !~ /\.monitor$/ { print $2; exit }
  ' || true)"

  if [[ -n "${sink}" ]]; then
    log "default sink → ${sink}"
    pactl set-default-sink "${sink}" || true
  fi
  if [[ -n "${source}" ]]; then
    log "default source → ${source}"
    pactl set-default-source "${source}" || true
  elif [[ "${PROFILE}" == "a2dp_sink" ]]; then
    log "A2DP has no mic — keep existing default source"
  fi
  return 0
}

cycle() {
  if try_connect; then
    apply_pulse || true
    log "ok (connected=$(bt_connected && echo yes || echo no))"
  else
    log "connect failed — will retry"
  fi
}

log "MAC=${mac_norm} profile=${PROFILE} interval=${INTERVAL}s"
cycle

if [[ "${ONCE}" == "true" ]]; then
  exit 0
fi

while true; do
  sleep "${INTERVAL}"
  cycle
done
