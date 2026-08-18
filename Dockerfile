# Raspberry Pi voice client — slim image for Pi 3B (arm/v7 or arm64)
#
# Builder has gcc/g++. Runtime keeps numpy/OpenBLAS, PortAudio, Pulse/mpv,
# and RNNoise v0.1.1. Wake is microWakeWord only (bundled TFLite C lib).

FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_EXTRA_INDEX_URL=https://www.piwheels.org/simple \
    HOME=/home/pi

RUN apt-get update && apt-get install -y --no-install-recommends \
      portaudio19-dev \
      libportaudio2 \
      libffi-dev \
      pkg-config \
      libopenblas0 \
      libgfortran5 \
      ca-certificates \
      gcc \
      g++ \
      wget \
      autoconf \
      automake \
      libtool \
      make \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 29 audio 2>/dev/null || true \
    && useradd -m -u 1000 -G audio pi

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Classic Xiph RNNoise v0.1.1 (small GRU). Not master (~56MB model).
COPY scripts/install-rnnoise.sh /tmp/install-rnnoise.sh
RUN chmod +x /tmp/install-rnnoise.sh \
    && RNNOISE_SKIP_APT=1 bash /tmp/install-rnnoise.sh \
    && rm -f /tmp/install-rnnoise.sh

# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/pi \
    PULSE_SERVER=unix:/run/user/1000/pulse/native \
    XDG_RUNTIME_DIR=/run/user/1000

RUN apt-get update && apt-get install -y --no-install-recommends \
      libportaudio2 \
      libopenblas0 \
      libgfortran5 \
      mpv \
      alsa-utils \
      libasound2-plugins \
      libpulse0 \
      pulseaudio-utils \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 29 audio 2>/dev/null || true \
    && useradd -m -u 1000 -G audio pi

COPY --from=builder /usr/local /usr/local
COPY --from=builder /opt/rnnoise-lib/ /usr/lib/
RUN ldconfig

WORKDIR /app
COPY asound.conf /etc/asound.conf
COPY pi_assistant.py .
COPY models ./models
RUN chown -R pi:pi /home/pi /app

USER pi

CMD ["python", "pi_assistant.py"]
