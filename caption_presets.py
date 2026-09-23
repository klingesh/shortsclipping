"""Named caption looks, shared by the pipeline, the API and the dashboard.

This module is the single source of truth for caption presets. The dashboard
fetches the table over ``GET /api/caption-presets`` instead of keeping its own
copy: the previous hardcoded ``CAPTION_PRESETS`` array in SubtitleModal.jsx
could drift from what the server actually burned, and the Remotion preview
would then lie about the output. One table, fetched at runtime, cannot drift.

Every preset resolves to the keyword arguments ``subtitles.generate_ass``
already accepts, so adding a look requires no change to the ASS generator.

Font names must resolve in the Docker image. A preset naming a font with no
bundled TTF and no fontconfig alias burns as DejaVu while the browser preview
shows the real face — see fonts/openshorts-fontmap.conf and
tests/test_subtitles.py::test_font_is_one_the_image_actually_ships.
"""

import copy


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
# Family names are declared explicitly rather than derived from filenames,
# because the two disagree for every multi-word family: ArchivoBlack-Regular.ttf
# reports "Archivo Black", BebasNeue-Regular.ttf reports "Bebas Neue". libass
# matches on the family, so deriving it from the filename picks a name nothing
# resolves and the burn silently falls back to DejaVu (issue #57).
#
# Values are the file in fonts/ that provides the family. subtitles.burn_subtitles
# passes fontsdir=<repo>/fonts, so libass finds these directly and no fontconfig
# alias is needed for them.
BUNDLED_FONTS = {
    "Anton": "Anton-Regular.ttf",
    "Archivo Black": "ArchivoBlack-Regular.ttf",
    "Bebas Neue": "BebasNeue-Regular.ttf",
    "Luckiest Guy": "LuckiestGuy-Regular.ttf",
    "Noto Serif": "NotoSerif-Bold.ttf",
}

# Families the Docker image installs via apt (fonts-liberation), plus the
# fallback fontconfig always has. Not in fonts/, but resolvable.
IMAGE_FONTS = {
    "Liberation Sans",
    "Liberation Serif",
    "Liberation Mono",
    "DejaVu Sans",
}

# The font list the dashboard offers, served alongside the presets so the choice
# can be validated server-side. It was previously a hardcoded array in
# SubtitleModal.jsx, which nothing could test: the repo's only font guard checked
# the single default in AUTO_CAPTION_STYLE, so an offered font that the image
# could not resolve would burn as DejaVu with no error anywhere (issue #57).
# Every family here resolves — verified with libass fontselect output, not
# assumed — by shipping in fonts/, in the image, or through a fontconfig alias.
FONT_CHOICES = [
    # Display faces, the ones worth using on a short.
    {"family": "Anton", "label": "Anton", "group": "display"},
    {"family": "Bebas Neue", "label": "Bebas Neue", "group": "display"},
    {"family": "Archivo Black", "label": "Archivo Black", "group": "display"},
    {"family": "Luckiest Guy", "label": "Luckiest Guy", "group": "display"},
    {"family": "Impact", "label": "Impact", "group": "display"},
    # Generic faces, kept because the modal has always offered them. Each is
    # aliased onto a metric-compatible Liberation face in the fontmap.
    {"family": "Verdana", "label": "Verdana", "group": "text"},
    {"family": "Arial", "label": "Arial", "group": "text"},
    {"family": "Helvetica", "label": "Helvetica", "group": "text"},
    {"family": "Georgia", "label": "Georgia", "group": "text"},
    {"family": "Courier New", "label": "Courier New", "group": "text"},
]


def font_catalog():
    """The font list for the dashboard, in display order."""
    return [dict(choice) for choice in FONT_CHOICES]


# Field defaults every preset inherits. Spelling them out here means a preset
# can only ever override, never omit — a half-populated style dict used to be
# possible via the dashboard table, which set no font_size at all and silently
# left it at whatever the modal's frozen useState held.
BASE_STYLE = {
    "style": "karaoke",          # karaoke (per-word highlight) | classic (uniform)
    "alignment": "bottom",       # top | middle | bottom
    "font_name": "Anton",
    "font_size": 44,             # PlayResY=288 units, NOT pixels (see generate_ass)
    "font_color": "#FFFFFF",
    "highlight_color": "#FFE500",
    "border_color": "#000000",
    "border_width": 4,
    "bg_color": "#000000",
    "bg_opacity": 0.0,
    "effect": "pop",             # none | glow | pop | box
    "base_opacity": 1.0,
    "uppercase": True,
    "max_chars": 16,
    "max_duration": 1.4,
    # Distance from the edge named by `alignment`, in PlayResY=288 units — the
    # same scale as font_size, not pixels. 43 is ~15% of frame height, which is
    # what clears TikTok's and Reels' own bottom chrome (subtitles.SAFE_MARGIN_V).
    # Measured behaviour on a bottom anchor is linear: the text centre lands at
    # roughly 1 - margin_v/288 - 0.06 of frame height, so 0 sits on the bottom
    # edge and 220 reaches the upper third. See verify_placement.py.
    "margin_v": 43,
}


# ``max_chars=1`` forces one word per block: _collect_word_blocks starts a new
# block as soon as the accumulated length exceeds max_chars, and the first word
# already does. This is what produces the single-word-on-screen look rather
# than a wrapped line with a moving highlight.
_ONE_WORD = {"max_chars": 1, "max_duration": 1.0}


# The streamer-clip family. These are built for reaction/gaming clips where the
# caption is the punchline: one word at a time, heavy outline, saturated colour
# that does not occur in footage.
_STREAMER_PRESETS = {
    "streamer_lime": {
        "label": "Streamer Lime",
        "group": "streamer",
        # Matches the user's reference frames: a single lime word under the
        # clip. font_color carries the colour, not highlight_color — at one
        # word per block there is no inactive text for a highlight to contrast
        # against, so colouring only the active word would render identically
        # while making the preset confusing to edit.
        "font_color": "#7CFC00",
        "highlight_color": "#7CFC00",
        "border_width": 5,
        "effect": "pop",
        **_ONE_WORD,
    },
    "streamer_hype": {
        "label": "Streamer Hype",
        "group": "streamer",
        "font_color": "#FFFFFF",
        "highlight_color": "#FFE500",
        "border_width": 5,
        "effect": "pop",
        "max_chars": 14,
        "max_duration": 1.2,
    },
    "streamer_alert": {
        "label": "Streamer Alert",
        "group": "streamer",
        "font_color": "#FFFFFF",
        "highlight_color": "#FF2D2D",
        "border_width": 5,
        "effect": "box",
        "max_chars": 14,
        "max_duration": 1.2,
    },
    "streamer_cyan": {
        "label": "Streamer Cyan",
        "group": "streamer",
        "font_color": "#FFFFFF",
        "highlight_color": "#00E5FF",
        "border_width": 4,
        "effect": "glow",
        "max_chars": 14,
        "max_duration": 1.2,
    },
    "streamer_clean": {
        "label": "Streamer Clean",
        "group": "streamer",
        "font_color": "#FFFFFF",
        "highlight_color": "#FFFFFF",
        "border_width": 4,
        "effect": "none",
        **_ONE_WORD,
    },
    # The three below exist to give the display faces somewhere to be used.
    # Anton is condensed and reads as the "Impact" look; these cover the other
    # common short-form shapes.
    "streamer_bebas": {
        "label": "Bebas Tall",
        "group": "streamer",
        # Bebas Neue is caps-only by design, so uppercase is redundant but
        # harmless, and keeping it true means toggling the font alone never
        # changes the casing of the burn.
        "font_name": "Bebas Neue",
        "font_color": "#FFFFFF",
        "highlight_color": "#FFE500",
        "border_width": 5,
        "effect": "pop",
        "max_chars": 14,
        "max_duration": 1.2,
    },
    "streamer_block": {
        "label": "Block",
        "group": "streamer",
        # Archivo Black is much wider than Anton at the same nominal size, so it
        # needs a smaller value and fewer characters per block to stay in frame.
        "font_name": "Archivo Black",
        "font_size": 34,
        "font_color": "#FFFFFF",
        "highlight_color": "#00E5FF",
        "border_width": 5,
        "effect": "pop",
        "max_chars": 12,
        "max_duration": 1.2,
    },
    "streamer_toon": {
        "label": "Toon",
        "group": "streamer",
        "font_name": "Luckiest Guy",
        "font_size": 38,
        "font_color": "#FFFFFF",
        "highlight_color": "#FF2D2D",
        "border_width": 5,
        "effect": "pop",
        "max_chars": 12,
        "max_duration": 1.2,
    },
}


# The looks that shipped as the dashboard's hardcoded table, preserved so
# existing muscle memory and any saved settings keep working. Their original
# entries set no font_size and no explicit font_color; both now come from
# BASE_STYLE, which is why they burn heavier than they used to. Verdana is
# aliased to Liberation Sans in the image, so it resolves.
_PLATFORM_PRESETS = {
    "tiktok": {
        "label": "TikTok", "group": "platform", "font_name": "Verdana",
        "highlight_color": "#FE2C55", "base_opacity": 0.75, "uppercase": False,
        "border_width": 2, "effect": "none",
    },
    "reels": {
        "label": "Reels", "group": "platform", "font_name": "Verdana",
        "highlight_color": "#E1306C", "base_opacity": 0.7, "uppercase": False,
        "border_width": 2, "effect": "none",
    },
    "shorts": {
        "label": "Shorts Pop", "group": "platform", "font_name": "Verdana",
        "highlight_color": "#FF0000", "base_opacity": 0.7, "uppercase": False,
        "border_width": 2, "effect": "pop",
    },
    "gold": {
        "label": "Gold Glow", "group": "classic", "font_name": "Verdana",
        "highlight_color": "#FFD700", "base_opacity": 0.6, "uppercase": False,
        "border_width": 2, "effect": "glow",
    },
    "neon": {
        "label": "Neon", "group": "classic", "font_name": "Verdana",
        "highlight_color": "#00FF88", "base_opacity": 0.55, "uppercase": False,
        "border_width": 2, "effect": "glow",
    },
    "cyber": {
        "label": "Cyber", "group": "classic", "font_name": "Verdana",
        "highlight_color": "#00FFFF", "base_opacity": 0.5, "uppercase": False,
        "border_width": 2, "effect": "glow",
    },
    "karaoke": {
        "label": "Karaoke", "group": "classic", "font_name": "Verdana",
        "highlight_color": "#FF6B6B", "base_opacity": 0.6, "uppercase": False,
        "border_width": 2, "effect": "none",
    },
    "minimal": {
        "label": "Minimal", "group": "classic", "font_name": "Verdana",
        "highlight_color": "#FFFFFF", "base_opacity": 0.65, "uppercase": False,
        "border_width": 1, "effect": "none",
    },
    "beast": {
        "label": "Beast", "group": "classic", "font_name": "Impact",
        "highlight_color": "#FFD700", "base_opacity": 1.0, "uppercase": True,
        "border_width": 3, "effect": "pop",
    },
    "boxed": {
        "label": "Boxed", "group": "classic", "font_name": "Verdana",
        "highlight_color": "#7C3AED", "base_opacity": 0.85, "uppercase": False,
        "border_width": 2, "effect": "box",
    },
    "classic": {
        "label": "Classic", "group": "classic", "font_name": "Verdana",
        "style": "classic", "highlight_color": "#FFD700", "base_opacity": 1.0,
        "uppercase": False, "border_width": 2, "effect": "none",
    },
}


# The look every generated clip gets when nobody picks anything. Chosen by
# rendering four candidates on a real clip and comparing them (25-jul-2026):
# white Anton uppercase with a yellow active word, heavy black outline, gentle
# pop. Yellow because it is the one colour that almost never occurs in footage,
# so the active word reads instantly on any background; the base text stays
# fully opaque (dimming it tested worse over bright scenes).
DEFAULT_PRESET_ID = "auto"

PRESETS = {
    DEFAULT_PRESET_ID: {"label": "Default", "group": "default"},
    **_STREAMER_PRESETS,
    **_PLATFORM_PRESETS,
}

# Keys that describe the preset itself rather than the burn.
_METADATA_KEYS = ("label", "group")


def resolve(preset_id):
    """Return the full style dict for ``preset_id``.

    Unknown ids fall back to the default rather than raising: a stale preset id
    from a saved project or an older dashboard build must not cost the user
    their captions, which is the same fail-open posture auto_caption_clip takes.
    """
    overrides = PRESETS.get(str(preset_id or "").strip(), None)
    if overrides is None:
        overrides = PRESETS[DEFAULT_PRESET_ID]
    style = copy.deepcopy(BASE_STYLE)
    style.update({k: v for k, v in overrides.items() if k not in _METADATA_KEYS})
    return style


def exists(preset_id):
    """True when ``preset_id`` names a real preset (no fallback applied)."""
    return str(preset_id or "").strip() in PRESETS


def catalog():
    """Serializable preset list for the dashboard.

    Each entry carries the resolved style so the client never has to know the
    inheritance rules, and the Remotion preview can be driven straight from it.
    """
    return [
        {
            "id": preset_id,
            "label": overrides.get("label", preset_id),
            "group": overrides.get("group", "classic"),
            "style": resolve(preset_id),
        }
        for preset_id, overrides in PRESETS.items()
    ]


# Backwards-compatible alias. main.auto_caption_clip and any external caller
# that imported the old constant keep working, and keep reading the same keys.
AUTO_CAPTION_STYLE = resolve(DEFAULT_PRESET_ID)
