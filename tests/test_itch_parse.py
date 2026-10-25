"""itch.io page parsing. itch.io has no API for search, tag pages or page info, so Packrat reads its HTML.

The offline tests use markup shaped like itch.io's real pages. The live tests fetch itch.io itself and
fail when itch.io changes its pages; run them with: pytest -m live
"""
import pytest

from packrat import itch

SEARCH_CELL = """<div dir="auto" class="game_cell has_cover lazy_images" data-game_id="111"><div class="game_thumb">
<a class="thumb_link game_link" href="https://maker.itch.io/dice-pack"><img data-lazy_src="https://img.itch.zone/a.png"/></a>
</div><div class="game_cell_data"><div class="game_title"><a class="title game_link" href="https://maker.itch.io/dice-pack">Dice &amp; Tokens</a><a class="price_tag meta_tag sale"><div class="price_value">$2.50</div></a></div>
<div title="Stone dice" class="game_text">Stone dice for your game</div>
<div class="game_author"><a href="https://maker.itch.io">Maker</a></div></div></div>"""

FREE_CELL = """<div class="game_cell has_cover" data-game_id="222"><div class="game_cell_data"><div class="game_title">
<a class="title game_link" href="https://other.itch.io/free-icons">Free Icons</a></div>
<div class="game_author"><a href="https://other.itch.io">Other</a></div></div></div>"""

ASSET_PAGE = """<div class="game_info_panel_widget base_widget"><table><tbody>
<tr><td>Status</td><td><a href="#">Released</a></td></tr>
<tr><td>Rating</td><td><div class="aggregate_rating">4.5</div></td></tr>
<tr><td>Author</td><td><a href="https://maker.itch.io">Maker</a></td></tr>
<tr><td>Tags</td><td><a href="#">32x32</a>, <a href="#">Pixel Art</a></td></tr>
<tr><td>License</td><td><a href="#">Creative Commons Zero v1.0 Universal</a></td></tr>
<tr><td>AI Disclosure</td><td>AI Disclosure: No Generative AI</td></tr>
</tbody></table></div>
<div class="formatted_description user_formatted"><p>A dice pack.</p><h2>Terms of use</h2><p>Use in commercial games.</p>
<p>Don't resell.</p><div class="more_information_toggle">"""


def test_search_cells():
    results = itch.parse_cells("<html>" + SEARCH_CELL + FREE_CELL + "</html>")
    assert [r["game_id"] for r in results] == ["111", "222"]
    paid, free = results
    assert paid["title"] == "Dice & Tokens"
    assert paid["url"] == "https://maker.itch.io/dice-pack"
    assert paid["author"] == "Maker" and paid["author_url"] == "https://maker.itch.io"
    assert paid["cover_url"] == "https://img.itch.zone/a.png"
    assert paid["price"] == "$2.50" and paid["short_text"] == "Stone dice for your game"
    assert free["price"] == "" and free["free"] and not paid["free"]  # no price tag: free or pay-what-you-want


def test_asset_page_info_and_terms():
    meta = itch.parse_page(ASSET_PAGE)
    assert meta["tags"] == ["32x32", "Pixel Art"]
    assert meta["author"] == "Maker"
    assert meta["license"] == "Creative Commons Zero v1.0 Universal"
    assert meta["ai"].endswith("No Generative AI")
    assert meta["info"] == {"Status": "Released"}  # rating and links are left out
    assert meta["terms"].splitlines()[:2] == ["Terms of use", "Use in commercial games."]


@pytest.mark.parametrize("text, slug", [("dice", "dice"), ("Pixel Art", "pixel-art"), (" 2D / RPG! ", "2d-rpg"), ("", "")])
def test_tag_slug(text, slug):
    assert itch.tag_slug(text) == slug


@pytest.mark.parametrize("url, ok", [
    ("https://maker.itch.io/dice-pack", True),
    ("maker.itch.io/dice-pack?x=1", True),
    ("https://itch.io/game-assets", False),
    ("https://example.com/dice", False),
])
def test_page_url(url, ok):
    if ok:
        assert itch.page_url(url).startswith("https://maker.itch.io/dice-pack")
    else:
        with pytest.raises(itch.ItchError):
            itch.page_url(url)


# ------------------------------------------------------------------ live (network)

@pytest.mark.live
def test_live_search():
    results = itch.search("dice")
    assert len(results) >= 10, "itch.io search returned too few results; its page markup may have changed"
    assert all(r["game_id"] and r["url"].startswith("https://") and r["title"] for r in results)
    assert any(r["price"] for r in results) and any(not r["price"] for r in results)


@pytest.mark.live
def test_live_free_tag_pages():
    first, second = itch.browse_free_tag("pixel art", 1), itch.browse_free_tag("pixel art", 2)
    assert first["tag"] == "pixel-art" and len(first["results"]) == 36 and first["more"]
    assert {r["game_id"] for r in first["results"]}.isdisjoint(r["game_id"] for r in second["results"])
    assert itch.browse_free_tag("xqzzy-not-a-tag")["results"] == []


@pytest.mark.live
def test_live_asset_pages_have_tags():
    urls = [r["url"] for r in itch.search("pixel art tileset")[:5]]
    metas = [itch.parse_page(itch.fetch_public(u)) for u in urls]
    assert any(m.get("tags") for m in metas), "no tags found on asset pages; itch.io's info panel may have changed"
