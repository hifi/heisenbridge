#!/bin/sh

cd /app
pip install -e .[dev]

if [ ! -f /data/heisenbridge.yaml ]; then
    python -m heisenbridge -c /data/heisenbridge.yaml --generate --listen-address heisenbridge
fi

python -u -m heisenbridge -c /data/heisenbridge.yaml --listen-address 0.0.0.0 http://synapse:8008
