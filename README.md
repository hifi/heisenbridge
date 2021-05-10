Heisenbridge
============

a bouncer-style Matrix IRC bridge.

Heisenbridge brings IRC to Matrix by creating an environment where every user connects to each network individually like they would with a traditional IRC bouncer.
Simplicity is achieved by exposing IRC in the most straightforward way as possible where it makes sense so it feels familiar for long time IRC users and prevents hiding protocol level events to help diagnose integration issues.

These compromises also mean that some IRC or Matrix features are managed with text commands rather than using the Matrix UI directly and some Matrix features that may seem to match IRC are not available anyway.
Users on IRC should not know you are using Matrix unless you send media that is linked from your homeserver.

Please file an issue when you find something is missing or isn't working that you'd like to see fixed. Also bear in mind this project is still in very early stages of development so many things are missing or outright broken. Pull requests are more than welcome!

Support and development discussion in [#heisenbridge:vi.fi](https://matrix.to/#/#heisenbridge:vi.fi)

Use [matrix-appservice-irc](https://github.com/matrix-org/matrix-appservice-irc) instead if you need to plumb rooms between Matrix and IRC seamlessly or need to connect large amounts of users.

Features
--------
* "zero configuration" - no databases or storage required
* brings IRC to Matrix rather than Matrix to IRC - not annoying to folks on IRC
* completely managed through admin room - just DM `@Heisenbridge`!
* channel management through bridge bot - type `Heisenbridge: help` to get started!
* online help within Matrix
* access control for local and federated users
* fully puppeted users from IRC, they come and go as they would on Matrix
* tested with up to 1600 users in a single channel
* IRCnet !channels _are_ supported, you're welcome
* any number of IRC networks and users technically possible
* channel customization by setting the name and avatar
* TLS support for networks that have it
* customizable ident support
* long message splitting directly to IRC
* automatic identify/auth with server password or command on connect

Docker
------
The master branch is automatically published to [Docker Hub](https://hub.docker.com/r/hif1/heisenbridge):
```
docker pull hif1/heisenbridge
docker run --rm hif1/heisenbridge -h
```

Usage
-----
```
usage: python -m heisenbridge [-h] [-v] -c CONFIG [-l LISTEN_ADDRESS]
                              [-p LISTEN_PORT] [-u UID] [-g GID] [-i]
                              [--generate] [--reset] [-o OWNER]
                              [homeserver]

a Matrix IRC bridge

positional arguments:
  homeserver            URL of Matrix homeserver (default:
                        http://localhost:8008)

optional arguments:
  -h, --help            show this help message and exit
  -v, --verbose         logging verbosity level: once is info, twice is debug
                        (default: 0)
  -c CONFIG, --config CONFIG
                        registration YAML file path, must be writable if
                        generating (default: None)
  -l LISTEN_ADDRESS, --listen-address LISTEN_ADDRESS
                        bridge listen address (default: 127.0.0.1)
  -p LISTEN_PORT, --listen-port LISTEN_PORT
                        bridge listen port (default: 9898)
  -u UID, --uid UID     user id to run as (default: None)
  -g GID, --gid GID     group id to run as (default: None)
  -i, --identd          enable identd on TCP port 113, requires root (default:
                        False)
  --generate            generate registration YAML for Matrix homeserver
  --reset               reset ALL bridge configuration from homeserver and
                        exit
  -o OWNER, --owner OWNER
                        set owner MXID (eg: @user:homeserver) or first talking
                        local user will claim the bridge (default: None)
```

Install
-------

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
