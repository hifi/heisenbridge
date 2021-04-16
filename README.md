Heisenbridge
============

a work-in-progress Matrix IRC bridge.

Install
-----------

1. Install Python 3.6 or newer
2. Install dependencies in virtualenv

   ```bash
   virtualenv venv
   source venv/bin/activate
   pip install git+https://github.com/hifi/heisenbridge
   ```

3. Generate registration YAML

   ```bash
   python -m heisenbridge -c /path/to/synapse/config/heisenbridge.yaml --generate
   ```

4. Add `heisenbridge.yaml` to Synapse appservice list
5. (Re)start Synapse
6. Start Heisenbridge

   ```bash
   python -m heisenbridge -c /path/to/synapse/config/heisenbridge.yaml
   ```

7. Start a DM with `@heisenbridge:your.homeserver` to get online usage help

To update your installation, run `pip install --upgrade git+https://github.com/hifi/heisenbridge`

Develop
-------

1. Install Python 3.6 or newer
2. Install dependencies

   ```bash
   virtualenv venv
   source venv/bin/activate
   pip install -e .[dev,test]
   ```

3. (Optional) Set up pre-commit hooks

   ```bash
   pre-commit install
   ```

The pre-commit hooks are run by the CI as well.
