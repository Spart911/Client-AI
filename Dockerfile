# Raspberry Pi voice client — slim image for Pi 3B (arm/v7 or arm64)
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # piwheels: prebuilt wheels for armv7 (Pi 3B 32-bit) — avoids compiling numpy/cffi
    PIP_EXTRA_INDEX_URL=https://www.piwheels.org/simple \
    HOME=/home/pi \
    OWW_MODEL=alexa \
    # armv7: no onnxruntime → tflite (install-oww-runtime.sh)
    OWW_FRAMEWORK=tflite \
    PULSE_SERVER=unix:/run/user/1000/pulse/native \
    XDG_RUNTIME_DIR=/run/user/1000

RUN apt-get update && apt-get install -y --no-install-recommends \
      portaudio19-dev \
      libportaudio2 \
      libffi-dev \
      pkg-config \
      libopenblas0 \
      libgfortran5 \
      mpv \
      alsa-utils \
      libasound2-plugins \
      libpulse0 \
      pulseaudio-utils \
      ca-certificates \
      gcc \
      g++ \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 29 audio 2>/dev/null || true \
    && useradd -m -u 1000 -G audio pi

WORKDIR /app

COPY requirements.txt .
COPY scripts/install-oww-runtime.sh /tmp/install-oww-runtime.sh
RUN chmod +x /tmp/install-oww-runtime.sh \
    && pip install --upgrade pip \
    && bash /tmp/install-oww-runtime.sh \
    && rm -f /tmp/install-oww-runtime.sh

# Classic Xiph RNNoise v0.1.1 (small GRU). Separate layer so pip cache survives.
COPY scripts/install-rnnoise.sh /tmp/install-rnnoise.sh
RUN chmod +x /tmp/install-rnnoise.sh \
    && bash /tmp/install-rnnoise.sh \
    && rm -f /tmp/install-rnnoise.sh

COPY asound.conf /etc/asound.conf
COPY pi_assistant.py .
COPY models ./models
RUN chown -R pi:pi /home/pi /app

USER pi

CMD ["python", "pi_assistant.py"]
