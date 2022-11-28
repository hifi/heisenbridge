FROM docker.io/alpine:3.17.0

# install runtime dependencies
RUN apk add --no-cache python3 py3-ruamel.yaml.clib

WORKDIR /opt/heisenbridge
COPY . .

# install deps and run a sanity check
RUN apk add --no-cache --virtual build-dependencies py3-setuptools py3-pip && \
    python setup.py gen_version && \
    rm -rf .git && \
    pip install -e . && \
    apk del build-dependencies && \
    python -m heisenbridge  -h

# identd also needs to be enabled with --identd in CMD
EXPOSE 9898/tcp 113/tcp
ENTRYPOINT ["heisenbridge", "-l", "0.0.0.0"]
CMD []
