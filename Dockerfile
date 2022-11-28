FROM docker.io/alpine:3.17.0

RUN apk add --no-cache python3 py3-setuptools py3-pip py3-ruamel.yaml.clib

WORKDIR /opt/heisenbridge
COPY . .

# install deps and run a sanity check
RUN python setup.py gen_version && \
    rm -rf .git && \
    pip install -e . && \
    python -m heisenbridge  -h

# identd also needs to be enabled with --identd in CMD
EXPOSE 9898/tcp 113/tcp
ENTRYPOINT ["heisenbridge", "-l", "0.0.0.0"]
CMD []
