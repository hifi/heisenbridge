"""URL link previews and inline media embedding for IRC -> Matrix messages.

Heisenbridge does not normally generate link previews. Matrix clients that
implement MSC4095 (notably Beeper) render an ``m.url_previews`` array bundled
onto ``m.room.message`` events as a rich preview card. This module:

* extracts URLs from incoming IRC messages,
* for an HTML page, scrapes OpenGraph/HTML metadata and builds a preview card
  (uploading the og:image to the homeserver so it renders without leaking the
  recipient's IP to the origin),
* for a URL that points directly at a media file, downloads it and produces an
  inline ``m.image`` / ``m.video`` / ``m.audio`` embed,

with an optional per-domain allowlist so embedding can be restricted to trusted
sources (see issue #209).
"""

import asyncio
import logging
import re
import struct
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from urllib.parse import urljoin
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

DEFAULT_FETCH_TIMEOUT = 6.0
DEFAULT_MAX_HTML_BYTES = 512 * 1024
DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_MEDIA_BYTES = 50 * 1024 * 1024
DEFAULT_CACHE_TTL = 30 * 60
DEFAULT_CACHE_MAX_ENTRIES = 256
# Many large sites (YouTube, Twitter/X, Instagram, ...) only serve the
# lightweight server-rendered page with OpenGraph tags near the top of <head>
# to recognised link-preview crawlers; an unknown User-Agent gets the heavy
# JS-app variant where og: tags are buried past our read cap (or absent). Use
# the widely-recognised Discord crawler UA by default so previews work for
# these sites; it is overridable via config (URLPREVIEW --user-agent).
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"

# Trailing characters that are rarely part of a URL (closing paren handled
# separately so balanced parens inside a URL are preserved).
_TRAILING = ".,;:!?]}>’”'\""


def extract_urls(text: str, limit: int = 4) -> List[str]:
    """Return up to ``limit`` distinct http(s) URLs from ``text``, in order."""
    if not text:
        return []
    seen = set()
    out: List[str] = []
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(_TRAILING)
        # Strip a trailing ')' only when unbalanced, so balanced parens inside a
        # URL (e.g. Wikipedia) survive, then clean any punctuation it exposed.
        while url.endswith(")") and url.count("(") < url.count(")"):
            url = url[:-1].rstrip(_TRAILING)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
            if len(out) >= limit:
                break
    return out


def domain_allowed(url: str, allowlist: Optional[List[str]]) -> bool:
    """Return True if the URL's host matches the allowlist.

    An empty/None allowlist allows everything. Entries match the host exactly
    or as a parent domain (``example.com`` matches ``img.example.com``).
    """
    if not allowlist:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    for entry in allowlist:
        entry = entry.strip().lower().lstrip(".")
        if not entry:
            continue
        if host == entry or host.endswith("." + entry):
            return True
    return False


@dataclass
class MediaEmbed:
    """An inline media event to send for a direct media URL."""

    msgtype: str  # m.image / m.video / m.audio
    url: str  # mxc:// URI
    body: str  # filename / fallback text
    mimetype: Optional[str] = None
    size: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class FetchResult:
    """Outcome of fetching a URL: a preview card and/or an inline media embed."""

    preview: Optional[Dict] = None
    media: Optional[MediaEmbed] = None

    def is_empty(self) -> bool:
        return self.preview is None and self.media is None


class _MetaParser(HTMLParser):
    """Extract <title> and og:/twitter:/description meta from an HTML head."""

    _WANTED = frozenset(
        {
            "og:title",
            "og:description",
            "og:image",
            "og:image:url",
            "og:image:secure_url",
            "og:image:width",
            "og:image:height",
            "og:image:alt",
            "og:url",
            "og:site_name",
            "og:type",
            "twitter:title",
            "twitter:description",
            "twitter:image",
            "twitter:image:src",
            "description",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}
        self.title: Optional[str] = None
        self._in_title = False
        self._title_chunks: List[str] = []
        self._in_head = True

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "body":
            self._in_head = False
            return
        if not self._in_head:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        key = a.get("property") or a.get("name") or a.get("itemprop")
        value = a.get("content")
        if not key or value is None:
            return
        key = key.strip().lower()
        if key in self._WANTED and key not in self.meta:
            self.meta[key] = value.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
            if self._title_chunks and self.title is None:
                self.title = unescape("".join(self._title_chunks)).strip()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_chunks.append(data)


def parse_html_meta(html_bytes: bytes, encoding_hint: Optional[str]) -> _MetaParser:
    text: Optional[str] = None
    for enc in (encoding_hint, "utf-8", "latin-1"):
        if not enc:
            continue
        try:
            text = html_bytes.decode(enc, errors="replace")
            break
        except (LookupError, UnicodeDecodeError):
            continue
    if text is None:
        text = html_bytes.decode("utf-8", errors="replace")
    parser = _MetaParser()
    try:
        parser.feed(text)
    except Exception:  # noqa: BLE001 - hostile HTML; keep whatever we parsed
        logger.debug("HTML parser raised; using partial metadata", exc_info=True)
    return parser


def _pick(meta: Dict[str, str], *keys: str) -> Optional[str]:
    for k in keys:
        v = meta.get(k)
        if v:
            return v
    return None


async def _read_capped(resp, limit: int) -> bytes:
    """Read up to ``limit`` decompressed bytes from an aiohttp response.

    ``resp.content.read(n)`` only returns what is currently buffered (often a
    single chunk), which truncates large pages and drops og: tags that appear
    later in <head>. Read chunk-by-chunk until the cap or EOF.
    """
    chunks = []
    total = 0
    async for chunk in resp.content.iter_chunked(65536):
        chunks.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    return b"".join(chunks)[:limit]


class URLPreviewFetcher:
    """Fetches and caches link previews / media embeds for a single AppService.

    Image and media uploads go through the supplied intent (the bridge bot), so
    the resulting mxc:// URIs live on whichever homeserver the appservice is
    registered with — works for both Synapse and Beeper's hungryserv.
    """

    def __init__(
        self,
        intent,
        *,
        embed_media: bool = True,
        allowed_domains: Optional[List[str]] = None,
        fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
        max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        cache_max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._intent = intent
        self.embed_media = embed_media
        self.allowed_domains = allowed_domains or []
        self._timeout = aiohttp.ClientTimeout(total=fetch_timeout)
        self._max_html_bytes = max_html_bytes
        self._max_image_bytes = max_image_bytes
        self._max_media_bytes = max_media_bytes
        self._cache_ttl = cache_ttl
        self._cache_max = cache_max_entries
        self._user_agent = user_agent
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Tuple[float, Optional[FetchResult]]] = {}
        self._inflight: Dict[str, asyncio.Future] = {}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _session_or_create(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "en;q=0.8, *;q=0.5",
                },
            )
        return self._session

    def _cache_get(self, url: str) -> Tuple[bool, Optional[FetchResult]]:
        item = self._cache.get(url)
        if not item:
            return False, None
        ts, value = item
        if (time.monotonic() - ts) > self._cache_ttl:
            self._cache.pop(url, None)
            return False, None
        return True, value

    def _cache_put(self, url: str, value: Optional[FetchResult]) -> None:
        if len(self._cache) >= self._cache_max:
            for k in list(self._cache.keys())[: max(1, self._cache_max // 10)]:
                self._cache.pop(k, None)
        self._cache[url] = (time.monotonic(), value)

    async def fetch(self, url: str) -> Optional[FetchResult]:
        """Return a FetchResult for ``url`` or None if nothing useful was found."""
        if not domain_allowed(url, self.allowed_domains):
            logger.debug("URL %s skipped: domain not in allowlist", url)
            return None

        hit, cached = self._cache_get(url)
        if hit:
            return cached

        fut = self._inflight.get(url)
        if fut is not None:
            return await fut

        fut = asyncio.get_running_loop().create_future()
        self._inflight[url] = fut
        try:
            result = await self._do_fetch(url)
        except Exception:  # noqa: BLE001
            logger.debug("URL preview failed for %s", url, exc_info=True)
            result = None
        finally:
            self._inflight.pop(url, None)

        if result is not None and result.is_empty():
            result = None
        self._cache_put(url, result)
        if not fut.done():
            fut.set_result(result)
        return result

    async def _do_fetch(self, url: str) -> Optional[FetchResult]:
        session = self._session_or_create()
        try:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    return None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                base = ctype.split(";")[0].strip()
                final_url = str(resp.url)

                if base.startswith(("image/", "video/", "audio/")):
                    # The URL is itself a media file.
                    return await self._direct_media(url, final_url, base, resp)

                if "html" not in ctype and "xml" not in ctype:
                    return None

                body = await _read_capped(resp, self._max_html_bytes)
                encoding = resp.charset
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug("HTTP error for %s: %s", url, e)
            return None

        return FetchResult(preview=await self._build_card(url, final_url, body, encoding))

    async def _build_card(
        self, matched_url: str, final_url: str, body: bytes, encoding: Optional[str]
    ) -> Optional[Dict]:
        meta = parse_html_meta(body, encoding)
        title = _pick(meta.meta, "og:title", "twitter:title") or meta.title
        description = _pick(meta.meta, "og:description", "twitter:description", "description")
        canonical = _pick(meta.meta, "og:url") or final_url
        site_name = meta.meta.get("og:site_name")
        image_url = _pick(
            meta.meta,
            "og:image",
            "og:image:url",
            "og:image:secure_url",
            "twitter:image",
            "twitter:image:src",
        )

        preview: Dict[str, object] = {"matrix:matched_url": matched_url}
        if title:
            preview["og:title"] = title[:400]
        if description:
            preview["og:description"] = description[:900]
        if canonical:
            preview["og:url"] = canonical
        if site_name:
            preview["og:site_name"] = site_name

        if image_url:
            uploaded = await self._download_and_upload(
                urljoin(final_url, image_url), self._max_image_bytes, want_dimensions=True
            )
            if uploaded is not None:
                preview["og:image"] = uploaded.url
                if uploaded.mimetype:
                    preview["og:image:type"] = uploaded.mimetype
                if uploaded.size:
                    preview["matrix:image:size"] = uploaded.size
                if uploaded.width:
                    preview["og:image:width"] = uploaded.width
                if uploaded.height:
                    preview["og:image:height"] = uploaded.height
                alt = meta.meta.get("og:image:alt")
                if alt:
                    preview["og:image:alt"] = alt

        if not any(k in preview for k in ("og:title", "og:description", "og:image")):
            return None
        return preview

    async def _direct_media(self, matched_url: str, final_url: str, base: str, resp) -> Optional[FetchResult]:
        is_image = base.startswith("image/")
        cap = self._max_image_bytes if is_image else self._max_media_bytes
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > cap:
            return None
        try:
            data = await _read_capped(resp, cap + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug("media fetch failed %s: %s", final_url, e)
            return None
        if len(data) > cap:
            logger.debug("media too large, skipping: %s", final_url)
            return None

        embed = await self._upload_media_bytes(final_url, base, data)
        if embed is None:
            return None

        # If media embedding is disabled, fall back to an image preview card so
        # the link still gets *some* visual treatment.
        if not self.embed_media:
            if not is_image:
                return None
            return FetchResult(
                preview={
                    "matrix:matched_url": matched_url,
                    "og:url": final_url,
                    "og:title": embed.body,
                    "og:image": embed.url,
                    **({"og:image:type": embed.mimetype} if embed.mimetype else {}),
                    **({"matrix:image:size": embed.size} if embed.size else {}),
                    **({"og:image:width": embed.width} if embed.width else {}),
                    **({"og:image:height": embed.height} if embed.height else {}),
                }
            )
        return FetchResult(media=embed)

    async def _download_and_upload(self, media_url: str, cap: int, want_dimensions: bool) -> Optional[MediaEmbed]:
        session = self._session_or_create()
        try:
            async with session.get(media_url, allow_redirects=True) as resp:
                if resp.status >= 400:
                    return None
                base = (resp.headers.get("Content-Type") or "").lower().split(";")[0].strip()
                if not base.startswith(("image/", "video/", "audio/")):
                    return None
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > cap:
                    return None
                data = await _read_capped(resp, cap + 1)
                if len(data) > cap:
                    return None
                final_url = str(resp.url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug("media download failed %s: %s", media_url, e)
            return None
        return await self._upload_media_bytes(final_url, base, data)

    async def _upload_media_bytes(self, source_url: str, base: str, data: bytes) -> Optional[MediaEmbed]:
        width = height = None
        if base.startswith("image/"):
            try:
                width, height = sniff_image_dimensions(data, base)
            except Exception:  # noqa: BLE001
                pass
        filename = source_url.rsplit("/", 1)[-1].split("?", 1)[0] or "file"
        try:
            mxc = await self._intent.upload_media(data=data, mime_type=base or None, filename=filename, size=len(data))
        except Exception:  # noqa: BLE001
            logger.debug("media upload failed for %s", source_url, exc_info=True)
            return None
        msgtype = "m.image" if base.startswith("image/") else "m.video" if base.startswith("video/") else "m.audio"
        return MediaEmbed(
            msgtype=msgtype,
            url=mxc,
            body=filename,
            mimetype=base or None,
            size=len(data),
            width=width,
            height=height,
        )


def sniff_image_dimensions(data: bytes, ctype: str) -> Tuple[Optional[int], Optional[int]]:
    """Cheap dimension sniffing for common formats — avoids a Pillow dependency."""
    if ctype == "image/png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    if ctype == "image/gif" and len(data) >= 10 and data[:3] == b"GIF":
        width, height = struct.unpack("<HH", data[6:10])
        return width, height
    if ctype in ("image/jpeg", "image/jpg") and data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                return None, None
            while i < n and data[i] == 0xFF:
                i += 1
            if i >= n:
                return None, None
            marker = data[i]
            i += 1
            if marker in (0xD8, 0xD9):
                return None, None
            if 0xD0 <= marker <= 0xD7 or marker == 0x01:
                continue
            if i + 1 >= n:
                return None, None
            seg_len = (data[i] << 8) | data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 7 >= n:
                    return None, None
                height = (data[i + 3] << 8) | data[i + 4]
                width = (data[i + 5] << 8) | data[i + 6]
                return width, height
            i += seg_len
        return None, None
    if ctype == "image/webp" and len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        fourcc = data[12:16]
        if fourcc == b"VP8L" and len(data) >= 25:
            b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
            width = 1 + (((b1 & 0x3F) << 8) | b0)
            height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
            return width, height
        if fourcc == b"VP8X" and len(data) >= 30:
            width = 1 + (data[24] | (data[25] << 8) | (data[26] << 16))
            height = 1 + (data[27] | (data[28] << 8) | (data[29] << 16))
            return width, height
    return None, None
