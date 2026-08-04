#!/usr/bin/env bash
# Install openWakeWord + an inference runtime that works on this CPU.
#
# openwakeword declares a hard dependency on onnxruntime, which has no
# linux/armv7 wheels. On those boards we:
#   1) install tflite-runtime
#   2) install a tiny onnxruntime *stub* (import-only) so openwakeword can load
#   3) install openwakeword with --no-deps
# Wake inference then uses tflite (OWW_FRAMEWORK=tflite). Keep VAD off
# (OWW_VAD_THRESHOLD=0) — Silero VAD needs a real onnxruntime.
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

site_packages() {
  python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])'
}

install_onnxruntime_stub() {
  local sp
  sp="$(site_packages)"
  mkdir -p "${sp}/onnxruntime"
  cat > "${sp}/onnxruntime/__init__.py" <<'PY'
"""Import stub for platforms without an onnxruntime wheel (e.g. linux/armv7).

openwakeword imports onnxruntime at package import time (VAD helper). Wake
detection can still run via tflite-runtime when OWW_FRAMEWORK=tflite.
Do not enable Silero VAD (vad_threshold>0) with this stub.
"""

__version__ = "0.0.0+stub-armv7"


class InferenceSession:  # noqa: N801 — match onnxruntime API name
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "onnxruntime stub only: real InferenceSession is unavailable on this "
            "CPU/OS. Use OWW_FRAMEWORK=tflite and keep OWW_VAD_THRESHOLD=0."
        )


def get_available_providers():
    return []
PY
  # Make pip/pkg_resources believe a package named onnxruntime exists.
  mkdir -p "${sp}/onnxruntime-0.0.0+stub.dist-info"
  cat > "${sp}/onnxruntime-0.0.0+stub.dist-info/METADATA" <<'EOF'
Metadata-Version: 2.1
Name: onnxruntime
Version: 0.0.0+stub
Summary: Import stub for openwakeword on armv7
EOF
  : > "${sp}/onnxruntime-0.0.0+stub.dist-info/RECORD"
  echo "Installed onnxruntime import stub into ${sp}"
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

if [[ "${have_onnx}" -eq 0 ]]; then
  install_onnxruntime_stub
  echo "Installing openwakeword with --no-deps (stub onnxruntime on ${arch})"
  try_pip --no-deps "openwakeword>=0.6.0"
else
  try_pip "openwakeword>=0.6.0"
fi

python - <<'PY'
import openwakeword
print("openwakeword", getattr(openwakeword, "__version__", "?"))
try:
    import onnxruntime as ort
    print("onnxruntime", ort.__version__)
except Exception as exc:
    print("onnxruntime missing:", exc)
    raise
try:
    import tflite_runtime
    print("tflite_runtime ok", getattr(tflite_runtime, "__version__", ""))
except Exception as exc:
    print("tflite_runtime missing:", exc)
openwakeword.utils.download_models()
print("models downloaded")
PY

echo "install-oww-runtime: done"
