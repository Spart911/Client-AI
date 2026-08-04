#!/bin/sh
# Fix volume ownership, then run the client as user pi.
set -eu
mkdir -p /home/pi/.cache/vosk
chown -R pi:pi /home/pi/.cache
exec runuser -u pi -- "$@"
