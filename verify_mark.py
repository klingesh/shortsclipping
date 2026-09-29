"""Prove the username watermark lands where it says, in the font it says.

String-level checks are not enough and this repo has been bitten by that twice: a
filtergraph can be perfectly well-formed and still fail to encode, and a font can
resolve to the wrong face while the text renders fine. So this runs real ffmpeg
encodes and reads the PIXELS back to find out where the mark actually is.

It runs at 360x640, not 1080x1920. Every quantity in username_mark is a fraction
of the frame, so a small frame exercises identical arithmetic; the first version
of this script used 1080x1920 and exhausted the machine's memory, which tested
the host rather than the code. Pixel scanning is vectorised with numpy for the
same reason.

Note on the awkward-handle check: the text never enters the filtergraph — that is
the entire reason this module rasterises instead of calling drawtext — so putting
ten awkward handles through ten encodes proves nothing that one encode does not.
The ten are checked at the raster stage, where they can actually fail, and one
goes through the full chain to confirm the graph is text-independent.

Run inside the backend container:
    python /app/verify_mark.py
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")

import caption_presets  # noqa: E402
import username_mark  # noqa: E402

# Portrait, same 9:16 shape as a real short, small enough to be free.
WIDTH, HEIGHT = 360, 640
DURATION = 3

# Background grey. Chosen so a dark outline and a bright fill are both visible
# against it; black or white would hide one of them.
BG = 128
# How far from the background a pixel must be to count as ink. Above the noise
# h.264 adds to a flat field, below the contrast of any colour used here.
INK_THRESHOLD = 24


def _np():
    import numpy
    return numpy


def make_source(path, seconds=DURATION):
    """A mid-grey clip with sound."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i",
         f"color=c=0x{BG:02x}{BG:02x}{BG:02x}:s={WIDTH}x{HEIGHT}:d={seconds}:r=24",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", path],
        check=True, capture_output=True, timeout=300)
    return path


def frame_ink(video_path, at_time, workdir):
    """Where the non-background pixels are in one frame, or None if there are none.

    The mark is the only thing that is not flat grey, so its bounding box is
    found by thresholding the distance from the background rather than by looking
    for a particular colour. That way the same measurement works whatever fill a
    check picks.
    """
    from PIL import Image
    np = _np()

    png = os.path.join(workdir, f"probe_{at_time:.2f}.png")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(at_time), "-i", video_path,
         "-frames:v", "1", png],
        check=True, capture_output=True, timeout=120)
    try:
        array = np.asarray(Image.open(png).convert("RGB"), dtype=np.int16)
    finally:
        os.remove(png)

    deviation = np.abs(array - BG).max(axis=2)
    mask = deviation > INK_THRESHOLD
    if not mask.any():
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    height, width = mask.shape
    return {
        "count": int(mask.sum()),
        "box": (int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])),
        "x_mid": float((cols[0] + cols[-1]) / 2.0 / width),
        "y_mid": float((rows[0] + rows[-1]) / 2.0 / height),
        "peak": int(deviation.max()),
        # Mean over the ink only: a measure of contrast that does not move when
        # the mark's area changes.
        "ink_mean": float(deviation[mask].mean()),
    }


def check_fonts():
    """Every offered family resolves, and the display faces stay distinct."""
    ok = True
    resolved = {}
    for choice in caption_presets.font_catalog():
        family = choice["family"]
        path, how = username_mark.font_path_for(family)
        good = bool(path) and os.path.exists(path)
        if not good:
            ok = False
        resolved[family] = path
        print(f"  {family:<16} {'OK  ' if good else 'FAIL'} "
              f"{how:<12} {os.path.basename(path or '-')}")

    # The four display faces are the reason the font list exists. If they all
    # resolved to one file the choice would be cosmetic — which is issue #57.
    display = ["Anton", "Bebas Neue", "Archivo Black", "Luckiest Guy"]
    files = {resolved.get(f) for f in display}
    distinct = len(files) == len(display)
    if not distinct:
        ok = False
    print(f"  display faces distinct: {len(files)}/{len(display)} "
          f"{'OK' if distinct else 'COLLAPSED'}")
    return ok


def check_fonts_change_the_raster():
    """Choosing a different font must change the pixels.

    Resolving to distinct FILES is necessary but not sufficient: if _load_font
    silently fell back, every render would be identical while the report still
    named the requested family.
    """
    np = _np()
    workdir = tempfile.mkdtemp(prefix="markfont_")
    try:
        from PIL import Image
        shapes = {}
        rasters = {}
        for family in ("Anton", "Bebas Neue", "Archivo Black", "Luckiest Guy"):
            path = os.path.join(workdir, f"{family}.png")
            mark = username_mark.normalise_mark(
                {"text": "HANDLE", "font": family, "size": 0.12,
                 "outline_width": 0.0})
            _, w, h, font_path, how = username_mark.render_mark(
                mark, path, frame_width=WIDTH)
            shapes[family] = (w, h)
            rasters[family] = np.asarray(
                Image.open(path).convert("L").resize((64, 32)), dtype=np.int16)
            print(f"  {family:<16} {w}x{h} via {how} "
                  f"{os.path.basename(font_path)}")

        ok = True
        families = list(rasters)
        for i, a in enumerate(families):
            for b in families[i + 1:]:
                delta = float(np.abs(rasters[a] - rasters[b]).mean())
                good = delta > 2.0
                if not good:
                    ok = False
                print(f"  {a} vs {b}: mean pixel delta {delta:.2f} "
                      f"{'OK' if good else 'IDENTICAL'}")
        return ok
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


def check_anchors(source, workdir):
    """All nine anchors put the mark in the right third of the frame."""
    thirds = {
        "top_left": (0, 0), "top_center": (1, 0), "top_right": (2, 0),
        "middle_left": (0, 1), "center": (1, 1), "middle_right": (2, 1),
        "bottom_left": (0, 2), "bottom_center": (1, 2), "bottom_right": (2, 2),
    }
    ok = True
    for anchor, (want_col, want_row) in thirds.items():
        out = os.path.join(workdir, "anchor.mp4")
        username_mark.apply_mark(
            source,
            {"text": "@handle", "anchor": anchor, "margin": 0.04,
             "size": 0.06, "color": "#00E5FF"},
            out, workdir=workdir)
        ink = frame_ink(out, 1.0, workdir)
        os.remove(out)
        if ink is None:
            print(f"  {anchor:<14} FAIL nothing drawn")
            ok = False
            continue
        got_col = min(2, int(ink["x_mid"] * 3))
        got_row = min(2, int(ink["y_mid"] * 3))
        good = (got_col, got_row) == (want_col, want_row)
        if not good:
            ok = False
        print(f"  {anchor:<14} {'OK  ' if good else 'FAIL'} "
              f"centre ({ink['x_mid']:.3f},{ink['y_mid']:.3f}) "
              f"third ({got_col},{got_row}) want ({want_col},{want_row})")
    return ok


def check_free_xy(source, workdir):
    """A dragged point centres the mark on that point."""
    ok = True
    for want_x, want_y in ((0.25, 0.75), (0.5, 0.5), (0.8, 0.2)):
        out = os.path.join(workdir, "xy.mp4")
        username_mark.apply_mark(
            source,
            {"text": "@handle", "x": want_x, "y": want_y, "size": 0.06},
            out, workdir=workdir)
        ink = frame_ink(out, 1.0, workdir)
        os.remove(out)
        if ink is None:
            print(f"  ({want_x},{want_y}) FAIL nothing drawn")
            ok = False
            continue
        # The measured centre is the INK's bounding box, while the placement
        # centres the PNG's canvas. Glyphs do not fill their canvas symmetrically
        # — side bearings and descenders — so a few percent of slack is correct
        # rather than sloppy.
        dx = abs(ink["x_mid"] - want_x)
        dy = abs(ink["y_mid"] - want_y)
        good = dx < 0.04 and dy < 0.04
        if not good:
            ok = False
        print(f"  ({want_x:.2f},{want_y:.2f}) {'OK  ' if good else 'FAIL'} "
              f"measured ({ink['x_mid']:.3f},{ink['y_mid']:.3f}) "
              f"delta ({dx:.3f},{dy:.3f})")
    return ok


def frame_peak(video_path, at_time, workdir):
    """Largest deviation from the background in one frame, unthresholded.

    frame_ink cannot be used for the opacity check: a faint mark deviates by
    less than the ink threshold, so masking reports "nothing drawn" for a mark
    that is plainly there. White at 15% over mid-grey moves a pixel by 19
    levels, and the threshold is 24. Measuring the raw peak works at any
    opacity.
    """
    from PIL import Image
    np = _np()

    png = os.path.join(workdir, f"peak_{at_time:.2f}.png")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(at_time), "-i", video_path,
         "-frames:v", "1", png],
        check=True, capture_output=True, timeout=120)
    try:
        array = np.asarray(Image.open(png).convert("RGB"), dtype=np.int16)
    finally:
        os.remove(png)
    return int(np.abs(array - BG).max())


def check_opacity(source, workdir):
    """Less opacity, less contrast, and a faint mark is still there.

    "Still there" is judged against the encoder's own noise floor, measured from
    the unmarked source rather than assumed. A flat grey field does not survive
    h.264 perfectly, and a hardcoded constant here would either pass on noise or
    fail on a legitimate faint mark.
    """
    floor = frame_peak(source, 1.0, workdir)
    print(f"  encoder noise floor on the unmarked source: {floor}")

    ok = True
    contrast = {}
    for opacity in (1.0, 0.5, 0.15):
        out = os.path.join(workdir, "op.mp4")
        username_mark.apply_mark(
            source,
            {"text": "@handle", "anchor": "center", "size": 0.10,
             "color": "#FFFFFF", "outline_width": 0.0, "opacity": opacity},
            out, workdir=workdir)
        peak = frame_peak(out, 1.0, workdir)
        os.remove(out)
        contrast[opacity] = peak
        above = peak > floor + 4
        if not above:
            ok = False
        print(f"  opacity {opacity:<5} peak {peak:>3} "
              f"{'above' if above else 'AT OR BELOW'} the floor")

    if not (contrast[1.0] > contrast[0.5] > contrast[0.15]):
        ok = False
        print(f"  FAIL not monotonic: {contrast}")
    else:
        print(f"  monotonic in opacity: {contrast[1.0]} > {contrast[0.5]} > "
              f"{contrast[0.15]} OK")
    return ok


def check_window(source, workdir):
    """start/end gate the overlay: drawn inside the window, absent outside."""
    out = os.path.join(workdir, "window.mp4")
    username_mark.apply_mark(
        source,
        {"text": "@handle", "anchor": "center", "size": 0.10,
         "start": 1.0, "end": 2.0},
        out, workdir=workdir)
    ok = True
    for when, should_show in ((0.3, False), (1.5, True), (2.6, False)):
        ink = frame_ink(out, when, workdir)
        shown = ink is not None and ink["count"] > 20
        good = shown == should_show
        if not good:
            ok = False
        print(f"  t={when:>4}s {'shown' if shown else 'absent':<6} "
              f"want {'shown' if should_show else 'absent':<6} "
              f"{'OK' if good else 'FAIL'}")
    os.remove(out)
    return ok


def check_awkward_text(source, workdir):
    """Handles containing filtergraph metacharacters must raster and encode.

    Each of these has meaning inside a filter_complex. They are the reason this
    module rasterises rather than calling drawtext. Because the text never
    reaches the graph, the raster stage is where they could fail, so all ten are
    checked there and one is taken through the full encode to confirm the graph
    really is text-independent.
    """
    samples = [
        "@user:name",
        "user's_handle",
        "a,b;c",
        "[brackets]",
        "back\\slash",
        "100% real",
        "it's=this",
        "  padded  ",
        "emoji 🔥 clip",
        "ALLCAPSHANDLENAME",
    ]
    ok = True
    for text in samples:
        png = os.path.join(workdir, "awkward.png")
        try:
            mark = username_mark.normalise_mark({"text": text, "size": 0.06})
            _, w, h, _, _ = username_mark.render_mark(mark, png,
                                                      frame_width=WIDTH)
            good = w > 1 and h > 1
            print(f"  {text!r:<22} {'OK  ' if good else 'FAIL'} "
                  f"{w}x{h} stored {mark['text']!r}")
        except Exception as exc:
            good = False
            print(f"  {text!r:<22} FAIL {type(exc).__name__}: {exc}")
        if not good:
            ok = False
        if os.path.exists(png):
            os.remove(png)

    # One full pass, to show the graph does not care what the text was.
    out = os.path.join(workdir, "awkward.mp4")
    try:
        username_mark.apply_mark(
            source, {"text": "it's:a,b;[c]\\100%", "anchor": "bottom_left",
                     "size": 0.05},
            out, workdir=workdir)
        ink = frame_ink(out, 1.0, workdir)
        drawn = ink is not None and ink["count"] > 10
        print(f"  full encode with every metacharacter: "
              f"{'OK' if drawn else 'FAIL'}")
        if not drawn:
            ok = False
    except Exception as exc:
        ok = False
        print(f"  full encode FAIL {type(exc).__name__}: {exc}")
    finally:
        if os.path.exists(out):
            os.remove(out)
    return ok


def check_rejects():
    """Input that cannot mean anything must be refused, not guessed at."""
    cases = [
        ("empty text", {"text": ""}),
        ("whitespace only", {"text": "   "}),
        ("control chars only", {"text": "\x00\x01"}),
        ("bad anchor", {"text": "x", "anchor": "nowhere"}),
        ("end before start", {"text": "x", "start": 5.0, "end": 2.0}),
        ("not a dict", "just a string"),
        # Colours must be caught at the boundary, not at render time. Leaving
        # them to the renderer turned a mistyped colour into a 500 from inside
        # the raster instead of a 400 naming the field.
        ("bad colour", {"text": "x", "color": "reddish"}),
        ("short colour", {"text": "x", "color": "#FFF"}),
        ("bad outline colour", {"text": "x", "outline_color": "blackish"}),
    ]
    ok = True
    for label, spec in cases:
        try:
            username_mark.normalise_mark(spec)
        except username_mark.MarkError as exc:
            print(f"  {label:<20} rejected OK ({exc})")
        except Exception as exc:
            ok = False
            print(f"  {label:<20} FAIL wrong error {type(exc).__name__}: {exc}")
        else:
            ok = False
            print(f"  {label:<20} FAIL accepted")

    # A colour that IS valid must survive untouched, or the check above would
    # pass just as well if normalise_mark rejected everything.
    good = username_mark.normalise_mark(
        {"text": "x", "color": "#00e5ff", "outline_color": "#112233AA"})
    kept = good["color"] == "#00e5ff" and good["outline_color"] == "#112233AA"
    if not kept:
        ok = False
    print(f"  valid colours kept verbatim: {good['color']}, "
          f"{good['outline_color']} {'OK' if kept else 'FAIL'}")
    return ok


def check_clamped_to_frame(source, workdir):
    """A long handle at maximum size must FIT, not just start on screen.

    The first run of this script accepted a 3519px-wide mark inside a 1080px
    frame because it only asked whether the top-left corner was on screen. It
    was; the handle was still three-quarters cut off. So the real requirement is
    that the whole mark is inside the frame, and that its right-hand edge is
    visible in the pixels.
    """
    out = os.path.join(workdir, "huge.mp4")
    report = username_mark.apply_mark(
        source,
        {"text": "AVERYLONGCHANNELHANDLE", "anchor": "bottom_right",
         "size": username_mark.MAX_SIZE},
        out, workdir=workdir)
    x, y = report["position"]
    mark_w, mark_h = report["mark_size"]
    fits = (x >= 0 and y >= 0
            and x + mark_w <= WIDTH and y + mark_h <= HEIGHT)
    print(f"  mark {mark_w}x{mark_h} at ({x},{y}) in {WIDTH}x{HEIGHT} "
          f"{'OK' if fits else 'FAIL does not fit'}")

    ink = frame_ink(out, 1.0, workdir)
    os.remove(out)
    if ink is None:
        print("  FAIL nothing drawn")
        return False
    # The ink must stop before the frame edge. If the text were being cut off,
    # it would run right up to the last column.
    left, top, right, bottom = ink["box"]
    inside = right < WIDTH - 1 and bottom < HEIGHT - 1 and left > 0
    print(f"  ink box ({left},{top})-({right},{bottom}) clear of the edges: "
          f"{'OK' if inside else 'FAIL clipped'}")
    return fits and inside


def check_audio_preserved(source, workdir):
    """The mark pass must not drop the sound. -c:a copy, verified."""
    out = os.path.join(workdir, "audio.mp4")
    username_mark.apply_mark(source, {"text": "@handle"}, out, workdir=workdir)
    streams = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", out],
        timeout=60).decode().strip()
    ok = bool(streams)
    print(f"  audio stream after the mark pass: {streams or 'NONE'} "
          f"{'OK' if ok else 'FAIL'}")
    os.remove(out)
    return ok


def check_no_leftover_pngs(source, workdir):
    """The temporary PNG must be cleaned up even though it is written next to
    the output. A stray mark_*.png in the job's output dir would be served."""
    out = os.path.join(workdir, "clean.mp4")
    username_mark.apply_mark(source, {"text": "@handle"}, out, workdir=workdir)
    leftovers = [f for f in os.listdir(workdir) if f.startswith("mark_")]
    os.remove(out)
    ok = not leftovers
    print(f"  leftover mark_*.png: {leftovers or 'none'} "
          f"{'OK' if ok else 'FAIL'}")
    return ok


def main():
    workdir = tempfile.mkdtemp(prefix="markcheck_")
    source = os.path.join(workdir, "source.mp4")
    try:
        print(f"== building a {WIDTH}x{HEIGHT} source clip ==")
        make_source(source)
        w, h, d = username_mark.probe_video(source)
        print(f"  {w}x{h} {d:.2f}s")

        checks = [
            ("fonts resolve and stay distinct", check_fonts),
            ("font choice changes the raster", check_fonts_change_the_raster),
            ("rejects meaningless input", check_rejects),
            ("nine anchors land correctly",
             lambda: check_anchors(source, workdir)),
            ("free x/y centres the mark",
             lambda: check_free_xy(source, workdir)),
            ("opacity carries through", lambda: check_opacity(source, workdir)),
            ("time window gates the overlay",
             lambda: check_window(source, workdir)),
            ("awkward handles survive",
             lambda: check_awkward_text(source, workdir)),
            ("oversized handle stays on screen",
             lambda: check_clamped_to_frame(source, workdir)),
            ("audio survives", lambda: check_audio_preserved(source, workdir)),
            ("no leftover PNGs",
             lambda: check_no_leftover_pngs(source, workdir)),
        ]
        results = []
        for title, fn in checks:
            print(f"\n== {title} ==")
            try:
                results.append((title, fn()))
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"  raised {type(exc).__name__}: {exc}")
                results.append((title, False))

        print("\n== summary ==")
        for title, ok in results:
            print(f"  {'PASS' if ok else 'FAIL'}  {title}")
        failed = [t for t, ok in results if not ok]
        if failed:
            print(f"\n✗ {len(failed)} check(s) failed")
            return 1
        print("\n✓ the watermark lands where it says it does")
        return 0
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
