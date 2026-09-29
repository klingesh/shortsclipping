"""Red circle and arrow overlays that stay on the subject.

Shapes are drawn as RGBA PNGs with Pillow and composited with ffmpeg, the same
two-stage pattern hooks.py uses for its text overlay. Drawing them rather than
shipping image assets is what makes size, colour, thickness and rotation
adjustable, and it avoids the trap the user's own asset folder fell into: five of
its eight overlay files are JPEGs, which cannot carry transparency, so they would
paint a white box over the video.

Placement is either fixed (a normalised x/y) or tracked, following a keypoint
from subject_track so the annotation holds on a moving face. A tracked overlay is
driven by an ffmpeg ``sendcmd`` file that rewrites the overlay's x/y over time.

Anchors matter and differ per shape. A circle is centred on its target; an arrow
has to land its TIP there, which also has to survive rotation. Getting that wrong
puts the arrowhead half a shape-width away from what it is pointing at.

Note on sendcmd: punch_in.sendcmd_lines is close to what is needed but emits w
and h alongside x and y, and the overlay filter accepts neither. Rather than
change a function in the reframe hot path, an x/y-only emitter lives here.
"""

import math
import os
import subprocess
import uuid

from ffmpeg_utils import (escape_filter_value, video_encode_args,
                          METADATA_SCRUB, QUALITY)

SHAPES = ("circle", "arrow")

DEFAULT_COLOR = "#FF2D2D"

# Shape size as a fraction of frame width, so an annotation keeps its visual
# weight whatever the clip's resolution.
DEFAULT_SIZE = 0.28
MIN_SIZE = 0.02
MAX_SIZE = 1.5

# Ring/stroke thickness as a fraction of the shape's own size.
DEFAULT_THICKNESS = 0.09
MIN_THICKNESS = 0.01
MAX_THICKNESS = 0.5

# Supersample factor for drawing. Pillow has no antialiasing on ellipse or
# polygon, so shapes are drawn large and scaled down, which is the standard
# workaround and what assets/make_watermark.py already does (it renders at 4x).
SUPERSAMPLE = 4


class AnnotationError(ValueError):
    pass


def _clamp(value, low, high, fallback):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


def _parse_color(value):
    """#RRGGBB or #RRGGBBAA to an RGBA tuple."""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 6:
        text += "FF"
    if len(text) != 8:
        raise AnnotationError(f"colour must be #RRGGBB or #RRGGBBAA, got {value!r}")
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4, 6))
    except ValueError:
        raise AnnotationError(f"colour is not hex: {value!r}")


def _rotate_point(point, center, degrees):
    """Rotate ``point`` about ``center`` the way PIL's rotate moves pixels.

    Image.rotate(angle) turns the picture counter-clockwise as displayed, while
    image y grows downward, so the matrix is the transpose of the familiar
    textbook one. Pinned down by the requirement that a downward vector (0, +d)
    must land on (d·sin θ, d·cos θ): θ=90 sends "down" to "right", θ=180 to "up".

    Getting these signs backwards puts a rotated arrow's tip on the opposite
    diagonal from its target, which is how this was first written.
    """
    radians = math.radians(degrees)
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    dx, dy = point[0] - center[0], point[1] - center[1]
    return (center[0] + dx * cos_a + dy * sin_a,
            center[1] - dx * sin_a + dy * cos_a)


def _draw_circle(draw, size, color, thickness_px):
    # Inset by half the stroke so the ring's outer edge sits on the canvas edge
    # instead of being clipped: PIL centres an outline on the path.
    half = thickness_px / 2.0
    draw.ellipse([half, half, size - half, size - half],
                 outline=color, width=max(1, int(round(thickness_px))))
    # Anchor: the centre, which is what a circle is "pointing at".
    return (size / 2.0, size / 2.0)


def _draw_arrow(draw, size, color, thickness_px):
    """A solid arrow pointing DOWN, tip at bottom-centre.

    Down is the base orientation because rotation is applied afterwards, so one
    drawing serves every direction. The reference frames use a fat head on a
    short shaft rather than a thin line, which reads at small sizes on video.
    """
    head_width = size * 0.72
    head_height = size * 0.52
    shaft_width = max(thickness_px, size * 0.26)

    tip = (size / 2.0, size * 0.98)
    head_top = size * 0.98 - head_height

    draw.polygon([
        (size / 2.0 - head_width / 2.0, head_top),
        (size / 2.0 + head_width / 2.0, head_top),
        tip,
    ], fill=color)
    draw.rectangle([
        size / 2.0 - shaft_width / 2.0, size * 0.04,
        size / 2.0 + shaft_width / 2.0, head_top + 1,
    ], fill=color)
    return tip


_DRAW = {"circle": _draw_circle, "arrow": _draw_arrow}


def render_shape(shape, output_path, *, size_px, color=DEFAULT_COLOR,
                 thickness=DEFAULT_THICKNESS, rotation=0.0):
    """Write an RGBA PNG of ``shape``.

    Returns ``(path, width, height, anchor_x, anchor_y)`` with the anchor in
    pixels within the written image: the point that must land on the target.
    """
    from PIL import Image, ImageDraw

    if shape not in _DRAW:
        raise AnnotationError(f"unknown shape {shape!r}; expected one of {SHAPES}")

    size_px = int(max(8, size_px))
    rgba = _parse_color(color)
    thickness = _clamp(thickness, MIN_THICKNESS, MAX_THICKNESS,
                       DEFAULT_THICKNESS)

    # Draw big, then scale down: Pillow does not antialias ellipse or polygon
    # edges, and a hard-edged red ring on video looks obviously synthetic.
    big = size_px * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    anchor = _DRAW[shape](draw, big, rgba, thickness * big)

    if rotation:
        centre = (big / 2.0, big / 2.0)
        rotated = image.rotate(rotation, resample=Image.BICUBIC, expand=True)
        # expand=True re-centres the content in a larger canvas, so the anchor
        # moves by both the rotation and the canvas growth.
        moved = _rotate_point(anchor, centre, rotation)
        grow_x = (rotated.width - big) / 2.0
        grow_y = (rotated.height - big) / 2.0
        anchor = (moved[0] + grow_x, moved[1] + grow_y)
        image = rotated

    final_w = max(1, int(round(image.width / SUPERSAMPLE)))
    final_h = max(1, int(round(image.height / SUPERSAMPLE)))
    image = image.resize((final_w, final_h), Image.LANCZOS)
    image.save(output_path, "PNG")

    return (output_path, final_w, final_h,
            anchor[0] / SUPERSAMPLE, anchor[1] / SUPERSAMPLE)


def normalise_annotation(raw, index=0):
    """Validate one annotation dict and fill in defaults."""
    if not isinstance(raw, dict):
        raise AnnotationError(f"annotation {index} is not an object")

    shape = str(raw.get("type") or raw.get("shape") or "").strip().lower()
    if shape not in SHAPES:
        raise AnnotationError(
            f"annotation {index}: type must be one of {list(SHAPES)}")

    start = max(0.0, _clamp(raw.get("start", 0.0), 0.0, 86400.0, 0.0))
    end = _clamp(raw.get("end", start + 1.5), 0.0, 86400.0, start + 1.5)
    if end <= start:
        raise AnnotationError(
            f"annotation {index}: end ({end}) must be after start ({start})")

    track = raw.get("track")
    track = str(track).strip() if track else None

    offset = raw.get("offset") or [0.0, 0.0]
    try:
        offset = [float(offset[0]), float(offset[1])]
    except (TypeError, ValueError, IndexError):
        offset = [0.0, 0.0]

    return {
        "type": shape,
        "start": start,
        "end": end,
        "size": _clamp(raw.get("size", DEFAULT_SIZE), MIN_SIZE, MAX_SIZE,
                       DEFAULT_SIZE),
        "color": str(raw.get("color") or DEFAULT_COLOR),
        "thickness": _clamp(raw.get("thickness", DEFAULT_THICKNESS),
                            MIN_THICKNESS, MAX_THICKNESS, DEFAULT_THICKNESS),
        "rotation": _clamp(raw.get("rotation", 0.0), -360.0, 360.0, 0.0),
        # Fixed placement, used when track is None or the track has no data.
        "x": _clamp(raw.get("x", 0.5), -1.0, 2.0, 0.5),
        "y": _clamp(raw.get("y", 0.5), -1.0, 2.0, 0.5),
        "track": track,
        "offset": offset,
    }


def normalise_annotations(raw_list):
    if not raw_list:
        return []
    return [normalise_annotation(raw, i) for i, raw in enumerate(raw_list)]


def sendcmd_xy(positions, fps, target):
    """sendcmd lines rewriting one overlay's x/y over time.

    ``positions``: list of (frame_index, x_px, y_px). Deduped to change points,
    like punch_in.sendcmd_lines: at 30fps a 45s clip is 1350 frames and writing
    both parameters every frame makes a command file big enough to slow the
    filter down.
    """
    lines = []
    previous = None
    for frame, x, y in positions:
        x, y = int(round(x)), int(round(y))
        if previous == (x, y):
            continue
        t = frame / float(fps) if fps else 0.0
        px, py = previous if previous else (None, None)
        if x != px:
            lines.append(f"{t:.4f} {target} x {x};")
        if y != py:
            lines.append(f"{t:.4f} {target} y {y};")
        previous = (x, y)
    return lines


def resolve_positions(annotation, track, *, width, height, fps,
                      anchor, duration):
    """Where the overlay's top-left corner goes, per sampled frame.

    Returns (positions, moving). ``positions`` is a list of
    (frame_index, x_px, y_px); ``moving`` says whether it changes, so a static
    annotation can skip sendcmd entirely.

    Overlay x/y address the image's top-left, but the meaningful point is the
    anchor (a circle's centre, an arrow's tip), so the anchor offset is
    subtracted here.
    """
    anchor_x, anchor_y = anchor
    offset_x, offset_y = annotation["offset"]

    def corner(nx, ny):
        return (nx * width + offset_x * width - anchor_x,
                ny * height + offset_y * height - anchor_y)

    if not annotation["track"] or not track:
        x, y = corner(annotation["x"], annotation["y"])
        return [(0, x, y)], False

    import subject_track

    # One position per track sample inside the annotation's window. Sampling on
    # the track's own grid rather than every frame keeps the command file small
    # and matches the resolution the data actually has.
    step = 1.0 / max(1.0, float(track.get("fps") or fps) /
                     max(1, int(track.get("stride") or 1)))
    positions = []
    t = annotation["start"]
    last = None
    while t <= min(annotation["end"], duration):
        point = subject_track.point_at(track, t, annotation["track"])
        if point is None:
            # No detection here. Hold the last known spot rather than snapping
            # to the fixed fallback, which would look like a glitch; the
            # enable= window still controls whether it is visible at all.
            point = last
        if point is not None:
            last = point
            positions.append((int(round(t * fps)), *corner(point[0], point[1])))
        t += step

    if not positions:
        x, y = corner(annotation["x"], annotation["y"])
        return [(0, x, y)], False
    return positions, len(positions) > 1


def probe_video(video_path):
    """(width, height, fps, duration) for a clip."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=60)
        values = [v for v in (out.stdout or "").strip().splitlines() if v]
        width, height = int(values[0]), int(values[1])
        num, _, den = values[2].partition("/")
        fps = float(num) / float(den or 1)
        duration = float(values[3])
        return width, height, fps, duration
    except (subprocess.SubprocessError, ValueError, IndexError) as exc:
        raise AnnotationError(
            f"could not probe {os.path.basename(video_path)}: {exc}")


def build_filtergraph(entries, *, cmd_path=None):
    """The filter_complex for a list of prepared overlay entries.

    Each entry: {"input": n, "positions": [...], "moving": bool, "label": str,
                 "start": float, "end": float, "x": int, "y": int}
    Returned separately from the ffmpeg call so it can be tested without media.
    """
    parts = []
    current = "[0:v]"
    if cmd_path:
        # sendcmd has to sit in the chain ahead of the filters it addresses; it
        # routes by label, so one instance drives every moving overlay.
        parts.append(f"[0:v]sendcmd=f='{escape_filter_value(cmd_path)}'[base]")
        current = "[base]"

    for i, entry in enumerate(entries):
        out = f"[v{i}]" if i < len(entries) - 1 else "[out]"
        gate = f":enable='between(t,{entry['start']:.3f},{entry['end']:.3f})'"
        parts.append(
            f"{current}[{entry['input']}:v]"
            f"overlay@{entry['label']}=x={entry['x']}:y={entry['y']}{gate}{out}")
        current = out
    return ";".join(parts)


def apply_annotations(video_path, annotations, output_path, *, track=None,
                      workdir=None):
    """Burn ``annotations`` onto ``video_path``.

    ``track`` is a subject_track payload; annotations with a ``track`` keypoint
    follow it, the rest sit at their fixed x/y.
    """
    annotations = normalise_annotations(annotations)
    if not annotations:
        raise AnnotationError("no annotations to apply")
    if not os.path.isfile(video_path):
        raise AnnotationError(f"clip not found: {video_path}")

    width, height, fps, duration = probe_video(video_path)
    workdir = workdir or os.path.dirname(os.path.abspath(output_path))

    temp_files = []
    entries = []
    cmd_lines = []
    try:
        for i, annotation in enumerate(annotations):
            size_px = max(8, int(round(annotation["size"] * width)))
            png_path = os.path.join(
                workdir, f"anno_{uuid.uuid4().hex[:8]}.png")
            _, shape_w, shape_h, anchor_x, anchor_y = render_shape(
                annotation["type"], png_path, size_px=size_px,
                color=annotation["color"], thickness=annotation["thickness"],
                rotation=annotation["rotation"])
            temp_files.append(png_path)

            positions, moving = resolve_positions(
                annotation, track, width=width, height=height, fps=fps,
                anchor=(anchor_x, anchor_y), duration=duration)

            label = f"a{i}"
            first_x, first_y = positions[0][1], positions[0][2]
            if moving:
                cmd_lines.extend(
                    sendcmd_xy(positions, fps, f"overlay@{label}"))
            entries.append({
                "input": i + 1,
                "label": label,
                "start": annotation["start"],
                "end": annotation["end"],
                "x": int(round(first_x)),
                "y": int(round(first_y)),
                "png": png_path,
                "moving": moving,
                "shape": (shape_w, shape_h),
            })

        cmd_path = None
        if cmd_lines:
            cmd_path = os.path.join(workdir, f"anno_{uuid.uuid4().hex[:8]}.cmd")
            with open(cmd_path, "w") as handle:
                handle.write("\n".join(cmd_lines) + "\n")
            temp_files.append(cmd_path)

        graph = build_filtergraph(entries, cmd_path=cmd_path)

        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path]
        for entry in entries:
            cmd += ["-i", entry["png"]]
        cmd += [
            "-filter_complex", graph,
            "-map", "[out]",
            "-map", "0:a?",
            "-c:a", "copy",
            *video_encode_args(QUALITY),
            *METADATA_SCRUB,
            "-movflags", "+faststart",
            output_path,
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not os.path.exists(output_path):
            raise AnnotationError(
                f"ffmpeg failed: {(proc.stderr or '').strip()[-400:]}")

        return {
            "output": output_path,
            "count": len(entries),
            "moving": sum(1 for e in entries if e["moving"]),
            "width": width,
            "height": height,
            "fps": round(fps, 4),
        }
    finally:
        for path in temp_files:
            try:
                os.remove(path)
            except OSError:
                pass


def remap(annotation_list, segments):
    """Carry annotations across a cut-and-concat, like layout_ranges.remap.

    ``segments`` must be in the SAME timeline as the annotation times: for the
    fast recut path that is recut.rebase_segments(...) output, which is expressed
    in the canonical clip's own time. Passing source-absolute segments against
    clip-relative annotations would silently move every annotation.

    An annotation is kept when its window overlaps a kept stretch, clipped to
    that stretch, and shifted to where the stretch landed in the output. One
    annotation spanning a cut therefore comes back as two, which is right: the
    circle really was on screen in both halves.
    """
    out = []
    offset = 0.0
    for seg in segments or []:
        try:
            seg_start, seg_end = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if seg_end <= seg_start:
            continue
        for annotation in annotation_list or []:
            try:
                start = float(annotation["start"])
                end = float(annotation["end"])
            except (KeyError, TypeError, ValueError):
                continue
            lo, hi = max(start, seg_start), min(end, seg_end)
            if hi <= lo:
                continue
            moved = dict(annotation)
            moved["start"] = round(lo - seg_start + offset, 3)
            moved["end"] = round(hi - seg_start + offset, 3)
            out.append(moved)
        offset += seg_end - seg_start

    out.sort(key=lambda a: (a["start"], a["end"]))
    return out
