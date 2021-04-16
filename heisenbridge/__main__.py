from typing import Dict, List, Set

import traceback
import asyncio
import aiohttp
from aiohttp import web
import yaml
import argparse
import string
import random

from heisenbridge.matrix import Matrix, MatrixError, MatrixUserInUse
from heisenbridge.appservice import AppService
from heisenbridge.room import Room
from heisenbridge.control_room import ControlRoom
from heisenbridge.network_room import NetworkRoom
from heisenbridge.private_room import PrivateRoom
from heisenbridge.channel_room import ChannelRoom

class BridgeAppService(AppService):
    _rooms: Dict[str, Room]
    _users: Dict[str, str]

    def register_room(self, room: Room):
        self._rooms[room.id] = room

    def unregister_room(self, room_id):
        if room_id in self._rooms:
            del self._rooms[room_id]

    # this is mostly used by network rooms at init, it's a bit slow
    def find_rooms(self, type, user_id = None) -> List[Room]:
        ret = []

        for room in self._rooms.values():
            if room.__class__ == type and (user_id == None or room.user_id == user_id):
                ret.append(room)

        return ret

    def is_admin(self, user_id: str):
        if user_id == self.config['owner']:
            return True

        # FIXME: proper mask matching
        if user_id in self.config['allow'] and self.config['allow'][user_id] == 'admin':
            return True

        return False

    def is_user(self, user_id: str):
        if self.is_admin(user_id):
            return True

        # FIXME: proper mask matching
        if user_id in self.config['allow']:
            return True

        return False

    def strip_nick(self, nick):
        return nick.strip('@+&')

    def irc_user_id(self, network, nick, at = True, server = True):
        ret = ('@' if at else '') + 'irc_{}_{}'.format(network, self.strip_nick(nick).lower())
        if server:
            ret += ':' + self.server_name
        return ret

    async def cache_user(self, user_id, displayname):
        # start by caching that the user_id exists without a displayname
        if user_id not in self._users:
            self._users[user_id] = None

        # if the cached displayname is incorrect
        if displayname != None and self._users[user_id] != displayname:
            try:
                await self.api.put_user_displayname(user_id, displayname)
            except MatrixError:
                print('Failed to update user displayname but it is okay')

            self._users[user_id] = displayname

    def is_user_cached(self, user_id):
        return user_id in self._users

    async def ensure_irc_user_id(self, network, nick):
        user_id = self.irc_user_id(network, nick)

        # if we've seen this user before, we can skip registering
        if not self.is_user_cached(user_id):
            try:
                await self.api.post_user_register({
                    'type': 'm.login.application_service',
                    'username': self.irc_user_id(network, nick, False, False),
                })
            except MatrixUserInUse:
                pass

        # always ensure the displayname is up-to-date
        await self.cache_user(user_id, nick)

        return user_id

    async def _on_mx_event(self, event):
        # keep user cache up-to-date
        if 'user_id' in event:
            await self.cache_user(event['user_id'], None)

        if 'room_id' in event and event['room_id'] in self._rooms:
            try:
                room = self._rooms[event['room_id']]
                if not await room.on_mx_event(event):
                    print('Event handler for {} returned false, leaving and cleaning up'.format(event['type']))
                    self.unregister_room(room.id)
                    await room.cleanup()

                    try:
                        await self.api.post_room_leave(room.id)
                    except MatrixError:
                        pass
                    try:
                        await self.api.post_room_forget(room.id)
                    except MatrixError:
                        pass
            except Exception as e:
                print('Ignoring exception from room handler:', str(e))
                traceback.print_exc()
        elif event['type'] == 'm.room.member' and event['user_id'] != self.user_id and event['content']['membership'] == 'invite':
            print('got an invite')

            # only respond to an invite
            if event['room_id'] in self._rooms:
                print('Control room already open, uhh')
                return

            # set owner
            if 'owner' not in self.config or self.config['owner'] == None:
                print('We have an owner now, let us rejoice!')
                self.config['owner'] = event['user_id']
                await self.save()

            if not self.is_user(event['user_id']):
                print('Non-whitelisted user tried to talk with us:', event['user_id'])
                return

            print('Whitelisted user invited us, going to accept')

            # accept invite sequence
            try:
                room = ControlRoom(event['room_id'], event['user_id'], self, [event['user_id']])
                await room.save()
                self.register_room(room)
                await self.api.post_room_join(room.id)

                # show help on open
                await room.show_help()
            except Exception as e:
                if event['room_id'] in self._rooms:
                    del self._rooms[event['room_id']]
                print(e)
        else:
            pass
            #print(json.dumps(event, indent=4, sort_keys=True))

    async def _transaction(self, req):
        body = await req.json()

        for event in body['events']:
          await self._on_mx_event(event)

        return web.json_response({})

    async def run(self, config_file, listen_address, listen_port, homeserver_url):
        with open(config_file) as f:
            registration = yaml.safe_load(f)

        app = aiohttp.web.Application()
        app.router.add_put('/transactions/{id}', self._transaction)

        self.api = Matrix(homeserver_url, registration['as_token'])

        whoami = await self.api.get_user_whoami()
        print('We are ' + whoami['user_id'])

        self._rooms = {}
        self._users = {}
        self.user_id = whoami['user_id']
        self.server_name = self.user_id.split(':')[1]
        self.config = {'networks': {}, 'owner': None, 'allow': {}}

        # load config from HS
        await self.load()
        print(self.config)

        resp = await self.api.get_user_joined_rooms()
        print("Got rooms from server:")
        print(resp)

        try:
            await self.api.post_user_register({
                'type': 'm.login.application_service',
                'username': registration['sender_localpart']
            })
        except MatrixUserInUse:
            pass

        await self.api.put_user_displayname(self.user_id, 'Friendly IRC Bridge')

        # room types and their init order, network must be before chat and group
        room_types = [ ControlRoom, NetworkRoom, PrivateRoom, ChannelRoom ]

        room_type_map = {}
        for room_type in room_types:
            room_type_map[room_type.__name__] = room_type

        print(room_type_map)

        # import all rooms
        for room_id in resp['joined_rooms']:
            try:
                config = await self.api.get_room_account_data(self.user_id, room_id, 'irc')

                if 'type' not in config or 'user_id' not in config:
                    raise Exception('Invalid config')

                cls = room_type_map.get(config['type'])
                if not cls:
                    raise Exception('Unknown room type')

                members = list((await self.api.get_room_joined_members(room_id))['joined'].keys())

                # add to cache immediately but without known displayname
                for user_id in members:
                    await self.cache_user(user_id, None)

                room = cls(room_id, config['user_id'], self, members)
                room.from_config(config)

                # only add valid rooms to event handler
                if room.is_valid():
                    self._rooms[room_id] = room
                else:
                    await room.cleanup()
                    raise Exception('Room validation failed after init')
            except Exception as e:
                print('Failed to configure room, leaving:')
                print(e)

                self.unregister_room(room_id)

                try:
                    await self.api.post_room_leave(room_id)
                except MatrixError:
                    pass
                try:
                    await self.api.post_room_forget(room_id)
                except MatrixError:
                    pass

        print('Connecting network rooms...')

        # connect network rooms
        for room in self._rooms.values():
            if type(room) == NetworkRoom and room.connected:
                await room.connect()

        print('Init done!')

        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, listen_address, listen_port)
        await site.start()

        await asyncio.Event().wait()

parser = argparse.ArgumentParser(description='The Friendly IRC bridge for Matrix', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('-c', '--config', help='registration YAML file path, must be writable if generating', required=True)
parser.add_argument('-l', '--listen-address', help='bridge listen address', default='127.0.0.1')
parser.add_argument('-p', '--listen-port', help='bridge listen port', type=int, default='9898')
parser.add_argument('--generate', action='store_true', help='generate registration YAML for Matrix homeserver', default=argparse.SUPPRESS)
parser.add_argument('homeserver', nargs='?', help='URL of Matrix homeserver', default='http://localhost:8008')

args = parser.parse_args()

import io
if 'generate' in args:
    letters = string.ascii_letters + string.digits

    registration = {
        'id': 'irc',
        'url': 'http://{}:{}'.format(args.listen_address, args.listen_port),
        'as_token': ''.join(random.choice(letters) for i in range(64)),
        'hs_token': ''.join(random.choice(letters) for i in range(64)),
        'rate_limited': False,
        'sender_localpart': 'irc',
        'namespaces': {
            'users': [
                {
                    'regex': '@irc_*',
                    'exclusive': True
                }
            ],
            'aliases': [],
            'rooms': [],
        }
    }

    with open(args.config, 'w') as f:
        yaml.dump(registration, f, sort_keys=False)

    print('Registration file generated and saved to {}'.format(args.config))
else:
    service = BridgeAppService()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(service.run(args.config, args.listen_address, args.listen_port, args.homeserver))
    loop.close()
