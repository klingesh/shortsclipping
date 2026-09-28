"""Subject tracking on a finished clip.

The pure logic — subject selection, smoothing, interpolation, gap handling and
recut remapping — is tested against hand-built payloads, so none of it needs
mediapipe or a video file. Detection itself is exercised separately by
verify_track.py against a real clip, because a synthetic frame is not a face and
asserting that a detector finds one in generated noise would be testing nothing.
"""

import json

import pytest

import subject_track as st


def _face(x, y, w=0.2, h=0.2, mouth=None, nose=None):
    points = {}
    if nose:
        points["nose"] = list(nose)
    if mouth:
        points["mouth"] = list(mouth)
    return {"box": [x, y, w, h], "area": w * h, "points": points}


def _sample(t, frame=0, detected=True, box=None, points=None):
    entry = {"t": t, "frame": frame, "detected": detected}
    if detected:
        entry["box"] = box or [0.4, 0.4, 0.2, 0.2]
        entry["points"] = points or {"mouth": [0.5, 0.55]}
    return entry


def _payload(samples, **extra):
    data = {"v": 1, "width": 1080, "height": 1920, "fps": 30.0,
            "stride": 3, "samples": samples}
    data.update(extra)
    return data


class TestSubjectSelection:
    def test_no_faces_gives_nothing(self):
        assert st._pick([], None) is None

    def test_first_frame_takes_the_largest(self):
        small = _face(0.1, 0.1, 0.1, 0.1)
        big = _face(0.6, 0.6, 0.3, 0.3)
        assert st._pick([small, big], None) is big

    def test_continuity_beats_size(self):
        # The heart of it: in a two-shot the other person leaning in must not
        # steal the annotation. Nearest to the previous pick wins even when the
        # other face is bigger.
        previous = _face(0.2, 0.5, nose=[0.25, 0.55])
        tracked = _face(0.21, 0.5, 0.15, 0.15, nose=[0.26, 0.55])
        bigger_other = _face(0.7, 0.5, 0.4, 0.4, nose=[0.75, 0.55])
        assert st._pick([bigger_other, tracked], previous) is tracked

    def test_a_big_jump_is_treated_as_a_new_subject(self):
        # Past the jump threshold it is a cut, not movement, so fall back to the
        # prominent face rather than dragging the annotation across the frame.
        previous = _face(0.05, 0.5, nose=[0.05, 0.5])
        far_big = _face(0.8, 0.5, 0.4, 0.4, nose=[0.85, 0.5])
        far_small = _face(0.75, 0.5, 0.1, 0.1, nose=[0.78, 0.5])
        assert st._pick([far_small, far_big], previous) is far_big

    def test_anchor_prefers_the_nose_over_the_box_centre(self):
        with_nose = _face(0.0, 0.0, 1.0, 1.0, nose=[0.3, 0.4])
        assert st._anchor(with_nose) == [0.3, 0.4]

    def test_anchor_falls_back_to_the_box_centre(self):
        assert st._anchor(_face(0.2, 0.4, 0.2, 0.2)) == pytest.approx([0.3, 0.5])


class TestSmoothing:
    def test_first_sample_passes_through(self):
        face = _face(0.4, 0.4, mouth=[0.5, 0.5])
        assert st._smooth(face, None) is face

    def test_it_moves_partway_not_all_the_way(self):
        previous = _face(0.0, 0.0, 0.2, 0.2, mouth=[0.0, 0.0])
        current = _face(1.0, 1.0, 0.2, 0.2, mouth=[1.0, 1.0])
        out = st._smooth(current, previous, alpha=0.5)
        assert out["points"]["mouth"] == [0.5, 0.5]
        assert out["box"][0] == 0.5

    def test_a_new_keypoint_is_taken_as_is(self):
        previous = _face(0.4, 0.4, mouth=[0.5, 0.5])
        current = _face(0.4, 0.4, mouth=[0.5, 0.5], nose=[0.51, 0.45])
        out = st._smooth(current, previous, alpha=0.5)
        assert out["points"]["nose"] == [0.51, 0.45]

    def test_alpha_one_disables_smoothing(self):
        previous = _face(0.0, 0.0, 0.2, 0.2, mouth=[0.0, 0.0])
        current = _face(1.0, 1.0, 0.2, 0.2, mouth=[1.0, 1.0])
        out = st._smooth(current, previous, alpha=1.0)
        assert out["points"]["mouth"] == [1.0, 1.0]


class TestPointLookup:
    def test_interpolates_between_samples(self):
        payload = _payload([
            _sample(0.0, points={"mouth": [0.0, 0.0]}),
            _sample(0.2, points={"mouth": [1.0, 1.0]}),
        ])
        assert st.point_at(payload, 0.1) == [0.5, 0.5]

    def test_clamps_before_the_first_and_after_the_last(self):
        payload = _payload([
            _sample(1.0, points={"mouth": [0.2, 0.3]}),
            _sample(2.0, points={"mouth": [0.8, 0.9]}),
        ])
        assert st.point_at(payload, 0.0) == [0.2, 0.3]
        assert st.point_at(payload, 99.0) == [0.8, 0.9]

    def test_a_long_gap_returns_none_rather_than_guessing(self):
        # Bridging a two-second hole would pin the arrow to whoever is on screen
        # now. Returning None lets the caller hide it instead.
        payload = _payload([
            _sample(0.0, points={"mouth": [0.1, 0.1]}),
            _sample(5.0, points={"mouth": [0.9, 0.9]}),
        ])
        assert st.point_at(payload, 2.5) is None

    def test_undetected_samples_are_ignored(self):
        payload = _payload([
            _sample(0.0, points={"mouth": [0.1, 0.1]}),
            _sample(0.1, detected=False),
            _sample(0.2, points={"mouth": [0.3, 0.3]}),
        ])
        assert st.point_at(payload, 0.1) == [0.2, 0.2]

    def test_missing_keypoint_name_gives_none(self):
        payload = _payload([_sample(0.0, points={"mouth": [0.1, 0.1]})])
        assert st.point_at(payload, 0.0, name="left_ear") is None

    def test_no_payload_gives_none(self):
        assert st.point_at(None, 1.0) is None
        assert st.point_at({}, 1.0) is None

    def test_all_undetected_gives_none(self):
        payload = _payload([_sample(0.0, detected=False)])
        assert st.point_at(payload, 0.0) is None

    def test_box_lookup_interpolates(self):
        payload = _payload([
            _sample(0.0, box=[0.0, 0.0, 0.2, 0.2]),
            _sample(0.2, box=[1.0, 1.0, 0.4, 0.4]),
        ])
        assert st.box_at(payload, 0.1) == pytest.approx([0.5, 0.5, 0.3, 0.3])


class TestSidecar:
    def test_suffix_matches_the_layout_ranges_pattern(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        assert st.sidecar_path(clip) == clip + ".track.json"

    def test_round_trip(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        payload = _payload([_sample(0.0)])
        st.write(clip, payload)
        assert st.read(clip) == payload

    def test_read_of_a_missing_sidecar_is_none(self, tmp_path):
        assert st.read(str(tmp_path / "absent.mp4")) is None

    def test_read_of_junk_is_none_not_an_exception(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        (tmp_path / "clip.mp4.track.json").write_text("{not json")
        assert st.read(str(clip)) is None

    def test_read_rejects_the_wrong_shape(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        (tmp_path / "clip.mp4.track.json").write_text(json.dumps([1, 2, 3]))
        assert st.read(str(clip)) is None

    def test_write_never_raises_on_an_unwritable_path(self):
        # Same contract as layout_ranges.write: losing the sidecar must not fail
        # a render that already produced a clip.
        assert st.write("/nonexistent-dir/clip.mp4", _payload([])) is not None

    def test_payload_is_json_serialisable(self):
        json.loads(json.dumps(_payload([_sample(0.0)])))


class TestRemap:
    def test_samples_outside_the_kept_segments_are_dropped(self):
        payload = _payload([
            _sample(0.0, points={"mouth": [0.1, 0.1]}),
            _sample(5.0, points={"mouth": [0.5, 0.5]}),
            _sample(9.0, points={"mouth": [0.9, 0.9]}),
        ])
        out = st.remap(payload, [{"start": 0.0, "end": 1.0}])
        assert [s["t"] for s in out["samples"]] == [0.0]

    def test_kept_segments_are_shifted_to_where_they_land(self):
        payload = _payload([
            _sample(0.5, points={"mouth": [0.1, 0.1]}),
            _sample(8.0, points={"mouth": [0.9, 0.9]}),
        ])
        out = st.remap(payload, [{"start": 0.0, "end": 1.0},
                                 {"start": 7.5, "end": 9.0}])
        # The 8.0s sample sits 0.5s into the second segment, which starts at 1.0s
        # in the output.
        assert [s["t"] for s in out["samples"]] == [0.5, 1.5]

    def test_duration_reflects_the_kept_total(self):
        payload = _payload([_sample(0.5)])
        out = st.remap(payload, [{"start": 0.0, "end": 1.0},
                                 {"start": 5.0, "end": 6.5}])
        assert out["duration"] == 2.5

    def test_coverage_is_recomputed(self):
        payload = _payload([
            _sample(0.1, detected=True),
            _sample(0.2, detected=False),
        ])
        out = st.remap(payload, [{"start": 0.0, "end": 1.0}])
        assert out["detected_samples"] == 1
        assert out["coverage"] == 0.5

    def test_malformed_segments_are_skipped(self):
        payload = _payload([_sample(0.5)])
        out = st.remap(payload, [
            {"start": "x", "end": 1.0},
            {"end": 1.0},
            {"start": 2.0, "end": 1.0},   # inverted
            {"start": 0.0, "end": 1.0},
        ])
        assert len(out["samples"]) == 1

    def test_output_stays_time_ordered(self):
        payload = _payload([
            _sample(8.0, points={"mouth": [0.9, 0.9]}),
            _sample(0.5, points={"mouth": [0.1, 0.1]}),
        ])
        out = st.remap(payload, [{"start": 0.0, "end": 1.0},
                                 {"start": 7.5, "end": 9.0}])
        times = [s["t"] for s in out["samples"]]
        assert times == sorted(times)

    def test_no_payload_gives_none(self):
        assert st.remap(None, [{"start": 0, "end": 1}]) is None


class TestConstants:
    def test_keypoint_order_matches_blazeface(self):
        # The order is fixed by the model; mouth must stay index 3 or every
        # annotation anchors to the wrong feature.
        assert st.KEYPOINTS.index("mouth") == 3
        assert st.KEYPOINTS.index("nose") == 2
        assert len(st.KEYPOINTS) == 6

    def test_tracking_does_not_import_main(self):
        # main.py loads YOLO and torch at module scope. Importing it here would
        # make an annotation preview pay for a detector it never uses and would
        # stop this module being importable in CI.
        import sys
        source = open(st.__file__, encoding="utf-8").read()
        assert "import main" not in source
        assert "from main import" not in source


class TestTrackGuards:
    def test_missing_clip_raises(self, tmp_path):
        with pytest.raises(st.TrackingUnavailable):
            st.track_clip(str(tmp_path / "absent.mp4"))

    def test_unopenable_file_raises(self, tmp_path):
        bad = tmp_path / "clip.mp4"
        bad.write_bytes(b"not a video")
        with pytest.raises(st.TrackingUnavailable):
            st.track_clip(str(bad))


class TestSamplingRate:
    """Sampling is per-second, not per-frame.

    A fixed frame stride does twice the detection work on a 60fps clip as on a
    30fps one for no extra accuracy, and short-form sources are routinely 60fps.
    Measured on a real 1440x2560 60fps clip, a fixed stride of 3 meant 770
    full-resolution detections and took minutes.
    """

    @pytest.mark.parametrize("fps,hz,expected", [
        (30.0, 10.0, 3),
        (60.0, 10.0, 6),     # the case that mattered: 60fps costs the same
        (25.0, 10.0, 2),     # 2.5 rounds to 2 (banker's rounding), so samples
                             # slightly faster than asked, which is the safe way
        (24.0, 12.0, 2),
        (30.0, 30.0, 1),
        (30.0, 100.0, 1),    # never below every frame
    ])
    def test_stride_follows_the_clip_rate(self, fps, hz, expected):
        assert st.stride_for(fps, hz) == expected

    @pytest.mark.parametrize("fps", [0, -1, None, "x"])
    def test_a_junk_frame_rate_falls_back_to_every_frame(self, fps):
        assert st.stride_for(fps) == 1

    def test_a_junk_sample_rate_does_not_divide_by_zero(self):
        assert st.stride_for(30.0, 0) >= 1
        assert st.stride_for(30.0, None) == 1

    def test_default_is_ten_per_second(self):
        assert st.DEFAULT_SAMPLE_HZ == 10.0

    def test_interval_stays_inside_the_gap_tolerance(self):
        # Sampling slower than MAX_GAP_SECONDS would make every interpolation
        # look like a dropout and point_at would return None constantly.
        assert 1.0 / st.DEFAULT_SAMPLE_HZ < st.MAX_GAP_SECONDS


class TestDownscaling:
    def test_wide_frames_are_shrunk(self):
        np = pytest.importorskip("numpy")
        frame = np.zeros((2560, 1440, 3), dtype="uint8")
        out = st._downscale(frame)
        assert out.shape[1] == st.ANALYSIS_MAX_WIDTH
        # Aspect ratio preserved, or the normalised coordinates would skew.
        assert abs(out.shape[0] / out.shape[1] - 2560 / 1440) < 0.01

    def test_small_frames_are_left_alone(self):
        np = pytest.importorskip("numpy")
        frame = np.zeros((320, 180, 3), dtype="uint8")
        assert st._downscale(frame) is frame
