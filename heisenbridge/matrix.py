import asyncio
import logging
import time
import urllib

from aiohttp import ClientError
from aiohttp import ClientResponseError
from aiohttp import ClientSession
from aiohttp import TCPConnector


class MatrixError(Exception):
    def __init__(self, errcode=None, error=None):
        self.errcode = errcode
        self.error = error
        super().__init__(self.error)


class MatrixNotFound(MatrixError):
    pass


class MatrixForbidden(MatrixError):
    pass


class MatrixUserInUse(MatrixError):
    pass


class Matrix:
    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.seq = 0
        self.session = str(int(time.time()))
        self.conn = TCPConnector()

    def _matrix_error(self, data):
        errors = {
            "M_NOT_FOUND": MatrixNotFound,
            "M_FORBIDDEN": MatrixForbidden,
            "M_USER_IN_USE": MatrixUserInUse,
        }

        ex = errors.get(data["errcode"], MatrixError)
        return ex(data["errcode"], data["error"])

    def _txn(self):
        self.seq += 1
        return self.session + "-" + str(self.seq)

    async def call(self, method, uri, data=None, retry=True):
        async with ClientSession(
            headers={"Authorization": "Bearer " + self.token}, connector=self.conn, connector_owner=False
        ) as session:
            for i in range(0, 60):
                try:
                    resp = await session.request(method, self.url + uri, json=data)
                    data = await resp.json()

                    if resp.status > 299:
                        raise self._matrix_error(data)

                    return data
                except ClientResponseError:
                    # fail fast if no retry allowed if dealing with HTTP error
                    if not retry:
                        raise

                except (ClientError, asyncio.TimeoutError):
                    # catch and fall-through to sleep
                    pass

                logging.warning(f"Request to HS failed, assuming it is down, retry {i+1}/60...")
                await asyncio.sleep(30)

    async def get_user_whoami(self):
        return await self.call("GET", "/_matrix/client/r0/account/whoami")

    async def get_user_joined_rooms(self):
        return await self.call("GET", "/_matrix/client/r0/joined_rooms")

    async def get_user_account_data(self, user_id, key):
        return await self.call("GET", "/_matrix/client/r0/user/" + user_id + "/account_data/" + key)

    async def put_user_account_data(self, user_id, key, data):
        return await self.call("PUT", "/_matrix/client/r0/user/" + user_id + "/account_data/" + key, data)

    async def get_room_account_data(self, user_id, room_id, key):
        return await self.call(
            "GET",
            "/_matrix/client/r0/user/" + user_id + "/rooms/" + room_id + "/account_data/" + key,
        )

    async def put_room_account_data(self, user_id, room_id, key, data):
        return await self.call(
            "PUT",
            "/_matrix/client/r0/user/" + user_id + "/rooms/" + room_id + "/account_data/" + key,
            data,
        )

    async def post_room_leave(self, room_id, user_id=None):
        return await self.call(
            "POST",
            "/_matrix/client/r0/rooms/" + room_id + "/leave" + ("?user_id={}".format(user_id) if user_id else ""),
        )

    async def post_room_kick(self, room_id, target_user_id, reason="", user_id=None):
        return await self.call(
            "POST",
            "/_matrix/client/r0/rooms/" + room_id + "/kick" + ("?user_id={}".format(user_id) if user_id else ""),
            {
                "reason": reason,
                "user_id": target_user_id,
            },
        )

    async def post_room_forget(self, room_id):
        return await self.call("POST", "/_matrix/client/r0/rooms/" + room_id + "/forget")

    async def get_room_joined_members(self, room_id):
        return await self.call("GET", "/_matrix/client/r0/rooms/" + room_id + "/joined_members")

    async def get_room_state_event(self, room_id, event_type, state_key=""):
        return await self.call("GET", "/_matrix/client/r0/rooms/" + room_id + "/state/" + event_type + "/" + state_key)

    async def post_room_join(self, room_id, user_id=None):
        return await self.call(
            "POST",
            "/_matrix/client/r0/rooms/" + room_id + "/join" + ("?user_id={}".format(user_id) if user_id else ""),
        )

    async def post_room_join_alias(self, room_alias, user_id=None):
        server_name = room_alias.split(":")[1]
        room_alias = urllib.parse.quote(room_alias)
        return await self.call(
            "POST",
            f"/_matrix/client/r0/join/{room_alias}?server_name={server_name}"
            + ("&user_id={}".format(user_id) if user_id else ""),
        )

    async def post_room_invite(self, room_id, user_id):
        return await self.call(
            "POST",
            "/_matrix/client/r0/rooms/" + room_id + "/invite",
            {"user_id": user_id},
        )

    async def put_room_send_event(self, room_id, type, content, user_id=None):
        return await self.call(
            "PUT",
            "/_matrix/client/r0/rooms/"
            + room_id
            + "/send/"
            + type
            + "/"
            + self._txn()
            + ("?user_id={}".format(user_id) if user_id else ""),
            content,
        )

    async def put_room_send_state(self, room_id, type, state_key, content, user_id=None):
        return await self.call(
            "PUT",
            "/_matrix/client/r0/rooms/"
            + room_id
            + "/state/"
            + type
            + "/"
            + state_key
            + ("?user_id={}".format(user_id) if user_id else ""),
            content,
        )

    async def post_room_create(self, data):
        return await self.call("POST", "/_matrix/client/r0/createRoom", data)

    async def post_user_register(self, data):
        return await self.call("POST", "/_matrix/client/r0/register?kind=user", data)

    async def put_user_displayname(self, user_id, displayname):
        return await self.call(
            "PUT",
            "/_matrix/client/r0/profile/{}/displayname?user_id={}".format(user_id, user_id),
            {"displayname": displayname},
        )

    async def put_user_avatar_url(self, user_id, url):
        return await self.call(
            "PUT",
            "/_matrix/client/r0/profile/{}/avatar_url?user_id={}".format(user_id, user_id),
            {"avatar_url": url},
        )

    async def get_synapse_admin_users_admin(self, user_id):
        return await self.call("GET", f"/_synapse/admin/v1/users/{user_id}/admin", retry=False)

    async def post_synapse_admin_room_join(self, room_id, user_id):
        return await self.call("POST", f"/_synapse/admin/v1/join/{room_id}", {"user_id": user_id})
