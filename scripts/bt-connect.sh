#!/usr/bin/env bash
# Connect a paired Bluetooth speaker/headset and route Pulse/PipeWire to it.
#
# Config from repo .env (or environment):
#   BT_DEVICE_MAC=AA:BB:CC:DD:EE:FF     required
#   BT_PROFILE=handsfree_head_unit     HFP mic+speaker (default)
#                                      use a2dp_sink for speaker-only
#   BT_CONNECT_INTERVAL=15             seconds between reconnect attempts
#   BT_KEEPALIVE_SEC=45                quiet blip so speaker won't sleep
#                                      (0 = disable). Many A2DP speakers sleep
#                                      after a few minutes of real silence.
#                                      Use mid-band tone: HFP filters out ~40 Hz.
#   BT_KEEPALIVE_VOL=3500              paplay 0..65536; loud enough to keep amp
#                                      awake, still below room-wake for USB mic.
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
# Default: blip every 45s — A2DP speakers often sleep well before 10–15 min.
KEEPALIVE_SEC="${BT_KEEPALIVE_SEC:-45}"
KEEPALIVE_VOL="${BT_KEEPALIVE_VOL:-3500}"  # paplay 0..65536; ~5% — keeps amp awake
KEEPALIVE_WAV="${BT_KEEPALIVE_WAV:-/tmp/voice-bt-keepalive-v4.wav}"
_last_keepalive_ts=0
LOG_FILE="${BT_LOG_FILE:-${SCRIPT_DIR}/logs/bt-connect.log}"
mkdir -p "$(dirname "${LOG_FILE}")"

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
  local line
  line="$(date -Iseconds) bt-connect: $*"
  echo "${line}"
  # File log survives RPi volatile journal (40-rpi-volatile-storage.conf).
  echo "${line}" >> "${LOG_FILE}" 2>/dev/null || true
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

current_card_profile() {
  # Active Profile: handsfree_head_unit
  pactl list cards 2>/dev/null | awk -v id="${card_id}" '
    $0 ~ "Name: " id { found=1 }
    found && /Active Profile:/ { print $3; exit }
  '
}

pulse_defaults_ok() {
  local sink source def_sink def_source mac_us
  mac_us="$(echo "${mac_norm}" | tr ':' '_')"
  def_sink="$(pactl get-default-sink 2>/dev/null || true)"
  def_source="$(pactl get-default-source 2>/dev/null || true)"
  [[ "${def_sink}" == *"${mac_us}"* ]] || return 1
  if [[ "${PROFILE}" == "a2dp_sink" ]]; then
    if [[ -n "${PULSE_DEFAULT_SOURCE:-}" ]]; then
      [[ "${def_source}" == "${PULSE_DEFAULT_SOURCE}" ]] || return 1
    fi
    return 0
  fi
  [[ "${def_source}" == *"${mac_us}"* ]] || return 1
  return 0
}

apply_pulse() {
  local force="${1:-false}"
  if ! command -v pactl >/dev/null 2>&1; then
    log "pactl not found — skip Pulse routing"
    return 0
  fi
  if ! wait_card; then
    log "Pulse card ${card_id} not ready yet"
    return 1
  fi

  local active
  active="$(current_card_profile || true)"
  if [[ "${force}" != "true" && "${active}" == "${PROFILE}" ]] && pulse_defaults_ok; then
    # Already routed — do not re-set profile (can glitch/disconnect some speakers).
    return 0
  fi

  if [[ "${active}" != "${PROFILE}" ]]; then
    log "profile ${card_id} → ${PROFILE} (was: ${active:-none})"
    pactl set-card-profile "${card_id}" "${PROFILE}" 2>/dev/null || {
      log "failed to set profile ${PROFILE} (is the device HFP-capable?)"
      return 1
    }
    sleep 1
  fi

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
    local def_sink
    def_sink="$(pactl get-default-sink 2>/dev/null || true)"
    if [[ "${def_sink}" != "${sink}" ]]; then
      log "default sink → ${sink}"
      pactl set-default-sink "${sink}" || true
    fi
  fi
  if [[ "${PROFILE}" == "a2dp_sink" ]]; then
    local usb_source="${PULSE_DEFAULT_SOURCE:-}"
    if [[ -n "${usb_source}" ]]; then
      local def_source
      def_source="$(pactl get-default-source 2>/dev/null || true)"
      if [[ "${def_source}" != "${usb_source}" ]]; then
        log "default source → ${usb_source} (USB, A2DP has no mic)"
        pactl set-default-source "${usb_source}" || true
      fi
    else
      log "A2DP has no mic — keep existing default source"
    fi
  elif [[ -n "${source}" ]]; then
    local def_source
    def_source="$(pactl get-default-source 2>/dev/null || true)"
    if [[ "${def_source}" != "${source}" ]]; then
      log "default source → ${source}"
      pactl set-default-source "${source}" || true
    fi
    mic_vol="${BT_MIC_VOLUME:-200%}"
    if [[ -n "${mic_vol}" ]]; then
      pactl set-source-volume "${source}" "${mic_vol}" || true
    fi
  fi
  return 0
}

ensure_keepalive_wav() {
  # HFP is ~300–3400 Hz: a 40 Hz tone never reaches the speaker amp.
  # Soft 880 Hz / ~200 ms — long enough for A2DP idle detection, short for wake.
  if [[ -f "${KEEPALIVE_WAV}" ]]; then
    return 0
  fi
  python3 - "${KEEPALIVE_WAV}" <<'PY'
import math, struct, sys, wave

path = sys.argv[1]
rate, dur, freq, amp = 16000, 0.20, 880.0, 2800  # amp out of 32767 (~8.5%)
n = max(1, int(rate * dur))
with wave.open(path, "w") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    frames = bytearray()
    for i in range(n):
        # Raised-cosine envelope — no click.
        env = math.sin(math.pi * i / max(1, n - 1))
        sample = int(amp * env * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", max(-32767, min(32767, sample)))
    w.writeframes(frames)
PY
}

sink_has_other_audio() {
  # Skip keepalive while TTS / music already plays on any sink.
  local n
  n="$(pactl list short sink-inputs 2>/dev/null | wc -l | tr -d ' ')"
  [[ "${n}" -gt 0 ]]
}

maybe_keepalive() {
  local sec now
  sec="${KEEPALIVE_SEC}"
  if [[ -z "${sec}" || "${sec}" == "0" ]]; then
    return 0
  fi
  if ! bt_connected; then
    return 0
  fi
  now="$(date +%s)"
  if (( now - _last_keepalive_ts < sec )); then
    return 0
  fi
  if sink_has_other_audio; then
    # Real audio already keeps the speaker awake — just refresh the timer.
    _last_keepalive_ts="${now}"
    return 0
  fi
  if ! command -v paplay >/dev/null 2>&1; then
    log "paplay missing — keepalive disabled"
    KEEPALIVE_SEC=0
    return 0
  fi
  ensure_keepalive_wav || {
    log "failed to build keepalive wav"
    return 0
  }
  local sink vol rc
  # Prefer the A2DP sink even if Pulse default drifted to HDMI/analog.
  sink="$(pactl get-default-sink 2>/dev/null || true)"
  if [[ "${PROFILE}" == "a2dp_sink" ]]; then
    local mac_us want
    mac_us="$(echo "${mac_norm}" | tr 'a-f' 'A-F' | tr ':' '_')"
    want="bluez_sink.${mac_us}.a2dp_sink"
    if pactl list short sinks 2>/dev/null | grep -q "${want}"; then
      sink="${want}"
      pactl set-default-sink "${want}" >/dev/null 2>&1 || true
    fi
  fi
  # Suspended bluez sinks swallow paplay with rc=0 and no RF audio.
  if [[ -n "${sink}" ]]; then
    pactl suspend-sink "${sink}" 0 >/dev/null 2>&1 || true
  fi
  vol="${KEEPALIVE_VOL}"
  # A2DP + USB mic: keep blip modest so wake doesn't latch on the tone.
  if [[ "${PROFILE}" == "a2dp_sink" ]] && [[ "${vol}" =~ ^[0-9]+$ ]] && (( vol > 5000 )); then
    log "keepalive vol ${vol}→5000 (a2dp wake-safe cap)"
    vol=5000
  fi
  # paplay --volume: 0..65536
  if [[ -n "${sink}" ]]; then
    paplay --device="${sink}" --volume="${vol}" "${KEEPALIVE_WAV}" >/dev/null 2>&1
    rc=$?
  else
    paplay --volume="${vol}" "${KEEPALIVE_WAV}" >/dev/null 2>&1
    rc=$?
  fi
  _last_keepalive_ts="${now}"
  if [[ "${rc}" -ne 0 ]]; then
    log "keepalive paplay failed rc=${rc} sink=${sink:-none}"
  else
    log "keepalive blip (vol=${vol} sink=${sink:-default})"
  fi
}

cycle() {
  local was_connected=false
  if bt_connected; then
    was_connected=true
  fi
  if try_connect; then
    if [[ "${was_connected}" == "true" ]]; then
      apply_pulse false || true
    else
      # Fresh (re)connect — force profile + routing.
      apply_pulse true || true
      log "ok (connected=yes, reconnected)"
      _last_keepalive_ts="$(date +%s)"
      return 0
    fi
    maybe_keepalive || true
  else
    log "connect failed — will retry"
  fi
}

log "MAC=${mac_norm} profile=${PROFILE} interval=${INTERVAL}s keepalive=${KEEPALIVE_SEC}s vol=${KEEPALIVE_VOL}"
# Drop legacy keepalive samples if present (force rebuild of louder v4 tone).
rm -f /tmp/voice-bt-keepalive.wav /tmp/voice-bt-keepalive-v2.wav /tmp/voice-bt-keepalive-v3.wav 2>/dev/null || true
_last_keepalive_ts="$(date +%s)"
cycle

if [[ "${ONCE}" == "true" ]]; then
  exit 0
fi

while true; do
  sleep "${INTERVAL}"
  cycle
done
