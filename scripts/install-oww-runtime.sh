#!/usr/bin/env bash
# Install openWakeWord + an inference runtime that works on this CPU.
#
# openwakeword declares a hard dependency on onnxruntime, which has no
# linux/armv7 wheels. On those boards we install openwakeword with --no-deps
# and use tflite-runtime instead.
set -euo pipefail

arch="$(uname -m)"
echo "install-oww-runtime: arch=${arch}"

try_pip() {
  echo "Trying: pip install $*"
  if pip install "$@"; then
    return 0
  fi
  return 1
}

# App deps (no openwakeword yet — it pulls onnxruntime).
try_pip -r /app/requirements.txt

# openWakeWord runtime extras (normally pulled in by the package).
try_pip "tqdm>=4,<5" "scikit-learn>=1,<2" "requests>=2,<3"

have_onnx=0
have_tflite=0

if try_pip "onnxruntime>=1.10.0,<2"; then
  have_onnx=1
  echo "Installed onnxruntime"
fi

if try_pip "tflite-runtime>=2.8.0,<3" \
  || try_pip --extra-index-url "https://google-coral.github.io/py-repo/" "tflite_runtime"; then
  have_tflite=1
  echo "Installed tflite-runtime"
fi

if [[ "${have_onnx}" -eq 0 && "${have_tflite}" -eq 0 ]]; then
  echo "ERROR: need onnxruntime or tflite-runtime; neither installed on ${arch}." >&2
  echo "Prefer Raspberry Pi OS 64-bit (arm64) for onnxruntime wheels." >&2
  exit 1
fi

if [[ "${have_onnx}" -eq 1 ]]; then
  # Normal install resolves onnxruntime from our already-installed copy.
  try_pip "openwakeword>=0.6.0"
else
  echo "Installing openwakeword with --no-deps (no onnxruntime on ${arch})"
  try_pip --no-deps "openwakeword>=0.6.0"
fi

python - <<'PY'
import openwakeword
print("openwakeword", getattr(openwakeword, "__version__", "?"))
try:
    import onnxruntime as ort
    print("onnxruntime", ort.__version__)
except Exception as exc:
    print("onnxruntime missing:", exc)
try:
    import tflite_runtime
    print("tflite_runtime ok", getattr(tflite_runtime, "__version__", ""))
except Exception as exc:
    print("tflite_runtime missing:", exc)
openwakeword.utils.download_models()
print("models downloaded")
PY

echo "install-oww-runtime: done"
