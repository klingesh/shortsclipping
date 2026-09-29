"""Tests for username_mark: the creator's own handle burned onto a clip.

These cover the pure logic — validation, placement arithmetic, the overlay graph
— so they run in CI without ffmpeg or a font pass. The pixel-level proof that the
mark actually lands where this arithmetic says lives in verify_mark.py, which
needs real encodes and so cannot run here.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import caption_presets
import username_mark


# --------------------------------------------------------------------------
# text cleaning
# --------------------------------------------------------------------------

def test_clean_text_keeps_a_plain_handle():
    assert username_mark.clean_text("streamerclipzzy07") == "streamerclipzzy07"


def test_clean_text_trims_surrounding_space():
    assert username_mark.clean_text("   @handle   ") == "@handle"


def test_clean_text_collapses_internal_whitespace():
    assert username_mark.clean_text("my   long    name") == "my long name"


def test_clean_text_drops_newlines():
    # A watermark is one line. A newline would silently change the layout.
    assert username_mark.clean_text("first\nsecond") == "first second"


def test_clean_text_drops_tabs_and_carriage_returns():
    assert username_mark.clean_text("a\tb\r\nc") == "a b c"


def test_clean_text_strips_control_characters():
    assert username_mark.clean_text("ha\x00nd\x1fle") == "handle"


def test_clean_text_rejects_empty():
    with pytest.raises(username_mark.MarkError):
        username_mark.clean_text("")


def test_clean_text_rejects_whitespace_only():
    with pytest.raises(username_mark.MarkError):
        username_mark.clean_text("   \t  ")


def test_clean_text_rejects_control_characters_only():
    with pytest.raises(username_mark.MarkError):
        username_mark.clean_text("\x00\x01\x02")


def test_clean_text_rejects_none():
    with pytest.raises(username_mark.MarkError):
        username_mark.clean_text(None)


def test_clean_text_caps_length():
    long = "x" * 200
    assert len(username_mark.clean_text(long)) == username_mark.MAX_TEXT_LENGTH


def test_clean_text_keeps_unicode():
    assert username_mark.clean_text("ünïcødé") == "ünïcødé"


# --------------------------------------------------------------------------
# colour parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("#FFFFFF", (255, 255, 255, 255)),
    ("#000000", (0, 0, 0, 255)),
    ("#00E5FF", (0, 229, 255, 255)),
    ("00E5FF", (0, 229, 255, 255)),          # the hash is optional
    ("#00E5FF80", (0, 229, 255, 128)),
    ("#00e5ff", (0, 229, 255, 255)),         # lower case
])
def test_parse_color_accepts_valid_hex(value, expected):
    assert username_mark._parse_color(value) == expected


@pytest.mark.parametrize("value", [
    "reddish", "#FFF", "#FFFFF", "#FFFFFFFFF", "", None, "#GGGGGG", "12345",
])
def test_parse_color_rejects_invalid(value):
    with pytest.raises(username_mark.MarkError):
        username_mark._parse_color(value)


def test_parse_color_error_names_the_field():
    # The endpoint turns this into a 400, so the message has to say which colour.
    with pytest.raises(username_mark.MarkError, match="outline colour"):
        username_mark._parse_color("nope", field="outline colour")


# --------------------------------------------------------------------------
# normalise_mark
# --------------------------------------------------------------------------

def test_normalise_fills_defaults():
    mark = username_mark.normalise_mark({"text": "@handle"})
    assert mark["anchor"] == username_mark.DEFAULT_ANCHOR
    assert mark["size"] == username_mark.DEFAULT_SIZE
    assert mark["color"] == username_mark.DEFAULT_COLOR
    assert mark["opacity"] == username_mark.DEFAULT_OPACITY
    assert mark["outline_width"] == username_mark.DEFAULT_OUTLINE
    assert mark["margin"] == username_mark.DEFAULT_MARGIN
    assert mark["font"] == caption_presets.BASE_STYLE["font_name"]


def test_normalise_returns_every_key_render_needs():
    # A half-populated dict is the failure BASE_STYLE exists to prevent in
    # caption_presets; the same applies here since render_mark subscripts these
    # directly and would raise KeyError deep in the raster.
    mark = username_mark.normalise_mark({"text": "x"})
    for key in ("text", "font", "size", "color", "opacity", "outline_width",
                "outline_color", "anchor", "margin", "x", "y", "start", "end"):
        assert key in mark, key


def test_normalise_rejects_a_non_dict():
    with pytest.raises(username_mark.MarkError):
        username_mark.normalise_mark("just a string")


def test_normalise_rejects_none():
    with pytest.raises(username_mark.MarkError):
        username_mark.normalise_mark(None)


@pytest.mark.parametrize("anchor", username_mark.ANCHORS)
def test_normalise_accepts_every_anchor(anchor):
    assert username_mark.normalise_mark(
        {"text": "x", "anchor": anchor})["anchor"] == anchor


def test_normalise_lowercases_the_anchor():
    assert username_mark.normalise_mark(
        {"text": "x", "anchor": "BOTTOM_LEFT"})["anchor"] == "bottom_left"


def test_normalise_rejects_an_unknown_anchor():
    with pytest.raises(username_mark.MarkError, match="anchor"):
        username_mark.normalise_mark({"text": "x", "anchor": "nowhere"})


def test_normalise_validates_colour_at_the_boundary():
    # Leaving this to render time made a mistyped colour a 500 from inside the
    # raster instead of a 400 naming the field.
    with pytest.raises(username_mark.MarkError, match="colour"):
        username_mark.normalise_mark({"text": "x", "color": "reddish"})


def test_normalise_validates_the_outline_colour_too():
    with pytest.raises(username_mark.MarkError, match="outline colour"):
        username_mark.normalise_mark({"text": "x", "outline_color": "blackish"})


def test_normalise_keeps_valid_colours_verbatim():
    # Not re-serialised: the editor shows back what it sent.
    mark = username_mark.normalise_mark(
        {"text": "x", "color": "#00e5ff", "outline_color": "#112233AA"})
    assert mark["color"] == "#00e5ff"
    assert mark["outline_color"] == "#112233AA"


@pytest.mark.parametrize("given,expected", [
    (-5.0, username_mark.MIN_SIZE),
    (0.0, username_mark.MIN_SIZE),
    (99.0, username_mark.MAX_SIZE),
    (0.05, 0.05),
])
def test_normalise_clamps_size(given, expected):
    assert username_mark.normalise_mark(
        {"text": "x", "size": given})["size"] == expected


def test_normalise_falls_back_on_unparsable_size():
    # Clamping a non-number must not raise: the field has a sane default and a
    # 500 over a typo in an optional slider is not worth it.
    assert username_mark.normalise_mark(
        {"text": "x", "size": "big"})["size"] == username_mark.DEFAULT_SIZE


@pytest.mark.parametrize("given,expected", [
    (0.0, 0.05), (2.0, 1.0), (0.4, 0.4),
])
def test_normalise_clamps_opacity(given, expected):
    assert username_mark.normalise_mark(
        {"text": "x", "opacity": given})["opacity"] == pytest.approx(expected)


def test_normalise_clamps_outline_width():
    assert username_mark.normalise_mark(
        {"text": "x", "outline_width": 9.0})["outline_width"] == \
        username_mark.MAX_OUTLINE
    assert username_mark.normalise_mark(
        {"text": "x", "outline_width": -1.0})["outline_width"] == \
        username_mark.MIN_OUTLINE


def test_normalise_allows_no_outline():
    assert username_mark.normalise_mark(
        {"text": "x", "outline_width": 0.0})["outline_width"] == 0.0


def test_normalise_clamps_margin():
    assert username_mark.normalise_mark(
        {"text": "x", "margin": 5.0})["margin"] == username_mark.MAX_MARGIN


def test_normalise_leaves_xy_as_none_by_default():
    # None is meaningful: it means "use the anchor". Defaulting it to 0.0 would
    # silently move every anchored mark to the top-left corner.
    mark = username_mark.normalise_mark({"text": "x"})
    assert mark["x"] is None and mark["y"] is None


def test_normalise_keeps_a_zero_coordinate():
    mark = username_mark.normalise_mark({"text": "x", "x": 0.0, "y": 0.0})
    assert mark["x"] == 0.0 and mark["y"] == 0.0


def test_normalise_clamps_xy_but_allows_slight_overhang():
    # A little past the edge is legitimate — a handle can bleed off deliberately
    # — but not arbitrarily far, which would put it out of the frame entirely.
    mark = username_mark.normalise_mark({"text": "x", "x": 99.0, "y": -99.0})
    assert mark["x"] == 1.5 and mark["y"] == -0.5


def test_normalise_defaults_the_time_window_to_the_whole_clip():
    mark = username_mark.normalise_mark({"text": "x"})
    assert mark["start"] is None and mark["end"] is None


def test_normalise_accepts_a_one_sided_window():
    assert username_mark.normalise_mark(
        {"text": "x", "start": 2.0})["end"] is None
    assert username_mark.normalise_mark(
        {"text": "x", "end": 2.0})["start"] is None


def test_normalise_rejects_a_backwards_window():
    with pytest.raises(username_mark.MarkError, match="after"):
        username_mark.normalise_mark({"text": "x", "start": 5.0, "end": 2.0})


def test_normalise_rejects_a_zero_length_window():
    with pytest.raises(username_mark.MarkError):
        username_mark.normalise_mark({"text": "x", "start": 3.0, "end": 3.0})


def test_normalise_clamps_a_negative_start_to_zero():
    assert username_mark.normalise_mark(
        {"text": "x", "start": -10.0, "end": 5.0})["start"] == 0.0


def test_normalise_is_idempotent():
    # apply_mark calls it unconditionally rather than tracking whether the caller
    # already did, which is only safe if running it twice is a no-op.
    once = username_mark.normalise_mark(
        {"text": " @handle ", "anchor": "TOP_RIGHT", "size": 9.0})
    twice = username_mark.normalise_mark(once)
    assert once == twice


def test_normalise_does_not_mutate_its_input():
    raw = {"text": "  @handle  ", "size": 99.0}
    before = dict(raw)
    username_mark.normalise_mark(raw)
    assert raw == before


# --------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------

FRAME = {"frame_width": 1000, "frame_height": 2000}
MARK = {"mark_width": 200, "mark_height": 100}


def _position(**overrides):
    spec = {"text": "x"}
    spec.update(overrides)
    return username_mark.mark_position(
        username_mark.normalise_mark(spec), **FRAME, **MARK)


def test_top_left_sits_at_the_margin():
    # margin is a fraction of WIDTH, for both axes, so the inset looks even.
    x, y = _position(anchor="top_left", margin=0.05)
    assert (x, y) == (50, 50)


def test_top_right_accounts_for_the_mark_width():
    x, y = _position(anchor="top_right", margin=0.05)
    assert (x, y) == (1000 - 200 - 50, 50)


def test_bottom_left_accounts_for_the_mark_height():
    x, y = _position(anchor="bottom_left", margin=0.05)
    assert (x, y) == (50, 2000 - 100 - 50)


def test_bottom_right_insets_from_both_edges():
    x, y = _position(anchor="bottom_right", margin=0.05)
    assert (x, y) == (750, 1850)


def test_centre_ignores_the_margin():
    # "Centre" cannot also mean "inset", or it would not be centred.
    a = _position(anchor="center", margin=0.05)
    b = _position(anchor="center", margin=0.4)
    assert a == b == (400, 950)


def test_middle_left_centres_vertically_only():
    x, y = _position(anchor="middle_left", margin=0.05)
    assert (x, y) == (50, 950)


def test_top_center_centres_horizontally_only():
    x, y = _position(anchor="top_center", margin=0.05)
    assert (x, y) == (400, 50)


def test_zero_margin_puts_the_mark_on_the_edge():
    assert _position(anchor="top_left", margin=0.0) == (0, 0)


@pytest.mark.parametrize("anchor", username_mark.ANCHORS)
def test_every_anchor_stays_inside_the_frame(anchor):
    x, y = _position(anchor=anchor, margin=0.05)
    assert 0 <= x <= FRAME["frame_width"] - MARK["mark_width"]
    assert 0 <= y <= FRAME["frame_height"] - MARK["mark_height"]


def test_free_xy_centres_the_mark_on_the_point():
    # A drag produces a point, and what the user dragged is the middle of the
    # mark, not its corner.
    x, y = _position(x=0.5, y=0.5)
    assert (x, y) == (500 - 100, 1000 - 50)


def test_free_xy_overrides_the_anchor():
    anchored = _position(anchor="top_left", margin=0.05)
    dragged = _position(anchor="top_left", margin=0.05, x=0.9, y=0.9)
    assert anchored != dragged


def test_free_xy_needs_both_coordinates():
    # One without the other is ambiguous, so the anchor still wins.
    only_x = _position(anchor="top_left", margin=0.05, x=0.9)
    assert only_x == _position(anchor="top_left", margin=0.05)


def test_free_xy_is_clamped_into_the_frame():
    x, y = _position(x=1.5, y=1.5)
    assert x == FRAME["frame_width"] - MARK["mark_width"]
    assert y == FRAME["frame_height"] - MARK["mark_height"]


def test_free_xy_clamped_at_the_origin():
    assert _position(x=-0.5, y=-0.5) == (0, 0)


def test_a_mark_wider_than_the_frame_is_pinned_to_the_left():
    # Better a visible left edge than an overlay pushed off screen entirely.
    x, y = username_mark.mark_position(
        username_mark.normalise_mark({"text": "x", "anchor": "bottom_right"}),
        frame_width=500, frame_height=1000,
        mark_width=900, mark_height=100)
    assert x == 0


def test_a_mark_taller_than_the_frame_is_pinned_to_the_top():
    x, y = username_mark.mark_position(
        username_mark.normalise_mark({"text": "x", "anchor": "bottom_right"}),
        frame_width=500, frame_height=200,
        mark_width=100, mark_height=400)
    assert y == 0


def test_position_returns_integers():
    # overlay x/y are pixel offsets; a float would be formatted into the graph.
    x, y = _position(anchor="center")
    assert isinstance(x, int) and isinstance(y, int)


# --------------------------------------------------------------------------
# the enable= window
# --------------------------------------------------------------------------

def test_no_window_means_the_whole_clip():
    mark = username_mark.normalise_mark({"text": "x"})
    assert username_mark._enable_window(mark) == ""


def test_window_with_both_ends_uses_between():
    mark = username_mark.normalise_mark({"text": "x", "start": 1.0, "end": 2.5})
    assert username_mark._enable_window(mark) == "between(t,1.000,2.500)"


def test_window_with_only_a_start_uses_gte():
    mark = username_mark.normalise_mark({"text": "x", "start": 1.25})
    assert username_mark._enable_window(mark) == "gte(t,1.250)"


def test_window_with_only_an_end_uses_lte():
    mark = username_mark.normalise_mark({"text": "x", "end": 4.0})
    assert username_mark._enable_window(mark) == "lte(t,4.000)"


# --------------------------------------------------------------------------
# the filtergraph
# --------------------------------------------------------------------------

def test_graph_overlays_the_png_onto_the_video():
    mark = username_mark.normalise_mark({"text": "x"})
    graph = username_mark.build_filtergraph(mark, x=10, y=20, opacity=1.0)
    assert "[0:v][mark]overlay=x=10:y=20" in graph


def test_graph_converts_to_rgba():
    # Without this the PNG's alpha is dropped and the mark arrives in a black box.
    mark = username_mark.normalise_mark({"text": "x"})
    graph = username_mark.build_filtergraph(mark, x=0, y=0, opacity=1.0)
    assert "format=rgba" in graph


def test_graph_omits_the_mixer_at_full_opacity():
    # A no-op filter is still a filter; leaving it out keeps the graph readable.
    mark = username_mark.normalise_mark({"text": "x"})
    graph = username_mark.build_filtergraph(mark, x=0, y=0, opacity=1.0)
    assert "colorchannelmixer" not in graph


def test_graph_dims_with_the_alpha_mixer():
    mark = username_mark.normalise_mark({"text": "x"})
    graph = username_mark.build_filtergraph(mark, x=0, y=0, opacity=0.5)
    assert "colorchannelmixer=aa=0.5000" in graph


def test_graph_has_no_enable_clause_without_a_window():
    mark = username_mark.normalise_mark({"text": "x"})
    graph = username_mark.build_filtergraph(mark, x=0, y=0, opacity=1.0)
    assert "enable=" not in graph


def test_graph_carries_the_window():
    mark = username_mark.normalise_mark({"text": "x", "start": 1.0, "end": 2.0})
    graph = username_mark.build_filtergraph(mark, x=0, y=0, opacity=1.0)
    assert "enable='between(t,1.000,2.000)'" in graph


def test_graph_never_contains_the_handle():
    # The whole reason this module rasterises instead of calling drawtext: the
    # text must not reach a parser that gives meaning to its punctuation. If it
    # ever does, every check below becomes an escaping problem.
    awkward = "it's:a,b;[c]\\100%"
    mark = username_mark.normalise_mark({"text": awkward})
    graph = username_mark.build_filtergraph(mark, x=0, y=0, opacity=0.8)
    assert awkward not in graph
    for char in ("'a,b", "[c]", "\\100"):
        assert char not in graph


def test_graph_is_a_single_line():
    # A newline in a filter_complex argument breaks the parse.
    mark = username_mark.normalise_mark({"text": "x", "start": 1.0})
    graph = username_mark.build_filtergraph(mark, x=1, y=2, opacity=0.5)
    assert "\n" not in graph


def test_graph_labels_are_balanced():
    # Every label produced must be consumed, or ffmpeg errors on an unused output.
    mark = username_mark.normalise_mark({"text": "x"})
    graph = username_mark.build_filtergraph(mark, x=0, y=0, opacity=0.5)
    produced = set(re.findall(r"\[(\w+)\](?=;|$)", graph))
    consumed = set(re.findall(r"\[(\w+)\](?=\[|\w)", graph))
    assert produced <= consumed | {"mark"}
    assert graph.count("[mark]") == 2   # produced once, consumed once


# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------

def test_every_offered_font_is_resolvable_or_reported():
    # font_path_for must never return a path it has not checked exists, because
    # the caller passes it straight to Pillow.
    for choice in caption_presets.font_catalog():
        path, how = username_mark.font_path_for(choice["family"])
        assert isinstance(how, str) and how
        if path is not None:
            assert os.path.exists(path), (choice["family"], path)


def test_bundled_fonts_resolve_without_fontconfig():
    # The four display faces ship in fonts/, so they must not depend on the
    # image's fontconfig being warm.
    for family in ("Anton", "Bebas Neue", "Archivo Black", "Luckiest Guy"):
        path, how = username_mark.font_path_for(family)
        assert how == "bundled", (family, how)
        assert path and path.endswith(caption_presets.BUNDLED_FONTS[family])


def test_an_unknown_font_does_not_raise():
    # fontconfig always answers something; the point is that it cannot explode.
    path, how = username_mark.font_path_for("NoSuchFaceNameAtAll")
    assert isinstance(how, str)
    if path is not None:
        assert os.path.exists(path)


# --------------------------------------------------------------------------
# the catalog served to the dashboard
# --------------------------------------------------------------------------

def test_catalog_lists_every_anchor():
    assert username_mark.catalog()["anchors"] == list(username_mark.ANCHORS)


def test_catalog_default_anchor_is_a_real_anchor():
    catalog = username_mark.catalog()
    assert catalog["default_anchor"] in catalog["anchors"]


def test_catalog_offers_the_caption_fonts():
    # Same list, so a handle can be matched to the captions.
    assert (username_mark.catalog()["fonts"]
            == caption_presets.font_catalog())


def test_catalog_defaults_are_accepted_by_normalise():
    # A default the validator would reject is the bug this catches: the dashboard
    # would open in a state it cannot submit.
    defaults = username_mark.catalog()["defaults"]
    mark = username_mark.normalise_mark({"text": "x", **defaults})
    for key, value in defaults.items():
        assert mark[key] == value, key


def test_catalog_limits_match_the_module():
    limits = username_mark.catalog()["limits"]
    assert limits["size"] == [username_mark.MIN_SIZE, username_mark.MAX_SIZE]
    assert limits["outline_width"] == [username_mark.MIN_OUTLINE,
                                       username_mark.MAX_OUTLINE]
    assert limits["margin"] == [username_mark.MIN_MARGIN,
                                username_mark.MAX_MARGIN]
    assert limits["text_length"] == username_mark.MAX_TEXT_LENGTH


def test_catalog_default_font_is_one_that_resolves():
    family = username_mark.catalog()["defaults"]["font"]
    path, _ = username_mark.font_path_for(family)
    assert path and os.path.exists(path)
