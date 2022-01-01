import re
from typing import Dict
from typing import Optional
from typing import Pattern

from mautrix.types import UserID
from mautrix.util.formatter.formatted_string import EntityType
from mautrix.util.formatter.html_reader import HTMLNode
from mautrix.util.formatter.markdown_string import MarkdownString
from mautrix.util.formatter.parser import MatrixParser
from mautrix.util.formatter.parser import RecursionContext
from mautrix.util.formatter.parser import T


class IRCString(MarkdownString):
    def format(self, entity_type: EntityType, **kwargs) -> "IRCString":
        if entity_type == EntityType.BOLD:
            self.text = f"*{self.text}*"
        elif entity_type == EntityType.ITALIC:
            self.text = f"_{self.text}_"
        elif entity_type == EntityType.STRIKETHROUGH:
            self.text = f"~{self.text}~"
        elif entity_type == EntityType.UNDERLINE:
            self.text = self.text
        elif entity_type == EntityType.URL:
            if kwargs["url"] != self.text:
                self.text = f"{self.text} ({kwargs['url']})"
        elif entity_type == EntityType.EMAIL:
            self.text = self.text
        elif entity_type == EntityType.PREFORMATTED:
            self.text = re.sub(r"\n+", "\n", self.text) + "\n"
        elif entity_type == EntityType.INLINE_CODE:
            self.text = f'"{self.text}"'
        elif entity_type == EntityType.BLOCKQUOTE:
            children = self.trim().split("\n")
            children = [child.prepend("> ") for child in children]
            self.text = self.join(children, "\n").text
        elif entity_type == EntityType.USER_MENTION:
            if kwargs["displayname"] is not None:
                self.text = kwargs["displayname"]

        return self


class IRCMatrixParser(MatrixParser):
    fs = IRCString
    list_bullets = ("-", "*", "+", "=")
    displaynames = Dict[str, str]

    # use .* to account for legacy empty mxid
    mention_regex: Pattern = re.compile("https://matrix.to/#/(@.*:.+)")

    def __init__(self, displaynames: Dict[str, str]) -> T:
        self.displaynames = displaynames

    async def tag_aware_parse_node(self, node: HTMLNode, ctx: RecursionContext) -> T:
        msgs = await self.node_to_tagged_fstrings(node, ctx)
        output = self.fs()
        prev_was_block = True
        for msg, tag in msgs:
            if tag in self.block_tags:
                msg = msg.trim()
                if not prev_was_block:
                    output.append("\n")
                prev_was_block = True
            else:
                prev_was_block = False
            output = output.append(msg)
        return output.trim()

    async def user_pill_to_fstring(self, msg: T, user_id: UserID) -> Optional[T]:
        displayname = None
        if user_id in self.displaynames:
            displayname = self.displaynames[user_id]
        return msg.format(self.e.USER_MENTION, user_id=user_id, displayname=displayname)
