Heisenbridge
============

a work-in-progress Matrix IRC bridge.

Quicker Start
-------------
`docker-compose up` and point your browser to [localhost](http://localhost), register yourself and talk with `@heisenbridge:localhost`.
You may need to `docker-compose restart heisenbridge` if the first time init was slower than Synapse as it doesn't yet revive connection automatically.
You can also connect your IRC client to irc://localhost:6667 for testing the other side.

Quick Start
-----------
1. Install Python 3.6 or newer
2. Install dependencies in virtualenv
   ```
   virtualenv venv
   source venv/bin/activate
   pip install -e .[dev,test]
   ```
3. Generate registration YAML
   ```
   python -m heisenbridge -c /path/to/synapse/config/heisenbridge.yaml --generate
   ```
4. Add `heisenbridge.yaml` to Synapse appservice list
5. (Re)start Synapse
6. Start Heisenbridge
   ```
   python -m heisenbridge -c /path/to/synapse/config/heisenbridge.yaml
   ```
7. Start a DM with `@heisenbridge:your.homeserver` to get online usage help
