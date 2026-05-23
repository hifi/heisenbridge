from heisenbridge.room import to_beeper_preview
from heisenbridge.url_preview import domain_allowed
from heisenbridge.url_preview import extract_urls
from heisenbridge.url_preview import parse_html_meta
from heisenbridge.url_preview import sniff_image_dimensions


def test_extract_urls_basic():
    assert extract_urls("") == []
    assert extract_urls("no links here") == []
    assert extract_urls("see https://example.com now") == ["https://example.com"]
    assert extract_urls("http://a.test and https://b.test") == ["http://a.test", "https://b.test"]


def test_extract_urls_strips_trailing_punctuation():
    assert extract_urls("look at https://example.com.") == ["https://example.com"]
    assert extract_urls("(https://example.com)") == ["https://example.com"]
    assert extract_urls("end: https://example.com!") == ["https://example.com"]
    # balanced parens inside the URL are kept
    assert extract_urls("https://en.wikipedia.org/wiki/Foo_(bar)") == ["https://en.wikipedia.org/wiki/Foo_(bar)"]


def test_extract_urls_dedupes_and_limits():
    assert extract_urls("https://a.test https://a.test") == ["https://a.test"]
    many = " ".join(f"https://{i}.test" for i in range(10))
    assert len(extract_urls(many, limit=3)) == 3


def test_domain_allowed():
    assert domain_allowed("https://example.com/x", None) is True
    assert domain_allowed("https://example.com/x", []) is True
    assert domain_allowed("https://example.com/x", ["example.com"]) is True
    assert domain_allowed("https://img.example.com/x", ["example.com"]) is True
    assert domain_allowed("https://evil.test/x", ["example.com"]) is False
    # leading dot and case are normalised
    assert domain_allowed("https://EXAMPLE.com/x", [".example.com"]) is True


def test_parse_html_meta_opengraph():
    html = (
        b"<html><head>"
        b"<title>Fallback Title</title>"
        b'<meta property="og:title" content="OG Title">'
        b'<meta property="og:description" content="A description">'
        b'<meta property="og:image" content="https://example.com/img.png">'
        b'<meta name="og:site_name" content="Example">'
        b"</head><body>...</body></html>"
    )
    meta = parse_html_meta(html, "utf-8")
    assert meta.meta["og:title"] == "OG Title"
    assert meta.meta["og:description"] == "A description"
    assert meta.meta["og:image"] == "https://example.com/img.png"
    assert meta.title == "Fallback Title"


def test_parse_html_meta_title_fallback():
    html = b"<html><head><title>Only Title</title></head><body>x</body></html>"
    meta = parse_html_meta(html, "utf-8")
    assert meta.title == "Only Title"
    assert "og:title" not in meta.meta


def test_to_beeper_preview_renames_matched_url():
    msc = {
        "matrix:matched_url": "https://example.com",
        "og:title": "T",
        "og:image": "mxc://a/b",
    }
    legacy = to_beeper_preview(msc)
    assert legacy["matched_url"] == "https://example.com"
    assert "matrix:matched_url" not in legacy
    assert legacy["og:title"] == "T"
    assert legacy["og:image"] == "mxc://a/b"
    # original is not mutated
    assert "matrix:matched_url" in msc


def test_sniff_png_dimensions():
    import struct

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 320, 240)
    assert sniff_image_dimensions(png, "image/png") == (320, 240)


def test_sniff_gif_dimensions():
    import struct

    gif = b"GIF89a" + struct.pack("<HH", 100, 50)
    assert sniff_image_dimensions(gif, "image/gif") == (100, 50)


def test_sniff_unknown_returns_none():
    assert sniff_image_dimensions(b"not an image", "image/tiff") == (None, None)
