Heisenbridge
============

a bouncer-style Matrix IRC bridge.

<img align="right" width="220" height="250" src="https://raw.githubusercontent.com/hifi/heisenbridge/master/logo/heisenbridge-light-transparent.png">

Heisenbridge brings IRC to Matrix by creating an environment where every user connects to each network individually like they would with a traditional IRC bouncer.
Simplicity is achieved by exposing IRC in the most straightforward way as possible where it makes sense so it feels familiar for long time IRC users.

Please file an issue when you find something is missing or isn't working that you'd like to see fixed. Pull requests are more than welcome!

Support and development discussion in [#heisenbridge:vi.fi](https://matrix.to/#/#heisenbridge:vi.fi) on Matrix or by joining [#heisenbridge](https://web.libera.chat/?channels=#heisenbridge) on [Libera.Chat](https://libera.chat) IRC network.
The IRC channel is plumbed with Heisenbridge to Matrix using the relaybot mode.

Features
--------
* "zero configuration" - no databases or storage required
* brings IRC to Matrix rather than Matrix to IRC - not annoying to folks on IRC
* completely managed through admin room - just DM `@heisenbridge`!
* channel management through bridge bot - type `heisenbridge: help` to get started!
* online help within Matrix
* access control for local and federated users
* configurable IRC user synchronization in rooms (fully synced on connect, half synced on join or lazy on talk)
* tested with up to 2000 users in a single channel
* optional room plumbing with single puppeting on Matrix <-> relaybot on IRC
* IRCnet !channels _are_ supported, you're welcome
* any number of IRC networks and users technically possible
* channel customization by setting the name and avatar
* TLS support for networks that have it
* customizable ident support
* configurable pillifying of IRC nicks
* long message splitting directly to IRC
* smart message formatting from Matrix to IRC using IRC conventions
* smart message edits from Matrix to IRC by sending only corrections
* automatic identify/auth with server password or automatic command on connect
* SASL plain authentication
* CertFP authentication
* CTCP support
* SOCKS proxy configuration per server

Comparison
----------

To help understand where Heisenbridge is a good fit here's a feature matrix of some of the key differences between other popular solutions:

|                         | Matrix |  IRC | Appservice | Ratelimit | Self-host | Permissions  |
|-------------------------|:------:|:----:|:----------:|:---------:|:---------:|--------------|
| Heisenbridge (bouncer)  | one    | one  | yes        | no        | yes       | IRC only     |
| Heisenbridge (relaybot) | many   | one  | yes        | IRC       | yes       | separate     |
| matrix-appservice-irc   | many   | many | yes        | no        | no        | synchronized |
| matterbridge            | one    | one  | no         | both      | only      | separate     |

**Matrix** = actual users on Matrix  
**IRC** = connected users to IRC from Matrix  
**Appservice** = runs as an appservice (requires a homeserver)  
**Ratelimit** = perceived ratelimiting in default setup (Matrix/IRC)  
**Self-host** = is it recommended to run your own instance  
**Permissions** = how room/channel permissions are managed (Matrix/IRC)  

Heisenbridge in bouncer mode has one direct Matrix user per IRC connection and IRC users are puppeted on Matrix.
The Matrix users identity is directly mapped to IRC and they are in full control.
There are no Matrix side permission management in this mode as all channels and private messages are between the Matrix user and the bridge.
Multiple authorized Matrix users can use the bridge separately to create their own IRC connections.

Heisenbridge in relaybot mode is part of a larger Matrix room and puppets everyone from IRC separately but still has only one IRC connection for relaying messages from Matrix.
Matrix users' identities are only relayed through the messages they send.
Permissions in this mode are separate meaning the bridge needs to be given explicit permission to join a Matrix room if it's not public.
The bridge only requires enough power levels to invite puppets (if the room is invite-only) and for them to be able to talk with the default power level they get.
Single authorized Matrix user manages the relaybot with Heisenbridge.

[matrix-appservice-irc](https://github.com/matrix-org/matrix-appservice-irc) runs in full single puppeting mode where both IRC and Matrix users are puppeted both ways.
The Matrix users identity is directly mapped to IRC and they are in full control.
This method of bridging creates multiple IRC connections which makes is mostly transparent for both sides of the bridge.
As the room and channel permissions are synchronized between IRC and Matrix (where applicable) means a Matrix user joining a Matrix room needs to be able to connect and join the IRC channel through the bridge as well.
Any Matrix user joining plumbed or portal IRC rooms are automatically connected to the IRC network.

[matterbridge](https://github.com/42wim/matterbridge) is a bot which connects to each network as a single user and is fairly easy and quick to setup by anyone.
Both IRC and Matrix users' identities are only relayed through the messages they send.
The bot operator manages everything and does not require any user interaction on either side.

PyPI
----
GitHub releases are automatically published to [PyPI](https://pypi.org/project/heisenbridge/):

```sh
pip install heisenbridge
```

Docker
------
The master branch is automatically published to [Docker Hub](https://hub.docker.com/r/hif1/heisenbridge):
```sh
docker pull hif1/heisenbridge
docker run --rm hif1/heisenbridge -h
```

Each GitHub release is also tagged as `x.y.z`, `x.y` and `x`.

An example docker-compose setup is in [docker-compose/](docker-compose/).

Additionally, if you use [matrix-docker-ansible-deploy](https://github.com/spantaleev/matrix-docker-ansible-deploy) to deploy your Synapse server, you can use it to integrate Heisenbridge as well - just follow the [relevant docs](https://github.com/spantaleev/matrix-docker-ansible-deploy/blob/master/docs/configuring-playbook-bridge-heisenbridge.md)

Usage
-----
```
usage: python -m heisenbridge [-h] [-v] (-c CONFIG | --version)
                              [-l LISTEN_ADDRESS] [-p LISTEN_PORT] [-u UID]
                              [-g GID] [-i] [--identd-port IDENTD_PORT]
                              [--generate] [--generate-compat] [--reset]
                              [--safe-mode] [-o OWNER]
                              [homeserver]

a bouncer-style Matrix IRC bridge

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
  --version             show bridge version
  -l LISTEN_ADDRESS, --listen-address LISTEN_ADDRESS
                        bridge listen address (default: 127.0.0.1)
  -p LISTEN_PORT, --listen-port LISTEN_PORT
                        bridge listen port (default: 9898)
  -u UID, --uid UID     user id to run as (default: None)
  -g GID, --gid GID     group id to run as (default: None)
  -i, --identd          enable identd service (default: False)
  --identd-port IDENTD_PORT
                        identd listen port (default: 113)
  --generate            generate registration YAML for Matrix homeserver
                        (Synapse)
  --generate-compat     generate registration YAML for Matrix homeserver
                        (Dendrite and Conduit)
  --reset               reset ALL bridge configuration from homeserver and
                        exit
  --safe-mode           prevent appservice from leaving invalid rooms on
                        startup (for debugging) (default: False)
  -o OWNER, --owner OWNER
                        set owner MXID (eg: @user:homeserver) or first talking
                        local user will claim the bridge (default: None)
```

Generate a registration file to use with your homeserver using the `--generate` switch.
If you are using Dendrite or Conduit prefer `--generate-compat` as otherwise you can't talk with Heisenbridge.

Both your homeserver and Heisenbridge use the same registration file to configure their shared secrets.
With Heisenbridge you need to use the generated registration file with the `--config` switch on startup.
If you are running Docker a shared volume mount is adviced for the registration file that both containers can access.

Communication between the homeserver and any bridge is bi-directional and requires that both can access each other directly over the network.
By default Heisenbridge expects your homeserver to be accessible on localhost port 8008/TCP and the bridge itself listens on localhost port 9898/TCP.
You can override these defaults using the appropriate command line options but prefer local addresses when possible.
If you are running Docker the homeserver is likely on a different hostname inside the Docker network so change it accordingly by setting the positional argument on startup.

Please note that the URL for Heisenbridge in the registration file is used by the homeserver to connect to it so make sure it is correct and accessible from where the homeserver is running.

If for whatever reason you run Heisenbridge over the internet and require HTTPS you need to put Heisenbridge behind a reverse proxy that does TLS termination as it doesn't itself support loading a TLS certificate.

For [Synapse](https://github.com/matrix-org/synapse) see their [installation instructions](https://github.com/matrix-org/synapse/blob/develop/docs/application_services.md) for appservices.

For [Conduit](https://gitlab.com/famedly/conduit) see their [installation instructions](https://gitlab.com/famedly/conduit/-/blob/next/APPSERVICES.md) for appservices.

Install
-------

1. Install Python 3.7 or newer
2. Install dependencies in virtualenv

   ```bash
   virtualenv venv
   source venv/bin/activate
   pip install heisenbridge
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

To update your installation, run `pip install --upgrade heisenbridge`

Develop
-------

1. Install Python 3.7 or newer
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
