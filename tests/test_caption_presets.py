"""The named caption looks and how they reach the burn.

The value of these tests is drift: a preset is only useful if the look the
dashboard shows is the look FFmpeg burns. The two ways that breaks are a preset
naming a font the image cannot resolve (libass falls back to DejaVu silently,
issue #57) and a preset whose style dict is missing a field the ASS generator
needs. Both are checked here for EVERY preset, not just the default.
"""

import os
import re
import xml.etree.ElementTree as ET

import pytest

import caption_presets
from subtitles import generate_ass


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(REPO_ROOT, "fonts")
FONTMAP = os.path.join(FONTS_DIR, "openshorts-fontmap.conf")

# Families present in the base Docker image via apt (fonts-liberation) plus the
# generic fallback fontconfig always has. Anything else must either ship as a
# TTF in fonts/ or be aliased onto one of these in the fontmap.
_IMAGE_FAMILIES = caption_presets.IMAGE_FONTS


def _w(text, start, end):
    return {"word": text, "start": start, "end": end}


def _transcript(words):
    return {"language": "en", "segments": [{
        "start": words[0]["start"], "end": words[-1]["end"],
        "text": " ".join(w["word"] for w in words), "words": words,
    }]}


def _bundled_font_families():
    """Family names provided by the TTFs committed in fonts/.

    Read from the declared registry, NOT derived from filenames. Deriving them
    was wrong for every multi-word family: ArchivoBlack-Regular.ttf reports
    "Archivo Black" and BebasNeue-Regular.ttf reports "Bebas Neue", so a
    filename-derived set rejects the very fonts that do resolve.
    """
    return set(caption_presets.BUNDLED_FONTS)


def _aliased_font_families():
    """Families the fontmap redirects onto something installed."""
    tree = ET.parse(FONTMAP)
    return {
        alias.find("family").text.strip()
        for alias in tree.getroot().findall("alias")
        if alias.find("family") is not None
    }


def _resolvable_font_families():
    return _IMAGE_FAMILIES | _bundled_font_families() | _aliased_font_families()


class TestPresetIntegrity:
    """Properties every preset must hold, checked across the whole table."""

    def test_table_is_not_empty(self):
        assert caption_presets.PRESETS

    @pytest.mark.parametrize("preset_id", sorted(caption_presets.PRESETS))
    def test_resolves_to_a_complete_style(self, preset_id):
        # Same field set test_subtitles asserts for the default, applied to all.
        required = {"style", "alignment", "font_name", "font_size", "font_color",
                    "highlight_color", "border_color", "border_width",
                    "bg_color", "bg_opacity", "effect", "base_opacity",
                    "uppercase", "max_chars", "max_duration"}
        assert required <= set(caption_presets.resolve(preset_id))

    @pytest.mark.parametrize("preset_id", sorted(caption_presets.PRESETS))
    def test_font_resolves_in_the_image(self, preset_id):
        # This is the test that stops a preset looking right in the browser and
        # burning as DejaVu. 'Courier New' is the live example of the failure:
        # the modal offers it, and it has neither a TTF nor an alias.
        font = caption_presets.resolve(preset_id)["font_name"]
        assert font in _resolvable_font_families(), (
            f"preset {preset_id!r} uses {font!r}, which has no bundled TTF and "
            f"no fontconfig alias — libass will silently fall back to DejaVu"
        )

    @pytest.mark.parametrize("preset_id", sorted(caption_presets.PRESETS))
    def test_colors_are_hex(self, preset_id):
        style = caption_presets.resolve(preset_id)
        for key in ("font_color", "highlight_color", "border_color", "bg_color"):
            assert re.fullmatch(r"#[0-9A-Fa-f]{6}", style[key]), \
                f"{preset_id}.{key} = {style[key]!r} is not #RRGGBB"

    @pytest.mark.parametrize("preset_id", sorted(caption_presets.PRESETS))
    def test_enums_are_valid(self, preset_id):
        style = caption_presets.resolve(preset_id)
        assert style["style"] in {"classic", "karaoke"}
        assert style["alignment"] in {"top", "middle", "bottom"}
        assert style["effect"] in {"none", "glow", "pop", "box"}
        assert 0.0 <= style["bg_opacity"] <= 1.0
        assert 0.05 <= style["base_opacity"] <= 1.0
        assert style["max_chars"] >= 1
        assert style["max_duration"] > 0


class TestResolve:
    def test_unknown_id_falls_back_to_default(self):
        # Fail-open, matching auto_caption_clip: a stale preset id from a saved
        # project must cost the user nothing.
        assert caption_presets.resolve("no-such-preset") == \
            caption_presets.resolve(caption_presets.DEFAULT_PRESET_ID)

    @pytest.mark.parametrize("junk", [None, "", "   "])
    def test_empty_ids_fall_back(self, junk):
        assert caption_presets.resolve(junk)["font_name"] == \
            caption_presets.BASE_STYLE["font_name"]

    def test_exists_does_not_fall_back(self):
        assert caption_presets.exists("streamer_lime") is True
        assert caption_presets.exists("no-such-preset") is False

    def test_returns_an_independent_copy(self):
        # resolve() handing out a shared dict would let one caller's tweak
        # rewrite the preset for the whole process.
        first = caption_presets.resolve("streamer_lime")
        first["font_color"] = "#000000"
        assert caption_presets.resolve("streamer_lime")["font_color"] != "#000000"

    def test_metadata_keys_never_leak_into_the_style(self):
        # 'label'/'group' describe the preset, not the burn; passing them on to
        # generate_ass would raise TypeError.
        style = caption_presets.resolve("streamer_lime")
        assert "label" not in style
        assert "group" not in style


class TestDefaultIsUnchanged:
    """The refactor moved AUTO_CAPTION_STYLE out of subtitles.py into the preset
    table. Every clip ships with this look, so it must be byte-identical to the
    dict that was chosen by comparison testing on 25-jul-2026."""

    EXPECTED = {
        "style": "karaoke",
        "alignment": "bottom",
        "font_name": "Anton",
        "font_size": 44,
        "font_color": "#FFFFFF",
        "highlight_color": "#FFE500",
        "border_color": "#000000",
        "border_width": 4,
        "effect": "pop",
        "base_opacity": 1.0,
        "uppercase": True,
        "max_chars": 16,
        "max_duration": 1.4,
    }

    def test_every_original_field_is_preserved(self):
        resolved = caption_presets.resolve(caption_presets.DEFAULT_PRESET_ID)
        for key, value in self.EXPECTED.items():
            assert resolved[key] == value, f"default preset changed {key}"

    def test_subtitles_still_re_exports_the_constant(self):
        # main.auto_caption_clip does `import subtitles; subtitles.AUTO_CAPTION_STYLE`.
        from subtitles import AUTO_CAPTION_STYLE
        for key, value in self.EXPECTED.items():
            assert AUTO_CAPTION_STYLE[key] == value


class TestStreamerLooks:
    def test_lime_preset_matches_the_reference_frames(self):
        style = caption_presets.resolve("streamer_lime")
        assert style["uppercase"] is True
        assert style["font_color"] == style["highlight_color"], \
            "at one word per block there is no inactive text, so body and " \
            "highlight must agree or the look depends on which one wins"
        assert style["border_width"] >= 4, "reference has a heavy black stroke"
        assert style["border_color"] == "#000000"

    @pytest.mark.parametrize("preset_id", ["streamer_lime", "streamer_clean"])
    def test_one_word_per_screen(self, preset_id, tmp_path):
        # The single-word look is produced purely by max_chars=1 driving
        # _collect_word_blocks. Assert the generated ASS really does show one
        # word at a time rather than trusting the constant.
        style = caption_presets.resolve(preset_id)
        assert style["max_chars"] == 1

        words = [_w(" HE", 0.0, 0.4), _w(" ACTUALLY", 0.4, 0.9),
                 _w(" RISKED", 0.9, 1.5)]
        out = tmp_path / "subs.ass"
        assert generate_ass(
            _transcript(words), 0, 5, str(out),
            max_chars=style["max_chars"], max_duration=style["max_duration"],
            font_color=style["font_color"],
            highlight_color=style["highlight_color"],
            uppercase=style["uppercase"], effect=style["effect"],
        ) is True

        dialogues = [l for l in out.read_text(encoding="utf-8-sig").splitlines()
                     if l.startswith("Dialogue:")]
        assert dialogues, "no dialogue events generated"
        # Each event renders its whole block. One word per block means no event
        # may contain two of the source words.
        for line in dialogues:
            text = line.split(",,", 1)[1]
            present = [w for w in ("HE", "ACTUALLY", "RISKED") if w in text]
            assert len(present) == 1, \
                f"event shows {present}, expected exactly one word: {text}"

    def test_a_streamer_group_exists_for_the_ui(self):
        groups = {p["group"] for p in caption_presets.catalog()}
        assert "streamer" in groups
        streamers = [p for p in caption_presets.catalog()
                     if p["group"] == "streamer"]
        assert len(streamers) >= 3, "the ask was a variety of streamer looks"


class TestCatalog:
    def test_entries_carry_id_label_group_and_resolved_style(self):
        for entry in caption_presets.catalog():
            assert entry["id"] in caption_presets.PRESETS
            assert entry["label"]
            assert entry["group"]
            assert entry["style"] == caption_presets.resolve(entry["id"])

    def test_ids_are_unique(self):
        ids = [e["id"] for e in caption_presets.catalog()]
        assert len(ids) == len(set(ids))

    def test_default_is_in_the_catalog(self):
        ids = {e["id"] for e in caption_presets.catalog()}
        assert caption_presets.DEFAULT_PRESET_ID in ids

    def test_is_json_serializable(self):
        # It goes out over /api/caption-presets.
        import json
        json.loads(json.dumps(caption_presets.catalog()))


class TestRequestMerge:
    """How a named preset combines with fields the caller also sent.

    The rule is preset-as-base, explicit-wins. Getting this wrong in either
    direction is silently bad: if the preset always won, "pick Streamer Lime
    then make it cyan" would ignore the cyan; if the request always won, every
    preset would be flattened by SubtitleRequest's own defaults, because Pydantic
    fills them in whether or not the client sent anything.
    """

    @staticmethod
    def _req(**kwargs):
        from app import SubtitleRequest
        return SubtitleRequest(job_id="j", clip_index=0, **kwargs)

    @staticmethod
    def _resolve(req):
        from app import _resolved_caption_style
        return _resolved_caption_style(req)

    def test_no_preset_preserves_legacy_behaviour(self):
        # Older clients send no preset at all. Their burn must be unchanged,
        # including the grouping the ASS generator has always defaulted to.
        style = self._resolve(self._req())
        assert style["style"] == "classic"
        assert style["font_name"] == "Verdana"
        assert style["fontsize"] == 16
        assert style["alignment"] == "bottom"
        assert style["max_chars"] == 20
        assert style["max_duration"] == 2.0

    def test_preset_supplies_every_style_field(self):
        style = self._resolve(self._req(preset="streamer_lime"))
        expected = caption_presets.resolve("streamer_lime")
        assert style["font_color"] == expected["font_color"]
        assert style["border_width"] == expected["border_width"]
        assert style["uppercase"] == expected["uppercase"]
        assert style["effect"] == expected["effect"]
        assert style["style"] == expected["style"]

    def test_preset_carries_the_block_grouping(self):
        # max_chars/max_duration have no request field, so a preset is the only
        # way to reach them — this is what makes one-word-per-screen reachable
        # from the API at all.
        style = self._resolve(self._req(preset="streamer_lime"))
        assert style["max_chars"] == 1
        assert style["max_duration"] == 1.0

    def test_explicit_field_overrides_the_preset(self):
        style = self._resolve(
            self._req(preset="streamer_lime", font_color="#00E5FF"))
        assert style["font_color"] == "#00E5FF"
        # Everything the caller did NOT send still comes from the preset.
        assert style["border_width"] == \
            caption_presets.resolve("streamer_lime")["border_width"]

    def test_explicit_value_equal_to_a_default_still_wins(self):
        # The subtle case: the caller explicitly asks for Verdana, which happens
        # to equal SubtitleRequest's default. Only model_fields_set can tell
        # this apart from "sent nothing", and it must not be overwritten by the
        # preset's Anton.
        style = self._resolve(self._req(preset="streamer_lime", font_name="Verdana"))
        assert style["font_name"] == "Verdana"

    def test_position_maps_onto_alignment(self):
        # The request speaks the modal's language, generate_ass speaks ASS's.
        style = self._resolve(self._req(preset="streamer_lime", position="top"))
        assert style["alignment"] == "top"

    def test_unknown_preset_does_not_raise(self):
        style = self._resolve(self._req(preset="nope"))
        assert style["font_name"] == \
            caption_presets.resolve(caption_presets.DEFAULT_PRESET_ID)["font_name"]

    def test_result_is_accepted_by_generate_ass(self):
        # The whole point of keying the merge output to generate_ass's parameter
        # names: it must splat straight in without a translation step.
        import inspect
        style = self._resolve(self._req(preset="streamer_hype"))
        accepted = set(inspect.signature(generate_ass).parameters)
        passed = set(style) - {"style"}  # 'style' picks the generator, not a kwarg
        assert passed <= accepted, f"generate_ass rejects {passed - accepted}"

    def test_every_preset_survives_the_round_trip(self, tmp_path):
        # End to end for the whole table: resolve through the request merge and
        # actually generate an ASS file with it.
        words = [_w(" ONE", 0.0, 0.4), _w(" TWO", 0.4, 0.9)]
        for preset_id in caption_presets.PRESETS:
            style = self._resolve(self._req(preset=preset_id))
            kwargs = {k: v for k, v in style.items() if k != "style"}
            out = tmp_path / f"{preset_id}.ass"
            assert generate_ass(
                _transcript(words), 0, 5, str(out), **kwargs) is True, \
                f"preset {preset_id} failed to generate"
            assert out.read_text(encoding="utf-8-sig").startswith("[Script Info]")


class TestFontRegistry:
    """The fonts the UI is allowed to offer.

    The list moved server-side so it could be tested at all. It could not be
    before: the offered fonts lived in a JS array while the only guard checked the
    single default in AUTO_CAPTION_STYLE, so a family the image cannot resolve
    would look correct in the browser preview and burn as DejaVu with no error
    anywhere (issue #57).

    These are structural checks. verify_fonts.py is the behavioural one — it burns
    a probe caption per family and reads libass's fontselect output, which is the
    only way to see the family that was actually used.
    """

    def test_every_bundled_font_file_exists(self):
        for family, filename in caption_presets.BUNDLED_FONTS.items():
            path = os.path.join(FONTS_DIR, filename)
            assert os.path.isfile(path), \
                f"{family} declares {filename}, which is not in fonts/"

    def test_bundled_font_files_are_real_fonts(self):
        # A failed download is the realistic way this breaks: GitHub answers a
        # bad path with an HTML 404 page, curl -o writes it, and the result is a
        # plausibly sized file that is not a font. That happened during this
        # change with LuckiestGuy. Check the sfnt magic rather than the size.
        valid_magic = {
            b"\x00\x01\x00\x00",  # TrueType outlines
            b"true",              # legacy Apple TrueType
            b"ttcf",              # TrueType collection
            b"OTTO",              # CFF outlines
        }
        for family, filename in caption_presets.BUNDLED_FONTS.items():
            with open(os.path.join(FONTS_DIR, filename), "rb") as handle:
                magic = handle.read(4)
            assert magic in valid_magic, \
                f"{family} ({filename}) is not an sfnt font; first bytes {magic!r}"

    @pytest.mark.parametrize(
        "family", [c["family"] for c in caption_presets.FONT_CHOICES])
    def test_every_offered_font_resolves(self, family):
        assert family in _resolvable_font_families(), (
            f"the UI offers {family!r}, which has no bundled TTF, is not in the "
            f"image, and has no fontconfig alias — the burn will fall back to "
            f"DejaVu while the browser preview shows the real face"
        )

    def test_fontless_families_are_aliased_locally(self):
        # Families with no bundled TTF must be named in our own fontmap rather
        # than relying on the base image's defaults. Debian's
        # 30-metric-aliases.conf happens to cover the MS core fonts today, so
        # this passes either way right now — the point is that it keeps passing
        # on a base image whose defaults differ.
        bundled = set(caption_presets.BUNDLED_FONTS)
        aliased = _aliased_font_families()
        for choice in caption_presets.FONT_CHOICES:
            family = choice["family"]
            if family in bundled or family in caption_presets.IMAGE_FONTS:
                continue
            assert family in aliased, \
                f"{family} has no bundled file and no local alias"

    def test_offered_fonts_are_unique(self):
        families = [c["family"] for c in caption_presets.FONT_CHOICES]
        assert len(families) == len(set(families))

    def test_catalog_is_serializable_and_complete(self):
        import json
        catalog = caption_presets.font_catalog()
        json.loads(json.dumps(catalog))
        for entry in catalog:
            assert entry["family"]
            assert entry["label"]
            assert entry["group"] in {"display", "text"}

    def test_catalog_returns_copies(self):
        # The endpoint hands these straight out; a caller mutating one must not
        # rewrite the module-level table for the whole process.
        caption_presets.font_catalog()[0]["label"] = "mutated"
        assert caption_presets.font_catalog()[0]["label"] != "mutated"

    def test_a_display_font_is_offered(self):
        groups = {c["group"] for c in caption_presets.FONT_CHOICES}
        assert "display" in groups

    def test_presets_only_use_offered_fonts(self):
        # Otherwise a preset lands the user on a font they cannot then pick back
        # after nudging something else.
        offered = {c["family"] for c in caption_presets.FONT_CHOICES}
        for preset_id in caption_presets.PRESETS:
            font = caption_presets.resolve(preset_id)["font_name"]
            assert font in offered, \
                f"preset {preset_id} uses {font!r}, which the UI does not offer"


class TestVerticalPlacement:
    """Free-form vertical placement via MarginV.

    `position` picks the anchor edge and `margin_v` is the distance from it, both
    in ASS PlayResY=288 units. Measured on a real burn the relationship is
    linear — text centre lands near 1 - margin_v/288 - 0.06 of frame height on a
    bottom anchor — so these tests only need to prove the value reaches the
    generators and is honoured. verify_placement.py covers the pixel geometry.
    """

    @staticmethod
    def _style_line(path):
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        return next(l for l in lines if l.startswith("Style: Default"))

    def test_default_margin_clears_the_platform_ui(self):
        from subtitles import SAFE_MARGIN_V
        assert caption_presets.BASE_STYLE["margin_v"] == SAFE_MARGIN_V

    @pytest.mark.parametrize("preset_id", sorted(caption_presets.PRESETS))
    def test_every_preset_carries_a_margin(self, preset_id):
        margin = caption_presets.resolve(preset_id)["margin_v"]
        assert isinstance(margin, int)
        assert 0 <= margin <= 200

    @pytest.mark.parametrize("margin", [0, 43, 120, 200])
    def test_margin_reaches_the_ass_style_line(self, margin, tmp_path):
        out = tmp_path / "subs.ass"
        words = [_w(" ONE", 0.0, 0.5)]
        assert generate_ass(_transcript(words), 0, 5, str(out),
                            margin_v=margin) is True
        # Format is ...,Alignment,MarginL,MarginR,MarginV,Encoding
        assert self._style_line(out).endswith(f",10,10,{margin},1")

    def test_out_of_range_margin_is_clamped_not_rejected(self, tmp_path):
        # A caption must never be lost to a bad number; both generators clamp.
        out = tmp_path / "subs.ass"
        words = [_w(" ONE", 0.0, 0.5)]
        assert generate_ass(_transcript(words), 0, 5, str(out),
                            margin_v=9999) is True
        assert self._style_line(out).endswith(",10,10,200,1")

    def test_burn_accepts_margin_on_the_classic_path(self, monkeypatch, tmp_path):
        # burn_subtitles used to hardcode MarginV=SAFE_MARGIN_V into force_style,
        # so the classic path ignored placement entirely. It takes the parameter
        # now; assert it lands in the filter rather than the old constant.
        import subtitles

        captured = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = " ".join(cmd)
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        monkeypatch.setattr(subtitles.subprocess, "run", fake_run)
        srt = tmp_path / "subs.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n\n", encoding="utf-8-sig")
        subtitles.burn_subtitles(str(tmp_path / "in.mp4"), str(srt),
                                 str(tmp_path / "out.mp4"), margin_v=150)
        assert "MarginV=150" in captured["cmd"]

    def test_ass_burn_still_ignores_force_style(self, monkeypatch, tmp_path):
        # The .ass branch must not gain a force_style just because margin_v is
        # now a parameter — that would flatten the per-word colour tags.
        import subtitles

        captured = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = " ".join(cmd)
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        monkeypatch.setattr(subtitles.subprocess, "run", fake_run)
        ass = tmp_path / "subs.ass"
        ass.write_text("[Script Info]\n", encoding="utf-8-sig")
        subtitles.burn_subtitles(str(tmp_path / "in.mp4"), str(ass),
                                 str(tmp_path / "out.mp4"), margin_v=150)
        assert "force_style" not in captured["cmd"]

    def test_seam_anchoring_survives_a_custom_margin(self, tmp_path):
        # On a SPLIT stretch the captions ride the seam between two stacked
        # speakers via an inline {\an5}, which ASS renders ignoring MarginV. A
        # user-chosen margin must not dislodge that.
        out = tmp_path / "subs.ass"
        words = [_w(" ONE", 0.0, 0.4), _w(" TWO", 0.4, 0.9)]
        assert generate_ass(_transcript(words), 0, 5, str(out),
                            margin_v=200, split_ranges=[(0.0, 1.0)]) is True
        text = out.read_text(encoding="utf-8-sig")
        dialogues = [l for l in text.splitlines() if l.startswith("Dialogue:")]
        assert dialogues
        assert all("{\\an5}" in l for l in dialogues), \
            "seam anchoring lost when a custom margin is set"
        # The style still records the requested margin; the inline tag overrides
        # it only for the events inside the split range.
        assert self._style_line(out).endswith(",10,10,200,1")

    def test_margin_flows_through_the_request_merge(self):
        from app import SubtitleRequest, _resolved_caption_style
        req = SubtitleRequest(job_id="j", clip_index=0, margin_v=120)
        assert _resolved_caption_style(req)["margin_v"] == 120

    def test_preset_margin_applies_when_caller_is_silent(self):
        from app import SubtitleRequest, _resolved_caption_style
        req = SubtitleRequest(job_id="j", clip_index=0, preset="streamer_lime")
        expected = caption_presets.resolve("streamer_lime")["margin_v"]
        assert _resolved_caption_style(req)["margin_v"] == expected

    def test_explicit_margin_overrides_the_preset(self):
        from app import SubtitleRequest, _resolved_caption_style
        req = SubtitleRequest(job_id="j", clip_index=0,
                              preset="streamer_lime", margin_v=10)
        assert _resolved_caption_style(req)["margin_v"] == 10
