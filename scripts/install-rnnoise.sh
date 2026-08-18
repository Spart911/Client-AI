#!/usr/bin/env bash
# Build classic Xiph RNNoise v0.1.1 (small GRU, weights in-tree).
# Current rnnoise master pulls a ~56MB model — too heavy for Pi 3B.
#
# RNNOISE_SKIP_APT=1 — caller already installed wget/autoconf/gcc (Docker builder).
# Shared objects are copied to RNNOISE_STAGE_DIR for the runtime image.
set -euo pipefail

VER="${RNNOISE_VERSION:-0.1.1}"
URL="https://github.com/xiph/rnnoise/archive/refs/tags/v${VER}.tar.gz"
SRC=/tmp/rnnoise-src
STAGE="${RNNOISE_STAGE_DIR:-/opt/rnnoise-lib}"

if [[ "${RNNOISE_SKIP_APT:-}" != "1" ]]; then
  apt-get update
  apt-get install -y --no-install-recommends \
    wget ca-certificates autoconf automake libtool make gcc
fi

rm -rf "${SRC}"
mkdir -p "${SRC}"
wget -O /tmp/rnnoise.tgz "${URL}"
tar xf /tmp/rnnoise.tgz -C "${SRC}" --strip-components=1
cd "${SRC}"
./autogen.sh
./configure --prefix=/usr --disable-examples --disable-doc
make -j"$(nproc 2>/dev/null || echo 2)"
make install
ldconfig

mkdir -p "${STAGE}"
copied=0
for f in /usr/lib/librnnoise.so* /usr/lib/*/librnnoise.so* /lib/librnnoise.so* /lib/*/librnnoise.so*; do
  if [[ -e "${f}" ]]; then
    cp -a "${f}" "${STAGE}/"
    copied=1
  fi
done
if [[ "${copied}" -eq 0 ]]; then
  echo "install-rnnoise: librnnoise.so not found after make install" >&2
  exit 1
fi

cd /
rm -rf "${SRC}" /tmp/rnnoise.tgz

if [[ "${RNNOISE_SKIP_APT:-}" != "1" ]]; then
  apt-get purge -y autoconf automake libtool
  apt-get autoremove -y --purge
  rm -rf /var/lib/apt/lists/*
fi

echo "install-rnnoise: librnnoise v${VER} staged in ${STAGE}"
