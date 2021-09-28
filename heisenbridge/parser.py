import re
from typing import Dict
from typing import Optional
from typing import Pattern

from mautrix.types import RoomAlias
from mautrix.types import UserID
from mautrix.util.formatter.formatted_string import EntityType
from mautrix.util.formatter.html_reader import HTMLNode
from mautrix.util.formatter.markdown_string import MarkdownString
from mautrix.util.formatter.parser import MatrixParser
from mautrix.util.formatter.parser import RecursionContext
from mautrix.util.formatter.parser import T


class IRCRecursionContext(RecursionContext):
    displaynames: Dict[str, str]

    def __init__(self, strip_linebreaks: bool = True, ul_depth: int = 0, displaynames: Optional[Dict[str, str]] = None):
        self.displaynames = displaynames
        super().__init__(strip_linebreaks, ul_depth)

    def enter_list(self) -> "RecursionContext":
        return IRCRecursionContext(
            strip_linebreaks=self.strip_linebreaks, ul_depth=self.ul_depth + 1, displaynames=self.displaynames
        )

    def enter_code_block(self) -> "RecursionContext":
        return IRCRecursionContext(strip_linebreaks=False, ul_depth=self.ul_depth, displaynames=self.displaynames)


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

    # use .* to account for legacy empty mxid
    mention_regex: Pattern = re.compile("https://matrix.to/#/(@.*:.+)")

    @classmethod
    def tag_aware_parse_node(cls, node: HTMLNode, ctx: RecursionContext) -> T:
        msgs = cls.node_to_tagged_fstrings(node, ctx)
        output = cls.fs()
        prev_was_block = True
        for msg, tag in msgs:
            if tag in cls.block_tags:
                msg = msg.trim()
                if not prev_was_block:
                    output.append("\n")
                prev_was_block = True
            else:
                prev_was_block = False
            output = output.append(msg)
        return output.trim()

    @classmethod
    def link_to_fstring(cls, node: HTMLNode, ctx: RecursionContext) -> T:
        msg = cls.tag_aware_parse_node(node, ctx)
        href = node.attrib.get("href", "")
        if not href:
            return msg

        if href.startswith("mailto:"):
            return cls.fs(href[len("mailto:") :]).format(cls.e.EMAIL)

        mention = cls.mention_regex.match(href)
        if mention:
            new_msg = cls.user_pill_to_fstring(msg, UserID(mention.group(1)), ctx)
            if new_msg:
                return new_msg

        room = cls.room_regex.match(href)
        if room:
            new_msg = cls.room_pill_to_fstring(msg, RoomAlias(room.group(1)))
            if new_msg:
                return new_msg

        # Custom attribute to tell the parser that the link isn't relevant and
        # shouldn't be included in plaintext representation.
        if cls.ignore_less_relevant_links and cls.exclude_plaintext_attrib in node.attrib:
            return msg

        return cls.url_to_fstring(msg, href)

    @classmethod
    def user_pill_to_fstring(cls, msg: T, user_id: UserID, ctx: RecursionContext) -> Optional[T]:
        displayname = None
        if user_id in ctx.displaynames:
            displayname = ctx.displaynames[user_id]
        return msg.format(cls.e.USER_MENTION, user_id=user_id, displayname=displayname)

    @classmethod
    def parse(cls, data: str, ctx: Optional[RecursionContext] = None) -> T:
        if ctx is None:
            ctx = RecursionContext()

        msg = cls.node_to_fstring(cls.read_html(f"<body>{data}</body>"), ctx)
        return msg
