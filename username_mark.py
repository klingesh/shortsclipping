"""The creator's own handle, burned into a clip.

This is NOT main.apply_watermark. That one stamps OpenShorts' branding onto free
-plan clips and deliberately sits at 40% of the height, inside the content band,
so cropping it out costs real footage. Doing that to the user's own handle would
be hostile: it is their mark, they must be able to move it, restyle it and take
it off again. So this follows the annotations pattern instead — a derived file
next to an untouched original, reversible without a re-encode.

Text is rasterised to an RGBA PNG with Pillow and composited by ffmpeg, the same
two-stage route hooks.py and annotations.py take. ffmpeg's drawtext was the
obvious alternative and is the wrong choice here: the text is arbitrary user
input, and a filtergraph gives meaning to colons, commas, quotes, brackets and
backslashes, so every handle containing one would have to be escaped correctly in
a parser that is not a shell. ffmpeg_utils' own docstring records an escaping
attempt that silently swallowed the following option. A PNG has no such surface,
and it also removes the dependency on the ffmpeg build carrying libfreetype.

Fonts are the ones the caption presets already offer, so a channel can match its
handle to its captions. Resolution goes through the bundled files first and
fontconfig second, and the path actually used is reported rather than assumed —
issue #57 was a font that silently rendered as DejaVu while the preview showed
the real face, and guessing here would reproduce it.
"""

import os
import re
import subprocess
import uuid

from ffmpeg_utils import video_encode_args, METADATA_SCRUB, QUALITY

import caption_presets

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# Where the mark sits. Nine named spots because that is how a watermark is
# actually placed — "bottom left, a bit in from the edge" — and free x/y is the
# escape hatch for anything else.
ANCHORS = (
    "top_left", "top_center", "top_right",
    "middle_left", "center", "middle_right",
    "bottom_left", "bottom_center", "bottom_right",
)
DEFAULT_ANCHOR = "bottom_left"

# Distance from the frame edge for an anchored mark, as a fraction of width.
DEFAULT_MARGIN = 0.05
MIN_MARGIN = 0.0
MAX_MARGIN = 0.45

# Cap font size as a fraction of frame WIDTH so the handle keeps its weight at
# any resolution. 0.045 of 1080 is ~49px, which reads on a phone without
# shouting.
DEFAULT_SIZE = 0.045
MIN_SIZE = 0.01
MAX_SIZE = 0.30

DEFAULT_COLOR = "#00E5FF"        # the cyan the reference edits use
DEFAULT_OUTLINE_COLOR = "#000000"

# Outline as a fraction of the font size. A handle has to stay legible over
# whatever is behind it, and a thin dark edge does that without a box.
DEFAULT_OUTLINE = 0.12
MIN_OUTLINE = 0.0
MAX_OUTLINE = 0.5

DEFAULT_OPACITY = 0.9

# A handle, not a paragraph. Long enough for "@some_channel_name_2026", short
# enough that it cannot become a caption by the back door.
MAX_TEXT_LENGTH = 40

SUPERSAMPLE = 2   # Pillow antialiases text, so 2x is enough to clean the edges

# Control characters and line breaks. A watermark is one line; a newline would
# silently change the layout, and C0/C1 controls can do stranger things.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Codepoints the text fonts have no glyphs for, so they would draw as tofu.
# hooks.py carries the full emoji renderer; this is only the detector, used to
# decide whether to ask hooks.py for help.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0E\U0000FE0F"
    "\U0000200D"
    "\U000020E3"
    "]+"
)


class MarkError(ValueError):
    pass


def _clamp(value, low, high, fallback):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


def _parse_color(value, *, field="colour"):
    """#RRGGBB or #RRGGBBAA to an RGBA tuple."""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 6:
        text += "FF"
    if len(text) != 8:
        raise MarkError(f"{field} must be #RRGGBB or #RRGGBBAA, got {value!r}")
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4, 6))
    except ValueError:
        raise MarkError(f"{field} is not hex: {value!r}")


def clean_text(value):
    """The handle, reduced to one line of text.

    Order matters. Tabs, newlines and carriage returns are control characters AND
    word separators, so deleting them as controls welds words together — "first\\n
    second" became "firstsecond", silently changing the name. They are turned into
    spaces first; only then are the remaining controls (which separate nothing)
    removed, and the runs collapsed.
    """
    text = str(value or "")
    text = re.sub(r"[\t\n\r\v\f\x85\u2028\u2029]", " ", text)
    text = _CONTROL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise MarkError("the watermark text is empty")
    return text[:MAX_TEXT_LENGTH]


def font_path_for(family):
    """(path, how) for a font family, or (None, reason).

    ``how`` is "bundled" when the file ships in fonts/ and "fontconfig" when it
    was resolved by fc-match. Returned rather than swallowed because a wrong
    answer here is invisible in the output — the text still renders, just in the
    wrong face.
    """
    filename = caption_presets.BUNDLED_FONTS.get(family)
    if filename:
        path = os.path.join(FONTS_DIR, filename)
        if os.path.exists(path):
            return path, "bundled"

    try:
        result = subprocess.run(
            ["fc-match", str(family), "--format=%{file}"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"fc-match unavailable ({type(exc).__name__})"
    path = (result.stdout or "").strip()
    if result.returncode == 0 and path and os.path.exists(path):
        return path, "fontconfig"
    return None, f"fontconfig could not resolve {family!r}"


def _load_font(family, size_px):
    """(font, path, how). Falls back to a bundled face rather than to Pillow's
    bitmap default, which ignores the size and would render a handle at 11px."""
    from PIL import ImageFont

    path, how = font_path_for(family)
    if path:
        try:
            return ImageFont.truetype(path, size_px), path, how
        except OSError:
            how = f"{path} could not be loaded"

    for fallback in ("Anton", "Liberation Sans", "DejaVu Sans"):
        if fallback == family:
            continue
        alt, _ = font_path_for(fallback)
        if not alt:
            continue
        try:
            return ImageFont.truetype(alt, size_px), alt, f"fallback ({how})"
        except OSError:
            continue
    raise MarkError(f"no usable font for {family!r}: {how}")


def _emoji_support(font_size):
    """hooks.py's emoji renderer, or None.

    Borrowed rather than duplicated: hooks.py already solves colour emoji,
    including the fixed-bitmap strike sizes NotoColorEmoji forces. Guarded
    because these are its internals — if they move, a handle with an emoji loses
    the emoji instead of raising, which is the better failure.
    """
    try:
        import hooks
        loader = hooks._load_emoji_font
        measure = hooks._measure_width
        draw_mixed = hooks._draw_mixed
    except (ImportError, AttributeError):
        return None
    emoji_font = loader(font_size)
    if emoji_font is None:
        return None
    return {"font": emoji_font, "measure": measure, "draw": draw_mixed}


def normalise_mark(raw):
    """Validate a watermark spec and fill in defaults."""
    if not isinstance(raw, dict):
        raise MarkError("the watermark spec is not an object")

    anchor = str(raw.get("anchor") or DEFAULT_ANCHOR).strip().lower()
    if anchor not in ANCHORS:
        raise MarkError(f"anchor must be one of {list(ANCHORS)}")

    family = str(raw.get("font") or caption_presets.BASE_STYLE["font_name"]).strip()

    # x/y are optional and override the anchor: a drag in the editor produces a
    # point, not a corner. None means "use the anchor", which is not the same as
    # 0.0 and so cannot be defaulted numerically.
    def optional_fraction(key):
        value = raw.get(key)
        if value is None:
            return None
        return _clamp(value, -0.5, 1.5, 0.5)

    start = raw.get("start")
    end = raw.get("end")
    start = None if start is None else max(0.0, _clamp(start, 0.0, 86400.0, 0.0))
    end = None if end is None else _clamp(end, 0.0, 86400.0, 0.0)
    if start is not None and end is not None and end <= start:
        raise MarkError(f"end ({end}) must be after start ({start})")

    # Colours are parsed here, at the boundary, and the original strings are
    # kept. Leaving it to render time made a mistyped colour a 500 from inside
    # the renderer instead of a 400 naming the field, which is the difference
    # between "the server broke" and "fix this input".
    colour = str(raw.get("color") or DEFAULT_COLOR)
    outline_colour = str(raw.get("outline_color") or DEFAULT_OUTLINE_COLOR)
    _parse_color(colour, field="colour")
    _parse_color(outline_colour, field="outline colour")

    return {
        "text": clean_text(raw.get("text")),
        "font": family,
        "size": _clamp(raw.get("size", DEFAULT_SIZE), MIN_SIZE, MAX_SIZE,
                       DEFAULT_SIZE),
        "color": colour,
        "opacity": _clamp(raw.get("opacity", DEFAULT_OPACITY), 0.05, 1.0,
                          DEFAULT_OPACITY),
        "outline_width": _clamp(raw.get("outline_width", DEFAULT_OUTLINE),
                                MIN_OUTLINE, MAX_OUTLINE, DEFAULT_OUTLINE),
        "outline_color": outline_colour,
        "anchor": anchor,
        "margin": _clamp(raw.get("margin", DEFAULT_MARGIN), MIN_MARGIN,
                         MAX_MARGIN, DEFAULT_MARGIN),
        "x": optional_fraction("x"),
        "y": optional_fraction("y"),
        "start": start,
        "end": end,
    }


def _measure(text, font, emoji, stroke_px):
    """(canvas_w, canvas_h, origin, bbox) for one line at a given font size.

    textbbox is asked for the stroke too: an outline grows the drawing past the
    glyph box, and ascenders/descenders mean the top is not at y=0. Sizing the
    canvas from font metrics alone clips both.
    """
    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    if emoji:
        text_w = emoji["measure"](probe, text, font, emoji["font"])
    else:
        text_w = probe.textlength(text, font=font)
    bbox = probe.textbbox((0, 0), text, font=font, stroke_width=stroke_px)
    pad = stroke_px + max(2, int(getattr(font, "size", 16)) // 10)
    canvas_w = max(1, int(round(max(text_w, bbox[2] - bbox[0]))) + pad * 2)
    canvas_h = max(1, int(round(bbox[3] - bbox[1])) + pad * 2)
    return canvas_w, canvas_h, (pad - bbox[0], pad - bbox[1]), bbox


def render_mark(mark, output_path, *, frame_width):
    """Write the handle as an RGBA PNG sized for a frame this wide.

    Returns (path, width, height, font_path, font_how).
    """
    from PIL import Image, ImageDraw

    text = mark["text"]
    fill = _parse_color(mark["color"], field="colour")
    stroke_fill = _parse_color(mark["outline_color"], field="outline colour")

    emoji_wanted = bool(_EMOJI_RE.search(text))
    font_px = max(8, int(round(mark["size"] * frame_width)))

    def build(px):
        big = max(8, px) * SUPERSAMPLE
        big_stroke = int(round(mark["outline_width"] * max(8, px))) * SUPERSAMPLE
        loaded, path, how = _load_font(mark["font"], big)
        emo = _emoji_support(big) if emoji_wanted else None
        return loaded, path, how, emo, big_stroke

    font, font_path, font_how, emoji, big_stroke = build(font_px)

    body = text
    if emoji_wanted and emoji is None:
        # Tofu boxes look like a bug in the export; dropping the emoji is
        # visibly a choice. Falls back to the first character so a handle made
        # only of emoji still produces something.
        body = re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", text)).strip() or text[:1]

    canvas_w, canvas_h, origin, _ = _measure(body, font, emoji, big_stroke)

    # Size is a fraction of frame width, but it sets the FONT size, and a long
    # handle at a large size is far wider than the frame — a 22-character name at
    # the maximum measured 3519px inside 1080px, so most of it was simply cut
    # off. Shrink to fit instead: a mark the user cannot read is not a watermark.
    # Text width is close to linear in font size, so one corrective pass lands
    # it, and the second guards against rounding leaving it a pixel over.
    shrunk_from = None
    for _ in range(2):
        limit = frame_width * SUPERSAMPLE
        if canvas_w <= limit:
            break
        shrunk_from = shrunk_from or font_px
        font_px = max(8, int(font_px * limit / canvas_w))
        font, font_path, font_how, emoji, big_stroke = build(font_px)
        canvas_w, canvas_h, origin, _ = _measure(body, font, emoji, big_stroke)

    image = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if emoji:
        emoji["draw"](image, draw, origin, body, font, emoji["font"],
                      fill=fill,
                      outline=(stroke_fill, big_stroke) if big_stroke else None)
    elif big_stroke:
        draw.text(origin, body, font=font, fill=fill,
                  stroke_width=big_stroke, stroke_fill=stroke_fill)
    else:
        draw.text(origin, body, font=font, fill=fill)

    final_w = max(1, min(frame_width, int(round(canvas_w / SUPERSAMPLE))))
    final_h = max(1, int(round(canvas_h / SUPERSAMPLE)))
    image = image.resize((final_w, final_h), Image.LANCZOS)
    image.save(output_path, "PNG")
    if shrunk_from is not None:
        print(f"   ℹ️ Watermark shrunk from {shrunk_from}px to {font_px}px "
              f"so '{body}' fits a {frame_width}px frame.")
    return output_path, final_w, final_h, font_path, font_how


def mark_position(mark, *, frame_width, frame_height, mark_width, mark_height):
    """Top-left pixel for the overlay, clamped to stay on screen.

    Free x/y place the mark's CENTRE, because that is what a drag produces. An
    anchor places its EDGE against the frame's, inset by the margin, because
    that is what "bottom left" means.
    """
    if mark["x"] is not None and mark["y"] is not None:
        x = mark["x"] * frame_width - mark_width / 2.0
        y = mark["y"] * frame_height - mark_height / 2.0
    else:
        inset = mark["margin"] * frame_width
        vertical, _, horizontal = mark["anchor"].partition("_")
        if horizontal == "left":
            x = inset
        elif horizontal == "right":
            x = frame_width - mark_width - inset
        else:
            x = (frame_width - mark_width) / 2.0

        if vertical == "top":
            y = inset
        elif vertical == "bottom":
            y = frame_height - mark_height - inset
        else:
            y = (frame_height - mark_height) / 2.0

    # A long handle at a large size can be wider than the frame; pin it to the
    # left/top rather than letting overlay push it off entirely.
    x = min(max(0.0, x), max(0.0, frame_width - mark_width))
    y = min(max(0.0, y), max(0.0, frame_height - mark_height))
    return int(round(x)), int(round(y))


def probe_video(path):
    """(width, height, duration) for a clip."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.STDOUT, timeout=60).decode().split()
    except (OSError, subprocess.SubprocessError) as exc:
        raise MarkError(f"could not probe {os.path.basename(path)}: {exc}")
    try:
        width, height = int(out[0]), int(out[1])
        duration = float(out[2]) if len(out) > 2 else 0.0
    except (IndexError, ValueError) as exc:
        raise MarkError(f"unexpected ffprobe output for "
                        f"{os.path.basename(path)}: {out!r} ({exc})")
    return width, height, duration


def build_filtergraph(mark, *, x, y, opacity):
    """The overlay graph. Returns the filter_complex string."""
    chain = ["format=rgba"]
    if opacity < 1.0:
        # colorchannelmixer scales the existing alpha, so it dims the
        # antialiased edges proportionally instead of hard-cutting them.
        chain.append(f"colorchannelmixer=aa={opacity:.4f}")
    graph = f"[1:v]{','.join(chain)}[mark];[0:v][mark]overlay=x={x}:y={y}"

    window = _enable_window(mark)
    if window:
        graph += f":enable='{window}'"
    return graph


def _enable_window(mark):
    """The overlay's enable= expression, or "" for the whole clip."""
    start, end = mark["start"], mark["end"]
    if start is None and end is None:
        return ""
    if end is None:
        return f"gte(t,{start:.3f})"
    if start is None:
        return f"lte(t,{end:.3f})"
    return f"between(t,{start:.3f},{end:.3f})"


def apply_mark(input_path, mark, output_path, *, workdir=None):
    """Burn the handle onto ``input_path``, writing ``output_path``.

    Returns a report: the resolved font and its path, the mark's pixel size and
    placement. The font is in there because "which face did it actually use" is
    not answerable from the video afterwards.
    """
    if not os.path.exists(input_path):
        raise MarkError(f"video not found: {input_path}")

    # Unconditional: normalise_mark is idempotent, so this costs nothing and
    # removes the question of whether the caller already did it.
    normalised = normalise_mark(mark)

    width, height, duration = probe_video(input_path)
    workdir = workdir or os.path.dirname(os.path.abspath(output_path))
    png_path = os.path.join(workdir, f"mark_{uuid.uuid4().hex[:8]}.png")

    try:
        _, mark_w, mark_h, font_path, font_how = render_mark(
            normalised, png_path, frame_width=width)
        x, y = mark_position(normalised, frame_width=width, frame_height=height,
                             mark_width=mark_w, mark_height=mark_h)
        graph = build_filtergraph(normalised, x=x, y=y,
                                  opacity=normalised["opacity"])

        cmd = ["ffmpeg", "-y", "-i", input_path, "-i", png_path,
               "-filter_complex", graph,
               *video_encode_args(QUALITY), "-c:a", "copy", *METADATA_SCRUB,
               "-movflags", "+faststart", output_path]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=1800)
        if result.returncode != 0 or not os.path.exists(output_path):
            err = (result.stderr or b"").decode(errors="ignore")[-500:]
            raise MarkError(f"ffmpeg failed to burn the watermark: {err}")
    finally:
        if os.path.exists(png_path):
            os.remove(png_path)

    return {
        "output": output_path,
        "mark": normalised,
        "font": normalised["font"],
        "font_path": font_path,
        "font_resolution": font_how,
        "mark_size": [mark_w, mark_h],
        "position": [x, y],
        "frame": [width, height],
        "duration": duration,
    }


def catalog():
    """What the dashboard needs to offer the control."""
    return {
        "anchors": list(ANCHORS),
        "default_anchor": DEFAULT_ANCHOR,
        "fonts": caption_presets.font_catalog(),
        "defaults": {
            "size": DEFAULT_SIZE,
            "color": DEFAULT_COLOR,
            "opacity": DEFAULT_OPACITY,
            "outline_width": DEFAULT_OUTLINE,
            "outline_color": DEFAULT_OUTLINE_COLOR,
            "margin": DEFAULT_MARGIN,
            "font": caption_presets.BASE_STYLE["font_name"],
        },
        "limits": {
            "size": [MIN_SIZE, MAX_SIZE],
            "opacity": [0.05, 1.0],
            "outline_width": [MIN_OUTLINE, MAX_OUTLINE],
            "margin": [MIN_MARGIN, MAX_MARGIN],
            "text_length": MAX_TEXT_LENGTH,
        },
    }
