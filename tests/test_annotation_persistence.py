"""Annotations surviving the edits that happen after they are placed.

The risk here is silent data loss rather than a crash. Two things in app.py
rebuild a clip's `recipe` from scratch on every edit, so anything stored inside
it disappears without an error; and annotation times are clip-relative, so a
changed cut moves them onto the wrong moments while still looking valid.
"""

import re

import pytest

import annotations as an


class TestRemap:
    def test_an_annotation_inside_a_kept_segment_survives(self):
        out = an.remap([{"type": "circle", "start": 0.2, "end": 0.8}],
                       [{"start": 0.0, "end": 1.0}])
        assert [(a["start"], a["end"]) for a in out] == [(0.2, 0.8)]

    def test_an_annotation_in_a_dropped_segment_disappears(self):
        out = an.remap([{"type": "circle", "start": 5.0, "end": 6.0}],
                       [{"start": 0.0, "end": 1.0}])
        assert out == []

    def test_a_later_segment_is_shifted_to_where_it_lands(self):
        # Segment two starts at 7.0 in the old timeline but 1.0 in the new one,
        # because segment one contributed a single second.
        out = an.remap([{"type": "arrow", "start": 7.5, "end": 8.0}],
                       [{"start": 0.0, "end": 1.0},
                        {"start": 7.0, "end": 9.0}])
        assert [(a["start"], a["end"]) for a in out] == [(1.5, 2.0)]

    def test_an_annotation_spanning_a_cut_comes_back_as_two(self):
        # Correct rather than clever: the circle really was on screen in both
        # halves, so it should be in both halves of the result.
        out = an.remap([{"type": "circle", "start": 0.5, "end": 8.0}],
                       [{"start": 0.0, "end": 1.0},
                        {"start": 7.0, "end": 9.0}])
        assert [(a["start"], a["end"]) for a in out] == [(0.5, 1.0), (1.0, 2.0)]

    def test_it_is_clipped_to_the_segment(self):
        out = an.remap([{"type": "circle", "start": -5.0, "end": 99.0}],
                       [{"start": 2.0, "end": 4.0}])
        assert [(a["start"], a["end"]) for a in out] == [(0.0, 2.0)]

    def test_other_fields_are_carried_through(self):
        out = an.remap(
            [{"type": "arrow", "start": 0.1, "end": 0.9, "track": "mouth",
              "color": "#00FF00", "rotation": 45, "size": 0.4}],
            [{"start": 0.0, "end": 1.0}])
        assert out[0]["track"] == "mouth"
        assert out[0]["color"] == "#00FF00"
        assert out[0]["rotation"] == 45
        assert out[0]["size"] == 0.4

    def test_the_input_is_not_mutated(self):
        original = [{"type": "circle", "start": 7.5, "end": 8.0}]
        an.remap(original, [{"start": 7.0, "end": 9.0}])
        assert original[0]["start"] == 7.5

    def test_output_is_time_ordered(self):
        out = an.remap(
            [{"type": "circle", "start": 8.0, "end": 8.5},
             {"type": "arrow", "start": 0.1, "end": 0.5}],
            [{"start": 0.0, "end": 1.0}, {"start": 7.0, "end": 9.0}])
        times = [a["start"] for a in out]
        assert times == sorted(times)

    @pytest.mark.parametrize("segments", [
        [{"start": "x", "end": 1.0}],
        [{"end": 1.0}],
        [{"start": 2.0, "end": 1.0}],
        [],
        None,
    ])
    def test_unusable_segments_drop_everything_rather_than_raising(self, segments):
        assert an.remap([{"type": "circle", "start": 0, "end": 1}],
                        segments) == []

    def test_malformed_annotations_are_skipped(self):
        out = an.remap(
            [{"type": "circle"},                      # no times
             {"type": "circle", "start": "a", "end": 1},
             {"type": "circle", "start": 0.1, "end": 0.5}],
            [{"start": 0.0, "end": 1.0}])
        assert len(out) == 1

    def test_zero_length_overlap_is_dropped(self):
        # Touching exactly at the boundary is not on screen.
        out = an.remap([{"type": "circle", "start": 1.0, "end": 2.0}],
                       [{"start": 0.0, "end": 1.0}])
        assert out == []


class TestStorageLocation:
    """Annotations must live beside `recipe`, not inside it."""

    def test_the_endpoint_stores_them_as_a_clip_sibling(self):
        source = open("/app/app.py", encoding="utf-8").read()
        # The write is clip_data['annotations'], not clip_data['recipe'][...].
        assert "clip_data['annotations'] = normalised" in source
        assert "recipe']['annotations'" not in source
        assert 'recipe"]["annotations"' not in source

    def test_both_recipe_rebuilds_would_have_dropped_them(self):
        # The reason for the rule: every recipe is constructed fresh, carrying
        # only these keys. If this ever grows to preserve unknown keys the
        # sibling placement is still correct, but the comment should be revisited.
        source = open("/app/app.py", encoding="utf-8").read()
        rebuilds = re.findall(r'new_recipe = \{[^}]*\}', source)
        assert len(rebuilds) >= 2, "expected the rerender and reframe rebuilds"
        for rebuild in rebuilds:
            assert "annotations" not in rebuild


class TestRerenderHandling:
    """A changed cut moves clip-relative times, so the two paths differ."""

    def test_the_fast_path_remaps_and_the_source_path_clears(self):
        source = open("/app/app.py", encoding="utf-8").read()
        # Fast path: rebased segments are in the canonical clip's own timeline,
        # which is the timeline the annotations use, so they can be remapped.
        assert "annotations.remap(" in source
        assert "rebase_segments(segments, canonical_range['start']" in source
        # Source path: no verified clip->source->clip mapping, so clear rather
        # than place them on the wrong moments.
        assert "updates['annotations'] = None" in source

    def test_the_track_sidecar_is_invalidated_on_rerender(self):
        # The clip was re-rendered, so cached face positions describe frames that
        # no longer line up.
        source = open("/app/app.py", encoding="utf-8").read()
        assert "subject_track.sidecar_path(" in source


class TestLayerOrder:
    def test_annotations_are_registered_for_canonical_resolution(self):
        # Without this a clip silently reverts to an earlier version on restore,
        # which is the failure the recut_/hooked_ entries already guard against.
        source = open("/app/app.py", encoding="utf-8").read()
        assert 'f"annotated_*_{clean}"' in source

    def test_stripping_walks_back_to_the_clean_file(self):
        import app
        assert hasattr(app, "_strip_annotations")

    def test_overlays_go_on_the_caption_stripped_file(self):
        # Captions must stay the last layer, and re-annotating must replace the
        # old overlays rather than stack a second set on them.
        source = open("/app/app.py", encoding="utf-8").read()
        block = source[source.index("async def add_annotations"):]
        block = block[:block.index("async def remove_annotations")]
        assert "_strip_burned_captions(output_dir, filename)" in block
        assert "_strip_annotations(output_dir, filename)" in block


class TestStripAnnotations:
    def test_it_walks_back_one_layer(self, tmp_path):
        import app
        (tmp_path / "base_clip_1.mp4").write_bytes(b"x")
        assert app._strip_annotations(
            str(tmp_path), "annotated_123_base_clip_1.mp4") == "base_clip_1.mp4"

    def test_it_walks_back_repeatedly(self, tmp_path):
        import app
        (tmp_path / "base_clip_1.mp4").write_bytes(b"x")
        (tmp_path / "annotated_1_base_clip_1.mp4").write_bytes(b"x")
        assert app._strip_annotations(
            str(tmp_path),
            "annotated_2_annotated_1_base_clip_1.mp4") == "base_clip_1.mp4"

    def test_it_stops_when_the_underlying_file_is_gone(self, tmp_path):
        # Same fail-safe as _strip_burned_captions: a library restore may only
        # have kept the current version.
        import app
        name = "annotated_123_base_clip_1.mp4"
        assert app._strip_annotations(str(tmp_path), name) == name

    def test_an_unannotated_name_is_unchanged(self, tmp_path):
        import app
        assert app._strip_annotations(
            str(tmp_path), "base_clip_1.mp4") == "base_clip_1.mp4"


class TestRequestValidation:
    def test_defaults_match_the_renderer(self):
        import app
        fields = app.AnnotationIn.model_fields
        assert fields["size"].default == an.DEFAULT_SIZE
        assert fields["color"].default == an.DEFAULT_COLOR
        assert fields["thickness"].default == an.DEFAULT_THICKNESS

    def test_a_clip_index_and_job_are_required(self):
        import app
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            app.AnnotationRequest(annotations=[{"type": "circle"}])

    def test_recaption_defaults_on(self):
        import app
        # A clip whose captions were stripped for the overlay pass and never put
        # back would silently lose them.
        assert app.AnnotationRequest.model_fields["recaption"].default is True
