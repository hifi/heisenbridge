Heisenbridge
============

a work-in-progress Matrix IRC bridge.

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
