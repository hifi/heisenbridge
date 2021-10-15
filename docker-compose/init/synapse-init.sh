#!/bin/sh

## first generate homeserver config
if [ ! -f /data/homeserver.yaml ]; then
    /start.py generate
    cat >> /data/homeserver.yaml <<EOS

app_service_config_files:
  - /data/heisenbridge.yaml
enable_registration: true
EOS
fi

while [ ! -f /data/heisenbridge.yaml ]; do
    echo "Waiting for /data/heisenbridge.yaml..."
    sleep 1
done

/start.py $*
