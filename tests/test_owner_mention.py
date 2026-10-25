from types import SimpleNamespace

from heisenbridge.private_room import PrivateRoom

OWNER = "@owner:example.org"
OWN_GHOST = "@irc_net_owner:example.org"
OTHER_GHOST = "@irc_net_friend:example.org"


class FakeQueue:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)


def make_room():
    room = PrivateRoom.__new__(PrivateRoom)
    room.user_id = OWNER
    room.network = SimpleNamespace(pills_length=2, pills_ignore=[], conn=SimpleNamespace(real_nickname="owner"))
    room.serv = SimpleNamespace(puppet_prefix="irc_", server_name="example.org")
    room.members = [OWNER, OWN_GHOST, OTHER_GHOST]
    room.displaynames = {OWNER: "owner", OWN_GHOST: "owner", OTHER_GHOST: "friend"}
    room._queue = FakeQueue()
    return room


def test_own_nick_pill_points_at_owner_not_own_ghost():
    pills = make_room().pills()

    assert pills["owner"] == (OWNER, "owner")
    assert pills["friend"] == (OTHER_GHOST, "friend")


def test_highlight_of_owner_carries_m_mentions():
    room = make_room()
    room.send_message("owner: hi", OTHER_GHOST, formatted=f'<a href="https://matrix.to/#/{OWNER}">owner</a>: hi')

    assert room._queue.events[0]["content"]["m.mentions"] == {"user_ids": [OWNER]}


def test_message_without_owner_pill_has_no_mentions():
    room = make_room()
    room.send_message(
        "friend: hi", OTHER_GHOST, formatted=f'<a href="https://matrix.to/#/{OTHER_GHOST}">friend</a>: hi'
    )

    assert "m.mentions" not in room._queue.events[0]["content"]
