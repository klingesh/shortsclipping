"""Check the editor's preview geometry against the burn it is previewing.

The browser redraws the shapes in CSS/SVG rather than fetching the PNGs ffmpeg
will composite, so there are two independent implementations of the same
geometry. If they disagree, the user places a shape where it does not land —
and the failure is silent, which is the worst kind.

This pins the two together at the only places they can drift:

  1. rotatePoint (JS) vs annotations._rotate_point (Python). This exact function
     was once written with inverted sine signs, which put a rotated arrow's tip
     on the opposite diagonal from its target. Only 45/135/225/315 revealed it.
  2. The anchor's final position, which is what actually has to sit on the
     target. Python gets it from render_shape on a real PNG; the browser
     computes it. Compared as a fraction of the shape's box so the two are
     dimensionless and comparable.
  3. The CSS rotation sign. PIL turns the picture one way and CSS the other, so
     the preview negates the angle. Verified by where a known point lands
     rather than by reading the code.

Run inside the backend container:
    python /app/verify_overlay_geometry.py
"""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")

import annotations  # noqa: E402

EDITOR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "dashboard", "src", "components", "AnnotationEditor.jsx")

ANGLES = [0, 1, 30, 45, 60, 90, 120, 135, 180, 225, 270, 315, 359,
          -45, -90, -135, -180, 17.5, 260.25]

# Points spread over the shape box, in box fractions. The centre is a fixed
# point of the rotation and so proves nothing on its own.
POINTS = [(0.5, 0.98), (0.5, 0.5), (0.0, 0.0), (1.0, 0.0),
          (1.0, 1.0), (0.0, 1.0), (0.5, 0.04), (0.23, 0.77)]

TOLERANCE = 1e-9


def js_rotate_via_node(points, centre, angles):
    """Run the editor's own rotatePoint in node, not a copy of it.

    The function is pulled out of the .jsx by name so this cannot pass while the
    shipped file says something different.
    """
    with open(EDITOR, "r", encoding="utf-8") as handle:
        source = handle.read()

    marker = "const rotatePoint ="
    start = source.index(marker)
    end = source.index("\n};", start) + len("\n};")
    body = source[start:end]

    script = body + "\n" + f"""
const points = {json.dumps(points)};
const centre = {json.dumps(centre)};
const angles = {json.dumps(angles)};
const out = [];
for (const a of angles) {{
  for (const p of points) {{
    out.push(rotatePoint(p, centre, a));
  }}
}}
process.stdout.write(JSON.stringify(out));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(script)
        path = handle.name
    try:
        result = subprocess.run([shutil.which("node") or "node", path],
                                capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"node failed: {result.stderr.strip()}")
        return json.loads(result.stdout)
    finally:
        os.unlink(path)


def check_rotation_agrees():
    """1. The two rotatePoint implementations must agree exactly."""
    centre = [0.5, 0.5]
    got = js_rotate_via_node(POINTS, centre, ANGLES)

    expected = []
    for angle in ANGLES:
        for point in POINTS:
            expected.append(annotations._rotate_point(point, tuple(centre), angle))

    worst = 0.0
    for (jx, jy), (px, py), in zip(got, expected):
        worst = max(worst, abs(jx - px), abs(jy - py))

    ok = worst <= TOLERANCE
    print(f"  rotatePoint vs _rotate_point over {len(expected)} cases: "
          f"worst delta {worst:.3e} {'OK' if ok else 'MISMATCH'}")
    return ok


def check_down_vector():
    """The property that pins the signs: down (0,+d) must go to (d sinθ, d cosθ)."""
    ok = True
    for angle in (0, 90, 180, 270, 45):
        rad = math.radians(angle)
        got = annotations._rotate_point((0.5, 1.0), (0.5, 0.0), angle)
        want = (0.5 + math.sin(rad), math.cos(rad))
        delta = max(abs(got[0] - want[0]), abs(got[1] - want[1]))
        if delta > 1e-12:
            ok = False
        print(f"  down vector at {angle:>4}°: got ({got[0]:+.4f},{got[1]:+.4f}) "
              f"want ({want[0]:+.4f},{want[1]:+.4f}) "
              f"{'OK' if delta <= 1e-12 else 'MISMATCH'}")
    return ok


def check_anchor_position():
    """2. Where the anchor ends up, real PNG vs the editor's arithmetic.

    render_shape returns the anchor in pixels of the image it wrote, including
    the canvas growth that rotate(expand=True) causes. The editor has no canvas
    growth — CSS rotates in place — so the comparison is made in the frame's own
    terms: the anchor's offset from the shape box's centre, as a fraction of the
    box. That is the quantity both sides use to place the shape.
    """
    size_px = 400
    ok = True
    workdir = tempfile.mkdtemp(prefix="anchorcheck_")
    try:
        for shape in annotations.SHAPES:
            box_anchor = (0.5, 0.98) if shape == "arrow" else (0.5, 0.5)
            for angle in (0, 30, 45, 90, 135, 180, 270, -45):
                path = os.path.join(workdir, f"{shape}_{angle}.png")
                _, width, height, anchor_x, anchor_y = annotations.render_shape(
                    shape, path, size_px=size_px, rotation=angle)

                # Python: anchor offset from the IMAGE centre, over the
                # unrotated box size. The image centre is where the original box
                # centre stayed, since rotate(expand=True) grows symmetrically.
                py_dx = (anchor_x - width / 2.0) / size_px
                py_dy = (anchor_y - height / 2.0) / size_px

                # Editor: rotate the box anchor about the box centre.
                jx, jy = annotations._rotate_point(box_anchor, (0.5, 0.5), angle)
                js_dx = jx - 0.5
                js_dy = jy - 0.5

                delta = max(abs(py_dx - js_dx), abs(py_dy - js_dy))
                # One pixel of the 400px box: render_shape rounds to whole
                # pixels twice (supersample, then resize).
                good = delta <= 1.0 / size_px * 2
                if not good:
                    ok = False
                print(f"  {shape:<6} {angle:>5}° anchor offset "
                      f"png({py_dx:+.4f},{py_dy:+.4f}) "
                      f"editor({js_dx:+.4f},{js_dy:+.4f}) "
                      f"delta {delta:.5f} {'OK' if good else 'MISMATCH'}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return ok


def check_css_sign():
    """3. The preview must negate the angle, and the file must actually do it.

    CSS rotate(θ) in y-down screen coordinates sends (0,+d) to (-d sinθ, d cosθ);
    PIL sends it to (+d sinθ, d cosθ). Opposite, so the sign has to flip.
    """
    ok = True
    for angle in (30, 90, 135):
        rad = math.radians(angle)
        pil = annotations._rotate_point((0.0, 1.0), (0.0, 0.0), angle)
        # CSS with the angle negated, applied as the standard screen-space matrix.
        css_rad = math.radians(-angle)
        css = (0.0 * math.cos(css_rad) - 1.0 * math.sin(css_rad),
               0.0 * math.sin(css_rad) + 1.0 * math.cos(css_rad))
        delta = max(abs(pil[0] - css[0]), abs(pil[1] - css[1]))
        if delta > 1e-12:
            ok = False
        print(f"  {angle:>4}°: pil({pil[0]:+.4f},{pil[1]:+.4f}) "
              f"css(-{angle}°)({css[0]:+.4f},{css[1]:+.4f}) "
              f"{'OK' if delta <= 1e-12 else 'MISMATCH'}")

    with open(EDITOR, "r", encoding="utf-8") as handle:
        source = handle.read()
    negates = "const cssRotation = -annotation.rotation;" in source
    print(f"  editor negates the angle: {'OK' if negates else 'MISSING'}")
    return ok and negates


def check_shape_constants():
    """The arrow proportions are duplicated in the .jsx; they must still match."""
    import re
    with open(EDITOR, "r", encoding="utf-8") as handle:
        source = handle.read()

    def const(name):
        match = re.search(rf"^const {name} = ([\d.]+);", source, re.M)
        return float(match.group(1)) if match else None

    # From _draw_arrow: head 0.72 wide, 0.52 tall, tip at 0.98, shaft >= 0.26,
    # tail starts at 0.04.
    expected = {"ARROW_HEAD_W": 0.72, "ARROW_HEAD_H": 0.52, "ARROW_TIP_Y": 0.98,
                "ARROW_SHAFT_MIN": 0.26, "ARROW_TAIL_Y": 0.04,
                "SIZE_MIN": annotations.MIN_SIZE, "SIZE_MAX": annotations.MAX_SIZE,
                "THICK_MIN": annotations.MIN_THICKNESS,
                "THICK_MAX": annotations.MAX_THICKNESS}
    ok = True
    for name, want in expected.items():
        got = const(name)
        good = got is not None and abs(got - want) < 1e-9
        if not good:
            ok = False
        print(f"  {name:<16} editor={got} backend={want} "
              f"{'OK' if good else 'MISMATCH'}")

    for name, want in (("DEFAULT_COLOR", annotations.DEFAULT_COLOR),
                       ("DEFAULT_SIZE", annotations.DEFAULT_SIZE),
                       ("DEFAULT_THICKNESS", annotations.DEFAULT_THICKNESS)):
        if isinstance(want, str):
            good = f"const {name} = '{want}';" in source
            shown = want
        else:
            got = const(name)
            good = got is not None and abs(got - want) < 1e-9
            shown = got
        if not good:
            ok = False
        print(f"  {name:<16} editor={shown} backend={want} "
              f"{'OK' if good else 'MISMATCH'}")
    return ok


def check_gap_constant():
    """The editor interpolates the track itself, so its gap rule must match."""
    import re
    with open(EDITOR, "r", encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(r"^const MAX_GAP_SECONDS = ([\d.]+);", source, re.M)
    got = float(match.group(1)) if match else None
    import subject_track
    want = subject_track.MAX_GAP_SECONDS
    good = got is not None and abs(got - want) < 1e-9
    print(f"  MAX_GAP_SECONDS  editor={got} backend={want} "
          f"{'OK' if good else 'MISMATCH'}")
    return good


def main():
    if not os.path.exists(EDITOR):
        print(f"✗ editor not found at {EDITOR}")
        return 1

    checks = [
        ("rotation agreement", check_rotation_agrees),
        ("down-vector property", check_down_vector),
        ("anchor placement vs real PNG", check_anchor_position),
        ("css rotation sign", check_css_sign),
        ("shape constants", check_shape_constants),
        ("track gap constant", check_gap_constant),
    ]
    results = []
    for title, fn in checks:
        print(f"\n== {title} ==")
        try:
            results.append((title, fn()))
        except Exception as exc:
            print(f"  raised {type(exc).__name__}: {exc}")
            results.append((title, False))

    print("\n== summary ==")
    for title, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {title}")
    failed = [t for t, ok in results if not ok]
    if failed:
        print(f"\n✗ {len(failed)} check(s) failed")
        return 1
    print("\n✓ the preview agrees with the burn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
