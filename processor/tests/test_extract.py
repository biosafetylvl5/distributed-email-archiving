from __future__ import annotations

import pytest

from dea_capture.extract import (
    css_urls,
    extract_embeds,
    extract_links,
    is_tracking_pixel,
    parse,
    parse_list_unsubscribe,
    parse_srcset,
)

from .conftest import load


def test_list_unsubscribe_header() -> None:
    assert parse_list_unsubscribe("<https://a.example/u>, <mailto:u@a.example>") == (
        "https://a.example/u",
        "mailto:u@a.example",
    )
    assert parse_list_unsubscribe(None) == ()


def test_generic_embeds_cover_every_html_source() -> None:
    msg = parse(load("generic.eml"))
    urls = {e.url: e.via for e in extract_embeds(msg.html)}
    assert urls == {
        "https://fonts.example.com/serif.woff2": "style",
        "https://www.example.com/img/banner.jpg": "style",
        "https://www.example.com/img/footer.png": "img",
        "https://fonts.example.com/css?family=Serif": "stylesheet",
        "https://www.example.com/css/letter.css": "stylesheet",
        "https://www.example.com/img/trailer-poster.jpg": "poster",
        "https://www.example.com/img/go.png": "input",
        "https://www.example.com/favicon.ico": "icon",
        "https://www.example.com/touch.png": "icon",
    }
    assert list(msg.cid_parts) == ["logo@example.com"]
    assert msg.cid_parts["logo@example.com"].data.startswith(b"\x89PNG")


def test_srcset_background_and_pixels() -> None:
    sub = extract_embeds(parse(load("substack.eml")).html)
    # srcset URLs keep their commas
    big = "https://substackcdn.com/image/fetch/w_1200,c_limit,f_auto,q_auto:good/photo.jpg"
    assert big in {e.url for e in sub}
    assert not any(e.url.endswith("/fetch/w_1200") for e in sub)
    pixels = [e.url for e in sub if e.pixel]
    assert pixels == ["https://eotrx.substackcdn.com/open?token=PIXELTOKEN&ref=1"]

    ac = extract_embeds(parse(load("activecampaign.eml")).html)
    assert ("https://content.app-us1.com/abc/2026/10/26/bg.jpg", "background") in {
        (e.url, e.via) for e in ac
    }
    assert [e.pixel for e in ac] == [False, True, False]

    mc = extract_embeds(parse(load("mailchimp.eml")).html)
    assert [e.pixel for e in mc] == [False, True]


def test_pixel_by_dimensions_only_for_images() -> None:
    assert not is_tracking_pixel("https://cdn.example.com/a.png")
    assert is_tracking_pixel("https://x.example.com/track/open.php?u=1")


def test_links_keep_order_text_and_text_part_extras() -> None:
    links = extract_links(parse(load("generic.eml")))
    hrefs = [link.href for link in links]
    assert hrefs[0] == "https://www.example.com/files/zine-october.pdf"
    assert "#top" in hrefs and "tel:+15555550100" in hrefs
    # the empty <a id="top"> has no href and is not a link
    assert all(h for h in hrefs)
    by_href = {link.href: link for link in links}
    assert by_href["https://www.example.com/"].text == "[image: Home]"
    extra = [link for link in links if link.via == "text"]
    # trailing punctuation is trimmed; URLs already in the HTML aren't repeated
    assert [link.href for link in extra] == ["https://text-only.example.com/notes?ref=letter"]
    assert [link.position for link in links] == list(range(len(links)))


def test_html_only_message_has_no_text_links() -> None:
    msg = parse(load("mailchimp.eml"))
    assert msg.text is None
    assert all(link.via == "html" for link in extract_links(msg))


@pytest.mark.parametrize(
    ("srcset", "urls"),
    [
        ("a.png 1x, b.png 2x", ["a.png", "b.png"]),
        # no whitespace after the comma: one URL, per the spec
        ("a.png,b.png", ["a.png,b.png"]),
        ("a.png, b.png", ["a.png", "b.png"]),
        (
            "https://cdn.example.com/f/w_424,c_limit,f_auto/x.jpg 424w,"
            "https://cdn.example.com/f/w_848,c_limit,f_auto/x.jpg 848w",
            [
                "https://cdn.example.com/f/w_424,c_limit,f_auto/x.jpg",
                "https://cdn.example.com/f/w_848,c_limit,f_auto/x.jpg",
            ],
        ),
        ("  a.png  ,  , b.png 2x  ", ["a.png", "b.png"]),
        ("a.png 1x (junk, more), b.png", ["a.png", "b.png"]),
        ("", []),
    ],
)
def test_parse_srcset(srcset: str, urls: list[str]) -> None:
    assert parse_srcset(srcset) == urls


def test_commented_out_import_is_ignored() -> None:
    embeds = {e.url for e in extract_embeds(parse(load("generic.eml")).html)}
    assert "https://www.example.com/commented-out.css" not in embeds


def test_css_urls_resolves_against_the_stylesheet() -> None:
    css = """
    /* url(https://x.example.com/commented.png) */
    @import "https://a.example.com/a.css";
    @import url('b.css') screen;
    @font-face { src: url(f.woff2) format("woff2"), url("data:font/woff;base64,AA"); }
    .x { background: url( 'img/bg.png' ) }
    .y { background: url(/abs.png) }
    """
    imports, assets = css_urls(css, "https://cdn.example.com/css/main.css")
    assert imports == ["https://a.example.com/a.css", "https://cdn.example.com/css/b.css"]
    assert assets == [
        "https://cdn.example.com/css/f.woff2",
        "https://cdn.example.com/css/img/bg.png",
        "https://cdn.example.com/abs.png",
    ]


def test_css_urls_without_a_base_keeps_only_absolute() -> None:
    imports, assets = css_urls(
        "@import 'rel.css'; .x{background:url(rel.png)} .y{background:url(https://abs.example.com/a.png)}"
    )
    assert (imports, assets) == ([], ["https://abs.example.com/a.png"])
