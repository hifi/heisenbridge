import time
from aiohttp import web, ClientSession

class MatrixError(Exception):
    def __init__(self, errcode = None, error = None):
        self.errcode = errcode
        self.error = error
        super().__init__(self.error)

class MatrixNotFound(MatrixError): pass
class MatrixForbidden(MatrixError): pass
class MatrixUserInUse(MatrixError): pass

class Matrix:
    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.seq = 0
        self.session = str(int(time.time()))

    def _matrix_error(self, data):
        errors = {
            'M_NOT_FOUND': MatrixNotFound,
            'M_FORBIDDEN': MatrixForbidden,
            'M_USER_IN_USE': MatrixUserInUse,
        }

        ex = errors.get(data['errcode'], MatrixError)
        return ex(data['errcode'], data['error'])

    def _txn(self):
        self.seq += 1
        return self.session + '-' + str(self.seq)

    async def call(self, method, uri, data = None):
        async with ClientSession(headers={'Authorization': 'Bearer ' + self.token}) as session:
            resp = await session.request(method, self.url + uri, json=data)
            data = await resp.json()

            if resp.status > 299:
                raise self._matrix_error(data)

            return data

    async def get_user_whoami(self):
        return await self.call('GET', '/_matrix/client/r0/account/whoami')

    async def get_user_joined_rooms(self):
        return await self.call('GET', '/_matrix/client/r0/joined_rooms')

    async def get_user_account_data(self, user_id, key):
        return await self.call('GET', '/_matrix/client/r0/user/' + user_id + '/account_data/' + key)

    async def put_user_account_data(self, user_id, key, data):
        return await self.call('PUT', '/_matrix/client/r0/user/' + user_id + '/account_data/' + key, data)

    async def get_room_account_data(self, user_id, room_id, key):
        return await self.call('GET', '/_matrix/client/r0/user/' + user_id + '/rooms/' + room_id + '/account_data/' + key)

    async def put_room_account_data(self, user_id, room_id, key, data):
        return await self.call('PUT', '/_matrix/client/r0/user/' + user_id + '/rooms/' + room_id + '/account_data/' + key, data)

    async def post_room_leave(self, room_id, user_id = None):
        return await self.call('POST', '/_matrix/client/r0/rooms/' + room_id + '/leave' + ('?user_id={}'.format(user_id) if user_id else ''))

    async def post_room_forget(self, room_id):
        return await self.call('POST', '/_matrix/client/r0/rooms/' + room_id + '/forget')

    async def get_room_joined_members(self, room_id):
        return await self.call('GET', '/_matrix/client/r0/rooms/' + room_id + '/joined_members')

    async def post_room_join(self, room_id, user_id = None):
        return await self.call('POST', '/_matrix/client/r0/rooms/' + room_id + '/join' + ('?user_id={}'.format(user_id) if user_id else ''))

    async def post_room_invite(self, room_id, user_id):
        return await self.call('POST', '/_matrix/client/r0/rooms/' + room_id + '/invite', {'user_id': user_id})

    async def put_room_send_event(self, room_id, type, content, user_id = None):
        return await self.call('PUT', '/_matrix/client/r0/rooms/' + room_id + '/send/' + type + '/' + self._txn() + ('?user_id={}'.format(user_id) if user_id else ''), content)

    async def post_room_create(self, data):
        return await self.call('POST', '/_matrix/client/r0/createRoom', data)

    async def post_user_register(self, data):
        return await self.call('POST', '/_matrix/client/r0/register?kind=user', data)

    async def put_user_displayname(self, user_id, displayname):
        return await self.call('PUT', '/_matrix/client/r0/profile/{}/displayname?user_id={}'.format(user_id, user_id), {'displayname': displayname})
