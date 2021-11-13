import asyncio
import logging
import time
import urllib

from aiohttp import ClientError
from aiohttp import ClientResponseError
from aiohttp import ClientSession
from aiohttp import TCPConnector


class MatrixError(Exception):
    def __init__(self, data):
        if "errcode" in data:
            self.errcode = data["errcode"]
        else:
            self.errcode = 0

        if "error" in data:
            self.error = data["error"]
        else:
            self.error = "Unspecified error"

        super().__init__(self.errcode)


class MatrixErrorUnknown(MatrixError):
    pass


class MatrixNotFound(MatrixError):
    pass


class MatrixForbidden(MatrixError):
    pass


class MatrixUserInUse(MatrixError):
    pass


class MatrixLimitExceeded(MatrixError):
    def __init__(self, data):
        super().__init__(data)

        if "retry_after_ms" in data:
            self.retry_after_s = data["retry_after_ms"] / 1000
        else:
            self.retry_after_s = 5


class Matrix:
    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.seq = 0
        self.session = str(int(time.time()))
        self.conn = TCPConnector()

    def _matrix_error(self, data):
        errors = {
            "M_UNKNOWN": MatrixErrorUnknown,
            "M_NOT_FOUND": MatrixNotFound,
            "M_FORBIDDEN": MatrixForbidden,
            "M_USER_IN_USE": MatrixUserInUse,
            "M_LIMIT_EXCEEDED": MatrixLimitExceeded,
        }

        ex = errors.get(data["errcode"], MatrixError)
        return ex(data)

    def _txn(self):
        self.seq += 1
        return self.session + "-" + str(self.seq)

    async def call(self, method, uri, data=None, content_type="application/json", retry=True):
        async with ClientSession(
            headers={"Authorization": "Bearer " + self.token}, connector=self.conn, connector_owner=False
        ) as session:
            for i in range(0, 60):
                try:
                    if content_type == "application/json":
                        resp = await session.request(method, self.url + uri, json=data)
                    else:
                        resp = await session.request(
                            method, self.url + uri, data=data, headers={"Content-type": content_type}
                        )
                    ret = await resp.json()

                    if resp.status > 299:
                        raise self._matrix_error(ret)

                    return ret
                except MatrixErrorUnknown:
                    logging.warning(
                        f"Request to HS failed with unknown Matrix error, HTTP code {resp.status}, falling through to retry."
                    )
                except MatrixLimitExceeded as e:
                    logging.warning(f"Request to HS was rate limited, retrying in {e.retry_after_s} seconds...")
                    await asyncio.sleep(e.retry_after_s)
                    continue
                except ClientResponseError as e:
                    # fail fast if no retry allowed if dealing with HTTP error
                    logging.debug(str(e))
                    if not retry:
                        raise

                except (ClientError, asyncio.TimeoutError) as e:
                    # catch and fall-through to sleep
                    logging.debug(str(e))
                    pass

                logging.warning(f"Request to HS failed, assuming it is down, retry {i+1}/60...")
                await asyncio.sleep(30)

    async def get_user_whoami(self):
        return await self.call("GET", "/_matrix/client/r0/account/whoami")

    async def get_user_joined_rooms(self):
        return await self.call("GET", "/_matrix/client/r0/joined_rooms")

    async def get_user_account_data(self, user_id, key):
        user_id = urllib.parse.quote(user_id, safe="")
        return await self.call("GET", "/_matrix/client/r0/user/" + user_id + "/account_data/" + key)

    async def put_user_account_data(self, user_id, key, data):
        user_id = urllib.parse.quote(user_id, safe="")
        return await self.call("PUT", "/_matrix/client/r0/user/" + user_id + "/account_data/" + key, data)

    async def get_room_account_data(self, user_id, room_id, key):
        user_id = urllib.parse.quote(user_id, safe="")
        return await self.call(
            "GET",
            "/_matrix/client/r0/user/" + user_id + "/rooms/" + room_id + "/account_data/" + key,
        )

    async def put_room_account_data(self, user_id, room_id, key, data):
        user_id = urllib.parse.quote(user_id, safe="")
        return await self.call(
            "PUT",
            "/_matrix/client/r0/user/" + user_id + "/rooms/" + room_id + "/account_data/" + key,
            data,
        )

    async def post_room_leave(self, room_id, user_id=None):
        if user_id:
            user_id = urllib.parse.quote(user_id, safe="")

        return await self.call(
            "POST",
            "/_matrix/client/r0/rooms/" + room_id + "/leave" + ("?user_id={}".format(user_id) if user_id else ""),
        )

    async def post_room_kick(self, room_id, target_user_id, reason="", user_id=None):
        if user_id:
            user_id = urllib.parse.quote(user_id, safe="")

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

    async def get_room_members(self, room_id, not_membership="leave"):
        q = ""
        if not_membership:
            q = f"?not_membership={not_membership}"
        return await self.call("GET", "/_matrix/client/r0/rooms/" + room_id + "/members" + q)

    async def get_room_event(self, room_id, event_id):
        return await self.call("GET", "/_matrix/client/r0/rooms/" + room_id + "/event/" + event_id)

    async def get_room_state_event(self, room_id, event_type, state_key=""):
        return await self.call("GET", "/_matrix/client/r0/rooms/" + room_id + "/state/" + event_type + "/" + state_key)

    async def post_room_join(self, room_id, user_id=None):
        if user_id:
            user_id = urllib.parse.quote(user_id, safe="")

        return await self.call(
            "POST",
            "/_matrix/client/r0/rooms/" + room_id + "/join" + ("?user_id={}".format(user_id) if user_id else ""),
        )

    async def post_room_join_alias(self, room_alias, user_id=None):
        server_name = room_alias.split(":")[1]
        room_alias = urllib.parse.quote(room_alias, safe="")
        if user_id:
            user_id = urllib.parse.quote(user_id, safe="")

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
        if user_id:
            user_id = urllib.parse.quote(user_id, safe="")

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
        if user_id:
            user_id = urllib.parse.quote(user_id, safe="")

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

    async def post_room_receipt(self, room_id, event_id, receipt_type="m.read"):
        room_id = urllib.parse.quote(room_id, safe="")
        event_id = urllib.parse.quote(event_id, safe="")
        receipt_type = urllib.parse.quote(receipt_type, safe="")

        return await self.call("POST", f"/_matrix/client/r0/rooms/{room_id}/receipt/{receipt_type}/{event_id}")

    async def post_user_register(self, data):
        return await self.call("POST", "/_matrix/client/r0/register?kind=user", data)

    async def put_user_displayname(self, user_id, displayname):
        user_id = urllib.parse.quote(user_id, safe="")

        return await self.call(
            "PUT",
            "/_matrix/client/r0/profile/{}/displayname?user_id={}".format(user_id, user_id),
            {"displayname": displayname},
        )

    async def put_user_avatar_url(self, user_id, url):
        user_id = urllib.parse.quote(user_id, safe="")

        return await self.call(
            "PUT",
            "/_matrix/client/r0/profile/{}/avatar_url?user_id={}".format(user_id, user_id),
            {"avatar_url": url},
        )

    async def get_user_avatar_url(self, user_id):
        user_id = urllib.parse.quote(user_id, safe="")

        return await self.call(
            "GET",
            "/_matrix/client/r0/profile/{}/avatar_url?user_id={}".format(user_id, user_id),
        )

    async def put_user_presence(self, user_id, presence="online", status_msg=""):
        user_id = urllib.parse.quote(user_id, safe="")

        return await self.call(
            "PUT", f"/_matrix/client/r0/presence/{user_id}/status", {"presence": presence, "status_msg": status_msg}
        )

    async def post_media_upload(self, data, content_type, filename=None):
        return await self.call(
            "POST",
            "/_matrix/media/r0/upload" + ("?filename=" + urllib.parse.quote(filename, safe="") if filename else ""),
            data,
            content_type=content_type,
        )

    async def get_synapse_admin_users_admin(self, user_id):
        user_id = urllib.parse.quote(user_id, safe="")
        return await self.call("GET", f"/_synapse/admin/v1/users/{user_id}/admin", retry=False)

    async def post_synapse_admin_room_join(self, room_id, user_id):
        return await self.call("POST", f"/_synapse/admin/v1/join/{room_id}", {"user_id": user_id})

    async def post_synapse_admin_media_quarantine(self, server_name, media_id):
        server_name = urllib.parse.quote(server_name, safe="")
        media_id = urllib.parse.quote(media_id, safe="")
        return await self.call("POST", f"/_synapse/admin/v1/media/quarantine/{server_name}/{media_id}")
