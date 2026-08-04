# Raspberry Pi voice client — slim image for Pi 3B (arm/v7 or arm64)
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/pi \
    VOSK_MODEL_PATH=/opt/vosk/vosk-model-small-ru-0.22

RUN apt-get update && apt-get install -y --no-install-recommends \
      portaudio19-dev \
      libportaudio2 \
      libatlas3-base \
      mpv \
      alsa-utils \
      curl \
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

# Prefetch small RU Vosk model at build (~50MB) — no download on first boot.
RUN mkdir -p /opt/vosk \
    && curl -fsSL -o /tmp/vosk.zip \
         "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip" \
    && unzip -q /tmp/vosk.zip -d /opt/vosk \
    && rm -f /tmp/vosk.zip

COPY pi_assistant.py .
RUN chown -R pi:pi /home/pi /app

USER pi

CMD ["python", "pi_assistant.py"]
