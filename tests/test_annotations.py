"""Red circle and arrow overlays.

Three layers of checking, because the failure modes are different:

- Shape rendering is checked by reading pixels out of the generated PNG. The
  thing that matters most is transparency: the user's own asset folder ships
  JPEGs for these overlays, which cannot carry an alpha channel, so they paint a
  white box over the video. A test that only checked "a file was written" would
  have passed for those too.
- Anchor placement is checked with real geometry at several rotations. An arrow
  whose tip drifts from its target by half a shape width is the whole feature
  broken, and it is easy to get the rotation sign wrong.
- Compositing is checked with real ffmpeg encodes, because the last ffmpeg graph
  in this project passed every string-level test while failing every encode.
"""

import os
import subprocess

import pytest

import annotations as an


def _has(binary):
    try:
        subprocess.run([binary, "-version"], capture_output=True, timeout=30)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


needs_ffmpeg = pytest.mark.skipif(not _has("ffmpeg"), reason="ffmpeg missing")


def _open(path):
    from PIL import Image
    return Image.open(path).convert("RGBA")


class TestColourParsing:
    @pytest.mark.parametrize("value,expected", [
        ("#FF0000", (255, 0, 0, 255)),
        ("FF0000", (255, 0, 0, 255)),
        ("#00FF0080", (0, 255, 0, 128)),
        ("#7CFC00", (124, 252, 0, 255)),
    ])
    def test_accepted_forms(self, value, expected):
        assert an._parse_color(value) == expected

    @pytest.mark.parametrize("value", ["", None, "red", "#FFF", "#GGGGGG", "#12345"])
    def test_rejected_forms(self, value):
        with pytest.raises(an.AnnotationError):
            an._parse_color(value)


class TestShapeRendering:
    def test_circle_png_has_transparency(self, tmp_path):
        # The defect the user's assets have: five of their eight overlay files
        # are JPEGs, so the "transparent" middle is opaque white.
        path = str(tmp_path / "c.png")
        an.render_shape("circle", path, size_px=120)
        image = _open(path)
        assert image.mode == "RGBA"
        # Dead centre of a ring must be fully transparent.
        assert image.getpixel((image.width // 2, image.height // 2))[3] == 0

    def test_circle_is_a_ring_not_a_disc(self, tmp_path):
        path = str(tmp_path / "c.png")
        an.render_shape("circle", path, size_px=160, thickness=0.1)
        image = _open(path)
        mid_y = image.height // 2
        # Scanning across the middle: transparent, stroke, transparent, stroke,
        # transparent. A filled disc would be one continuous opaque run.
        runs = []
        opaque = None
        for x in range(image.width):
            is_opaque = image.getpixel((x, mid_y))[3] > 128
            if is_opaque != opaque:
                runs.append(is_opaque)
                opaque = is_opaque
        assert runs.count(True) == 2, f"expected two stroke crossings, got {runs}"

    def test_arrow_is_solid_at_its_tip(self, tmp_path):
        path = str(tmp_path / "a.png")
        _, w, h, ax, ay = an.render_shape("arrow", path, size_px=120)
        image = _open(path)
        # Just inside the tip should be opaque arrow colour.
        px = image.getpixel((int(ax), int(min(ay, h - 2)) - 3))
        assert px[3] > 128

    def test_colour_is_honoured(self, tmp_path):
        path = str(tmp_path / "a.png")
        _, w, h, ax, ay = an.render_shape(
            "arrow", path, size_px=120, color="#00FF00")
        image = _open(path)
        # Sample inside the head.
        px = image.getpixel((int(ax), int(ay) - 6))
        assert px[1] > 200 and px[0] < 80, px

    def test_thickness_changes_the_stroke(self, tmp_path):
        def stroke_pixels(thickness):
            path = str(tmp_path / f"c{thickness}.png")
            an.render_shape("circle", path, size_px=200, thickness=thickness)
            image = _open(path)
            return sum(1 for x in range(image.width)
                       for y in range(image.height)
                       if image.getpixel((x, y))[3] > 128)

        assert stroke_pixels(0.2) > stroke_pixels(0.05) * 1.5

    def test_size_drives_the_canvas(self, tmp_path):
        for size in (64, 128, 256):
            path = str(tmp_path / f"c{size}.png")
            _, w, h, _, _ = an.render_shape("circle", path, size_px=size)
            assert abs(w - size) <= 2
            assert abs(h - size) <= 2

    def test_unknown_shape_raises(self, tmp_path):
        with pytest.raises(an.AnnotationError):
            an.render_shape("squiggle", str(tmp_path / "x.png"), size_px=64)


class TestAnchors:
    """Where the shape attaches to its target."""

    def test_circle_anchors_at_its_centre(self, tmp_path):
        path = str(tmp_path / "c.png")
        _, w, h, ax, ay = an.render_shape("circle", path, size_px=120)
        assert ax == pytest.approx(w / 2, abs=2)
        assert ay == pytest.approx(h / 2, abs=2)

    def test_unrotated_arrow_anchors_at_the_bottom_tip(self, tmp_path):
        path = str(tmp_path / "a.png")
        _, w, h, ax, ay = an.render_shape("arrow", path, size_px=120)
        assert ax == pytest.approx(w / 2, abs=2)
        # Pointing down, so the tip is near the bottom edge.
        assert ay > h * 0.9

    @pytest.mark.parametrize("rotation", [0, 45, 90, 135, 180, 225, 270, 315])
    def test_the_anchor_points_the_way_the_arrow_points(self, rotation,
                                                        tmp_path):
        """The anchor must be the TIP, not merely somewhere on the arrow.

        Checking only "the anchor sits on opaque pixels" is too weak: the shape
        spans the canvas, so at 90 degrees an anchor stuck on the TAIL passes
        that check while pointing backwards. This instead asserts the anchor lies
        in the direction the arrow points.

        The base drawing points down, i.e. (0, +1) in image coordinates. PIL
        rotates counter-clockwise as displayed, so after theta the tip direction
        is (sin theta, cos theta): 90 sends down to right, 180 to up.
        """
        import math

        path = str(tmp_path / f"a{rotation}.png")
        _, w, h, ax, ay = an.render_shape(
            "arrow", path, size_px=160, rotation=rotation)
        assert 0 <= ax <= w and 0 <= ay <= h, f"anchor {ax},{ay} outside {w}x{h}"

        radians = math.radians(rotation)
        expected = (math.sin(radians), math.cos(radians))
        actual = (ax - w / 2.0, ay - h / 2.0)
        length = math.hypot(*actual)
        assert length > w * 0.2, "anchor is too close to the centre to be a tip"
        actual = (actual[0] / length, actual[1] / length)

        # Cosine similarity: 1.0 means the anchor lies exactly along the
        # direction the arrow points.
        similarity = expected[0] * actual[0] + expected[1] * actual[1]
        assert similarity > 0.9, (
            f"rotation {rotation}: tip direction {actual} does not match "
            f"expected {expected} (similarity {similarity:.3f})")

    @pytest.mark.parametrize("rotation", [0, 45, 90, 135, 180, 225, 270, 315])
    def test_the_anchor_sits_on_arrow_pixels(self, rotation, tmp_path):
        # Necessary but not sufficient, so it is kept alongside the direction
        # check rather than instead of it.
        path = str(tmp_path / f"b{rotation}.png")
        _, w, h, ax, ay = an.render_shape(
            "arrow", path, size_px=160, rotation=rotation)
        image = _open(path)
        found = any(
            0 <= int(ax) + dx < w and 0 <= int(ay) + dy < h
            and image.getpixel((int(ax) + dx, int(ay) + dy))[3] > 100
            for dx in range(-7, 8) for dy in range(-7, 8))
        assert found, (f"rotation {rotation}: no arrow pixels near the anchor "
                       f"({ax:.1f},{ay:.1f}) in {w}x{h}")

    def test_rotation_grows_the_canvas(self, tmp_path):
        _, w0, h0, _, _ = an.render_shape(
            "arrow", str(tmp_path / "a0.png"), size_px=100, rotation=0)
        _, w45, h45, _, _ = an.render_shape(
            "arrow", str(tmp_path / "a45.png"), size_px=100, rotation=45)
        assert w45 > w0 and h45 > h0


class TestNormalisation:
    def test_defaults_are_filled_in(self):
        out = an.normalise_annotation({"type": "circle"})
        assert out["size"] == an.DEFAULT_SIZE
        assert out["color"] == an.DEFAULT_COLOR
        assert out["thickness"] == an.DEFAULT_THICKNESS
        assert out["end"] > out["start"]

    def test_shape_alias_is_accepted(self):
        assert an.normalise_annotation({"shape": "arrow"})["type"] == "arrow"

    @pytest.mark.parametrize("bad", [{}, {"type": "blob"}, {"type": ""}, None])
    def test_bad_type_raises(self, bad):
        with pytest.raises(an.AnnotationError):
            an.normalise_annotation(bad)

    def test_end_before_start_raises(self):
        with pytest.raises(an.AnnotationError):
            an.normalise_annotation({"type": "circle", "start": 5, "end": 2})

    def test_out_of_range_values_are_clamped(self):
        out = an.normalise_annotation({
            "type": "circle", "size": 99, "thickness": 99, "rotation": 9999})
        assert out["size"] == an.MAX_SIZE
        assert out["thickness"] == an.MAX_THICKNESS
        assert out["rotation"] == 360.0

    def test_junk_offset_falls_back(self):
        assert an.normalise_annotation(
            {"type": "circle", "offset": "nope"})["offset"] == [0.0, 0.0]

    def test_empty_list_is_empty(self):
        assert an.normalise_annotations(None) == []
        assert an.normalise_annotations([]) == []


class TestSendcmd:
    def test_only_change_points_are_written(self):
        positions = [(0, 10, 20), (3, 10, 20), (6, 11, 20), (9, 11, 20)]
        lines = an.sendcmd_xy(positions, 30, "overlay@a0")
        # First sample writes both; then only the x that changed.
        assert len(lines) == 3
        assert lines[0].endswith("x 10;")
        assert lines[1].endswith("y 20;")
        assert lines[2].endswith("x 11;")

    def test_target_is_addressed(self):
        lines = an.sendcmd_xy([(0, 1, 2)], 30, "overlay@a7")
        assert all("overlay@a7" in line for line in lines)

    def test_format_matches_the_existing_emitter(self):
        # punch_in.sendcmd_lines writes "TIME TARGET PARAM VALUE;" and reframe_v2
        # relies on that shape working; stay identical.
        line = an.sendcmd_xy([(30, 5, 6)], 30, "overlay@a0")[0]
        parts = line.rstrip(";").split()
        assert len(parts) == 4
        assert float(parts[0]) == pytest.approx(1.0)
        assert parts[1] == "overlay@a0"
        assert parts[2] in {"x", "y"}

    def test_no_positions_gives_no_lines(self):
        assert an.sendcmd_xy([], 30, "overlay@a0") == []


class TestPositionResolution:
    def test_fixed_placement_subtracts_the_anchor(self):
        annotation = an.normalise_annotation(
            {"type": "circle", "x": 0.5, "y": 0.5})
        positions, moving = an.resolve_positions(
            annotation, None, width=1000, height=2000, fps=30,
            anchor=(50, 60), duration=10)
        assert moving is False
        # Centre of frame minus the anchor offset.
        assert positions == [(0, 450.0, 940.0)]

    def test_offset_nudges_the_placement(self):
        annotation = an.normalise_annotation(
            {"type": "circle", "x": 0.5, "y": 0.5, "offset": [0.1, -0.05]})
        positions, _ = an.resolve_positions(
            annotation, None, width=1000, height=2000, fps=30,
            anchor=(0, 0), duration=10)
        assert positions[0][1] == pytest.approx(600.0)
        assert positions[0][2] == pytest.approx(900.0)

    def test_no_track_data_falls_back_to_fixed(self):
        annotation = an.normalise_annotation(
            {"type": "arrow", "track": "mouth", "x": 0.25, "y": 0.75})
        positions, moving = an.resolve_positions(
            annotation, None, width=1000, height=1000, fps=30,
            anchor=(0, 0), duration=10)
        assert moving is False
        assert positions == [(0, 250.0, 750.0)]

    def test_a_tracked_annotation_follows_the_keypoint(self):
        track = {
            "v": 1, "fps": 30.0, "stride": 3, "width": 1000, "height": 1000,
            "samples": [
                {"t": 0.0, "detected": True, "box": [0, 0, 0.1, 0.1],
                 "points": {"mouth": [0.2, 0.2]}},
                {"t": 0.1, "detected": True, "box": [0, 0, 0.1, 0.1],
                 "points": {"mouth": [0.4, 0.4]}},
                {"t": 0.2, "detected": True, "box": [0, 0, 0.1, 0.1],
                 "points": {"mouth": [0.6, 0.6]}},
            ],
        }
        annotation = an.normalise_annotation(
            {"type": "arrow", "track": "mouth", "start": 0.0, "end": 0.2})
        positions, moving = an.resolve_positions(
            annotation, track, width=1000, height=1000, fps=30,
            anchor=(0, 0), duration=1.0)
        assert moving is True
        assert len(positions) > 1
        # It should travel from near 200 toward 600.
        assert positions[0][1] < positions[-1][1]


class TestFiltergraph:
    def _entry(self, i, label, x=0, y=0, start=0.0, end=1.0):
        return {"input": i, "label": label, "x": x, "y": y,
                "start": start, "end": end, "moving": False}

    def test_single_overlay_ends_at_out(self):
        graph = an.build_filtergraph([self._entry(1, "a0")])
        assert graph.startswith("[0:v][1:v]overlay@a0=")
        assert graph.endswith("[out]")

    def test_overlays_chain(self):
        graph = an.build_filtergraph(
            [self._entry(1, "a0"), self._entry(2, "a1")])
        assert "[v0]" in graph
        assert graph.endswith("[out]")
        assert graph.count("overlay@") == 2

    def test_time_gating_is_applied(self):
        graph = an.build_filtergraph(
            [self._entry(1, "a0", start=1.25, end=3.5)])
        assert "enable='between(t,1.250,3.500)'" in graph

    def test_sendcmd_precedes_the_overlays(self, tmp_path):
        cmd = str(tmp_path / "c.cmd")
        graph = an.build_filtergraph(
            [self._entry(1, "a0")], cmd_path=cmd)
        assert graph.index("sendcmd") < graph.index("overlay@a0")
        assert "[base]" in graph

    def test_no_sendcmd_when_nothing_moves(self):
        graph = an.build_filtergraph([self._entry(1, "a0")])
        assert "sendcmd" not in graph


@needs_ffmpeg
class TestRealComposite:
    @staticmethod
    def _clip(path, seconds=3, w=270, h=480):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
             f"color=c=black:s={w}x{h}:d={seconds}:r=30",
             "-pix_fmt", "yuv420p", str(path)],
            check=True, capture_output=True)

    @staticmethod
    def _frame_at(video, t, out):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(video),
             "-frames:v", "1", str(out)],
            check=True, capture_output=True)
        return _open(out)

    def test_a_static_circle_is_burned_in(self, tmp_path):
        clip, out = tmp_path / "c.mp4", tmp_path / "o.mp4"
        self._clip(clip)
        report = an.apply_annotations(
            str(clip),
            [{"type": "circle", "start": 0.0, "end": 3.0, "x": 0.5, "y": 0.5,
              "color": "#FF0000", "size": 0.5}],
            str(out), workdir=str(tmp_path))
        assert report["count"] == 1
        assert report["moving"] == 0

        frame = self._frame_at(out, 1.0, tmp_path / "f.png")
        reds = sum(1 for x in range(frame.width) for y in range(frame.height)
                   if frame.getpixel((x, y))[0] > 120
                   and frame.getpixel((x, y))[1] < 90)
        assert reds > 100, f"expected a red ring on the frame, found {reds} px"

    def test_time_gating_hides_it_outside_the_window(self, tmp_path):
        clip, out = tmp_path / "c.mp4", tmp_path / "o.mp4"
        self._clip(clip, seconds=4)
        an.apply_annotations(
            str(clip),
            [{"type": "circle", "start": 2.0, "end": 3.0, "x": 0.5, "y": 0.5,
              "color": "#FF0000", "size": 0.6}],
            str(out), workdir=str(tmp_path))

        def red_count(t, name):
            frame = self._frame_at(out, t, tmp_path / name)
            return sum(1 for x in range(frame.width)
                       for y in range(frame.height)
                       if frame.getpixel((x, y))[0] > 120
                       and frame.getpixel((x, y))[1] < 90)

        assert red_count(0.5, "before.png") < 20
        assert red_count(2.5, "during.png") > 100
        assert red_count(3.8, "after.png") < 20

    def test_a_tracked_arrow_moves(self, tmp_path):
        clip, out = tmp_path / "c.mp4", tmp_path / "o.mp4"
        self._clip(clip, seconds=3)
        track = {
            "v": 1, "fps": 30.0, "stride": 3, "width": 270, "height": 480,
            "samples": [
                {"t": round(i * 0.1, 2), "detected": True,
                 "box": [0.1, 0.1, 0.2, 0.2],
                 "points": {"mouth": [0.15 + i * 0.025, 0.5]}}
                for i in range(28)
            ],
        }
        report = an.apply_annotations(
            str(clip),
            [{"type": "arrow", "track": "mouth", "start": 0.0, "end": 2.8,
              "color": "#FF0000", "size": 0.3, "rotation": 30}],
            str(out), workdir=str(tmp_path), track=track)
        assert report["moving"] == 1

        def red_centroid(t, name):
            frame = self._frame_at(out, t, tmp_path / name)
            xs = [x for x in range(frame.width) for y in range(frame.height)
                  if frame.getpixel((x, y))[0] > 120
                  and frame.getpixel((x, y))[1] < 90]
            return sum(xs) / len(xs) if xs else None

        early = red_centroid(0.3, "early.png")
        late = red_centroid(2.5, "late.png")
        assert early is not None, "no arrow early in the clip"
        assert late is not None, "no arrow late in the clip"
        # The tracked point sweeps left to right, so the arrow must follow.
        assert late > early + 20, f"arrow did not move: {early} -> {late}"

    def test_two_annotations_compose(self, tmp_path):
        clip, out = tmp_path / "c.mp4", tmp_path / "o.mp4"
        self._clip(clip)
        report = an.apply_annotations(
            str(clip),
            [
                {"type": "circle", "start": 0.0, "end": 3.0,
                 "x": 0.3, "y": 0.3, "size": 0.3},
                {"type": "arrow", "start": 0.0, "end": 3.0,
                 "x": 0.7, "y": 0.7, "size": 0.3, "rotation": 180},
            ],
            str(out), workdir=str(tmp_path))
        assert report["count"] == 2
        assert os.path.getsize(out) > 5000

    def test_audio_survives(self, tmp_path):
        clip, out = tmp_path / "c.mp4", tmp_path / "o.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=black:s=270x480:d=2:r=30",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "2", str(clip)],
            check=True, capture_output=True)
        an.apply_annotations(
            str(clip), [{"type": "circle", "start": 0, "end": 2}],
            str(out), workdir=str(tmp_path))
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, timeout=60)
        assert probe.stdout.strip(), "audio stream was dropped"

    def test_a_silent_clip_does_not_fail(self, tmp_path):
        # -map 0:a? has to tolerate a clip with no audio at all.
        clip, out = tmp_path / "c.mp4", tmp_path / "o.mp4"
        self._clip(clip, seconds=1)
        report = an.apply_annotations(
            str(clip), [{"type": "circle", "start": 0, "end": 1}],
            str(out), workdir=str(tmp_path))
        assert report["count"] == 1

    def test_temp_files_are_cleaned_up(self, tmp_path):
        clip, out = tmp_path / "c.mp4", tmp_path / "o.mp4"
        self._clip(clip, seconds=1)
        an.apply_annotations(
            str(clip), [{"type": "circle", "start": 0, "end": 1}],
            str(out), workdir=str(tmp_path))
        leftovers = [p for p in os.listdir(tmp_path)
                     if p.startswith("anno_")]
        assert leftovers == [], f"left behind {leftovers}"


class TestGuards:
    def test_no_annotations_raises(self, tmp_path):
        with pytest.raises(an.AnnotationError):
            an.apply_annotations(str(tmp_path / "c.mp4"), [],
                                 str(tmp_path / "o.mp4"))

    def test_missing_clip_raises(self, tmp_path):
        with pytest.raises(an.AnnotationError):
            an.apply_annotations(
                str(tmp_path / "absent.mp4"),
                [{"type": "circle", "start": 0, "end": 1}],
                str(tmp_path / "o.mp4"))
