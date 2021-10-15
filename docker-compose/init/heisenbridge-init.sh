#!/bin/sh

if [ ! -f /data/heisenbridge.yaml ]; then
    python -m heisenbridge -c /data/heisenbridge.yaml --generate --listen-address heisenbridge
fi

sleep 5 # wait a bit to avoid reconnect backoff during startup

python -m heisenbridge -c /data/heisenbridge.yaml --listen-address 0.0.0.0 http://homeserver:8008
