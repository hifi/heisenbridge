FROM python:3.9-slim

WORKDIR /opt/heisenbridge
COPY . .

# install deps and run a sanity check
RUN pip install -e . && \
    python -m heisenbridge  -h

# identd also needs to be enabled with --identd in CMD
EXPOSE 9898/tcp 113/tcp
ENTRYPOINT ["/usr/local/bin/python", "-m", "heisenbridge"]
CMD []
