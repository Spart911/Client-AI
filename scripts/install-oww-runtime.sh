#!/usr/bin/env bash
# Install an inference runtime for openWakeWord.
# Prefer onnxruntime (arm64/x86). On linux/armv7 fall back to tflite-runtime.
set -euo pipefail

arch="$(uname -m)"
echo "install-oww-runtime: arch=${arch}"

try_pip() {
  echo "Trying: $*"
  if pip install "$@"; then
    return 0
  fi
  return 1
}

if try_pip "onnxruntime>=1.16.0"; then
  echo "Installed onnxruntime"
  # Prefer onnx when available (pi_assistant falls back either way).
  exit 0
fi

echo "onnxruntime not available for ${arch} — trying tflite-runtime"

# Official / piwheels names vary by platform and Python version.
if try_pip "tflite-runtime" \
  || try_pip --extra-index-url "https://google-coral.github.io/py-repo/" "tflite_runtime"; then
  echo "Installed tflite-runtime"
  exit 0
fi

echo "ERROR: neither onnxruntime nor tflite-runtime could be installed." >&2
echo "On Raspberry Pi 3 prefer Raspberry Pi OS 64-bit (arm64 has onnxruntime wheels)," >&2
echo "or install a matching tflite-runtime wheel manually." >&2
exit 1
