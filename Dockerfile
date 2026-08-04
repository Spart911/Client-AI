# Raspberry Pi voice client — slim image for Pi 3B (arm/v7 or arm64)
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # piwheels: prebuilt wheels for armv7 (Pi 3B 32-bit) — avoids compiling numpy/cffi
    PIP_EXTRA_INDEX_URL=https://www.piwheels.org/simple \
    HOME=/home/pi \
    VOSK_MODEL_PATH=/opt/vosk/vosk-model-small-ru-0.22 \
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
      unzip \
      gcc \
      g++ \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 29 audio 2>/dev/null || true \
    && useradd -m -u 1000 -G audio pi

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Model is vendored in git (models/) — no download from alphacephei at build/runtime.
COPY models/vosk-model-small-ru-0.22.zip /tmp/vosk.zip
RUN mkdir -p /opt/vosk \
    && unzip -q /tmp/vosk.zip -d /opt/vosk \
    && rm -f /tmp/vosk.zip

COPY asound.conf /etc/asound.conf
COPY pi_assistant.py .
RUN chown -R pi:pi /home/pi /app

USER pi

CMD ["python", "pi_assistant.py"]
