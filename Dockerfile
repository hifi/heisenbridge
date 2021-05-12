FROM python:3.9-slim

WORKDIR /opt/heisenbridge
COPY . .

# install deps and run a sanity check
RUN pip install -e . && \
    python -m heisenbridge  -h

# expose and enable identd by default, if it's not published it doesn't matter
EXPOSE 9898/tcp 113/tcp
ENTRYPOINT ["/usr/local/bin/python", "-m", "heisenbridge", "-i"]
CMD []
