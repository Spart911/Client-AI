#!/usr/bin/env bash
# Build classic Xiph RNNoise v0.1.1 (small GRU, weights in-tree).
# Current rnnoise master pulls a ~56MB model — too heavy for Pi 3B.
set -euo pipefail

VER="${RNNOISE_VERSION:-0.1.1}"
URL="https://github.com/xiph/rnnoise/archive/refs/tags/v${VER}.tar.gz"
SRC=/tmp/rnnoise-src

apt-get update
apt-get install -y --no-install-recommends \
  wget ca-certificates autoconf automake libtool make gcc

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

cd /
rm -rf "${SRC}" /tmp/rnnoise.tgz
apt-get purge -y autoconf automake libtool
apt-get autoremove -y --purge
rm -rf /var/lib/apt/lists/*

echo "install-rnnoise: librnnoise v${VER} installed"
