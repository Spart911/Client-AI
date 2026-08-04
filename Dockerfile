# Raspberry Pi voice client — slim image for Pi 3B (arm/v7 or arm64)
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # piwheels: prebuilt wheels for armv7 (Pi 3B 32-bit) — avoids compiling numpy/cffi
    PIP_EXTRA_INDEX_URL=https://www.piwheels.org/simple \
    HOME=/home/pi

RUN apt-get update && apt-get install -y --no-install-recommends \
      portaudio19-dev \
      libportaudio2 \
      libffi-dev \
      pkg-config \
      libatlas3-base \
      mpv \
      alsa-utils \
      ca-certificates \
      gcc \
      g++ \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 29 audio 2>/dev/null || true \
    && useradd -m -u 1000 -G audio pi

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY pi_assistant.py docker-entrypoint.sh ./
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /home/pi/.cache/vosk \
    && chown -R pi:pi /home/pi /app

# Root only to chown the vosk volume, then drop to user pi.
USER root
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "pi_assistant.py"]
