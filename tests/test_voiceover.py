"""Muxing generated narration onto a finished clip.

The filter graph is built as a string by a pure function so it can be asserted
without touching media; the ffmpeg behaviour that actually matters (output length
is the VIDEO's length, regardless of which input is longest) is covered by a real
encode at the bottom of this file using lavfi sources, no fixtures needed.
"""

import os
import subprocess

import pytest

import voiceover


def _has_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=30)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


needs_ffmpeg = pytest.mark.skipif(
    not _has_ffmpeg(), reason="ffmpeg not available")


def _graph(**kwargs):
    """build_audio_graph with loudnorm off, so label assertions stay readable."""
    defaults = dict(mode="replace", has_original=True, has_music=False,
                    voice_volume=1.0, original_volume=0.15, music_volume=0.1,
                    video_duration=10.0, normalize=False)
    defaults.update(kwargs)
    return voiceover.build_audio_graph(**defaults)


class TestAudioGraph:
    def test_replace_uses_narration_only(self):
        graph, label = _graph(mode="replace")
        assert label == "[vo]"
        # replace must NOT reference the clip's own audio even when it has some.
        assert "[0:a]" not in graph
        assert "amix" not in graph

    def test_mix_includes_the_original(self):
        graph, label = _graph(mode="mix", original_volume=0.2)
        assert label == "[mix]"
        assert "[0:a]volume=0.2" in graph
        assert "amix=inputs=2" in graph

    def test_mix_without_original_audio_degrades_to_one_source(self):
        # A silent source has nothing to mix; the graph must not reference [0:a]
        # or ffmpeg fails on a missing stream.
        graph, label = _graph(mode="mix", has_original=False)
        assert label == "[vo]"
        assert "[0:a]" not in graph


class TestLoudnessNormalisation:
    """Regression cover for a real failure.

    audio_encode_args applies loudnorm as `-af`, and ffmpeg rejects that on a
    stream fed by a complex filtergraph: "Simple and complex filtering cannot be
    used together for the same stream". Every real encode failed until the filter
    moved inside the graph. The string tests all passed while it was broken,
    which is why the encode tests below exist.
    """

    def test_loudnorm_is_part_of_the_graph(self):
        graph, label = voiceover.build_audio_graph(
            mode="replace", has_original=True, has_music=False,
            voice_volume=1.0, original_volume=0.15, music_volume=0.1,
            video_duration=10.0, normalize=True)
        assert "loudnorm" in graph
        assert label == "[aout]"

    def test_it_is_applied_after_mixing(self):
        graph, _ = voiceover.build_audio_graph(
            mode="mix", has_original=True, has_music=True,
            voice_volume=1.0, original_volume=0.15, music_volume=0.1,
            video_duration=10.0, normalize=True)
        # Normalising the mixed result, not each source separately.
        assert graph.index("amix") < graph.index("loudnorm")

    def test_it_can_be_switched_off(self):
        graph, label = voiceover.build_audio_graph(
            mode="replace", has_original=True, has_music=False,
            voice_volume=1.0, original_volume=0.15, music_volume=0.1,
            video_duration=10.0, normalize=False)
        assert "loudnorm" not in graph
        assert label == "[vo]"

    @pytest.mark.parametrize("value,expected", [
        (None, True), ("1", True), ("", True), ("0", False), (" 0 ", False),
    ])
    def test_env_switch_matches_ffmpeg_utils(self, monkeypatch, value, expected):
        if value is None:
            monkeypatch.delenv("AUDIO_NORMALIZE", raising=False)
        else:
            monkeypatch.setenv("AUDIO_NORMALIZE", value)
        assert voiceover.loudness_enabled() is expected

    def test_the_command_never_uses_af(self, tmp_path, monkeypatch):
        # The actual defect: -af alongside -filter_complex. Assert the built
        # command cannot regress to it.
        captured = {}

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, *a, **k):
            captured["cmd"] = cmd
            open(tmp_path / "o.mp4", "wb").write(b"x")
            return Result()

        clip, narration = tmp_path / "c.mp4", tmp_path / "n.wav"
        clip.write_bytes(b"x")
        narration.write_bytes(b"x")
        monkeypatch.setattr(voiceover, "probe_duration",
                            lambda p: 5.0 if str(p).endswith(".mp4") else 3.0)
        monkeypatch.setattr(voiceover, "has_audio_stream", lambda p: True)
        monkeypatch.setattr(voiceover.subprocess, "run", fake_run)

        voiceover.apply_voiceover(str(clip), str(narration),
                                  str(tmp_path / "o.mp4"))
        cmd = captured["cmd"]
        assert "-filter_complex" in cmd
        assert "-af" not in cmd

    def test_music_is_a_third_source(self):
        graph, _ = _graph(mode="mix", has_music=True, music_volume=0.05)
        assert "[2:a]volume=0.05" in graph
        assert "amix=inputs=3" in graph

    def test_amix_does_not_renormalise(self):
        # amix defaults to dividing every input by the number of inputs, which
        # would silently undo the volume values above it.
        graph, _ = _graph(mode="mix", has_music=True)
        assert "normalize=0" in graph

    def test_every_source_is_padded(self):
        # apad is what stops the shortest input ending the mix early.
        graph, _ = _graph(mode="mix", has_music=True)
        assert graph.count("apad") == 3

    def test_unknown_mode_raises(self):
        with pytest.raises(voiceover.VoiceoverError):
            _graph(mode="duck")


class TestVolumeClamping:
    @pytest.mark.parametrize("value,expected", [
        (-5, 0.0), (0, 0.0), (1, 1.0), (4, 4.0), (99, 4.0),
    ])
    def test_clamped_into_range(self, value, expected):
        assert voiceover._volume(value, 1.0) == expected

    @pytest.mark.parametrize("junk", [None, "loud", "", object()])
    def test_junk_falls_back(self, junk):
        assert voiceover._volume(junk, 0.5) == 0.5


class TestMusicResolution:
    def test_none_means_no_bed(self):
        assert voiceover.resolve_music(None) is None
        assert voiceover.resolve_music("") is None

    @pytest.mark.parametrize("name", [
        "../../../etc/passwd",
        "/etc/passwd",
        "sub/dir/track.mp3",
        "..\\..\\windows\\system32\\config\\sam",
        ".hidden",
    ])
    def test_paths_are_rejected(self, name):
        # This value arrives over HTTP. Accepting a path would let a request
        # read any file the process can reach.
        with pytest.raises(voiceover.VoiceoverError):
            voiceover.resolve_music(name)

    def test_missing_track_raises(self):
        with pytest.raises(voiceover.VoiceoverError):
            voiceover.resolve_music("definitely-not-here.mp3")

    def test_catalog_only_lists_audio(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MUSIC_DIR", str(tmp_path))
        (tmp_path / "beat.mp3").write_bytes(b"x")
        (tmp_path / "pad.wav").write_bytes(b"x")
        (tmp_path / "README.md").write_text("not audio")
        (tmp_path / "cover.png").write_bytes(b"x")
        names = {e["name"] for e in voiceover.music_catalog()}
        assert names == {"beat.mp3", "pad.wav"}

    def test_catalog_survives_a_missing_directory(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MUSIC_DIR", str(tmp_path / "nope"))
        assert voiceover.music_catalog() == []

    def test_resolution_honours_music_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MUSIC_DIR", str(tmp_path))
        (tmp_path / "bed.mp3").write_bytes(b"x")
        assert voiceover.resolve_music("bed.mp3") == str(tmp_path / "bed.mp3")


class TestGuards:
    def test_missing_clip_raises(self, tmp_path):
        narration = tmp_path / "n.wav"
        narration.write_bytes(b"x")
        with pytest.raises(voiceover.VoiceoverError):
            voiceover.apply_voiceover(
                str(tmp_path / "absent.mp4"), str(narration),
                str(tmp_path / "out.mp4"))

    def test_missing_narration_raises(self, tmp_path):
        clip = tmp_path / "c.mp4"
        clip.write_bytes(b"x")
        with pytest.raises(voiceover.VoiceoverError):
            voiceover.apply_voiceover(
                str(clip), str(tmp_path / "absent.wav"),
                str(tmp_path / "out.mp4"))

    def test_unreadable_duration_raises(self, tmp_path):
        # A file that exists but is not media must fail loudly, not produce a
        # zero-length clip.
        clip = tmp_path / "c.mp4"
        clip.write_bytes(b"not a video")
        narration = tmp_path / "n.wav"
        narration.write_bytes(b"not audio")
        with pytest.raises(voiceover.VoiceoverError):
            voiceover.apply_voiceover(
                str(clip), str(narration), str(tmp_path / "out.mp4"))


@needs_ffmpeg
class TestRealMux:
    """The length contract, proved with real encodes.

    Everything above checks strings. These check that the output is the video's
    length whether the narration is shorter or longer, which is the property the
    rest of the pipeline depends on: caption timings, hook gating and layout
    ranges were all computed against the clip's duration.
    """

    @staticmethod
    def _video(path, seconds, silent=False):
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-f", "lavfi", "-i", f"color=c=black:s=128x128:d={seconds}"]
        if not silent:
            cmd += ["-f", "lavfi", "-i",
                    f"sine=frequency=220:duration={seconds}"]
        cmd += ["-t", str(seconds), "-pix_fmt", "yuv420p"]
        if not silent:
            cmd += ["-c:a", "aac"]
        cmd.append(str(path))
        subprocess.run(cmd, check=True, capture_output=True)

    @staticmethod
    def _tone(path, seconds):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
             f"sine=frequency=440:duration={seconds}", str(path)],
            check=True, capture_output=True)

    def test_short_narration_is_padded_to_the_clip(self, tmp_path):
        clip, narration, out = (tmp_path / "c.mp4", tmp_path / "n.wav",
                                tmp_path / "o.mp4")
        self._video(clip, 6)
        self._tone(narration, 2)
        report = voiceover.apply_voiceover(
            str(clip), str(narration), str(out))
        assert report["narration_truncated_sec"] == 0.0
        assert abs(voiceover.probe_duration(str(out)) - 6) < 0.5

    def test_long_narration_is_cut_and_reported(self, tmp_path):
        clip, narration, out = (tmp_path / "c.mp4", tmp_path / "n.wav",
                                tmp_path / "o.mp4")
        self._video(clip, 3)
        self._tone(narration, 9)
        report = voiceover.apply_voiceover(
            str(clip), str(narration), str(out))
        # The clip must not stretch to fit the script...
        assert abs(voiceover.probe_duration(str(out)) - 3) < 0.5
        # ...and the overflow has to be visible rather than swallowed.
        assert report["narration_truncated_sec"] > 5

    def test_mix_on_a_silent_clip_falls_back_to_replace(self, tmp_path):
        clip, narration, out = (tmp_path / "c.mp4", tmp_path / "n.wav",
                                tmp_path / "o.mp4")
        self._video(clip, 4, silent=True)
        self._tone(narration, 4)
        assert voiceover.has_audio_stream(str(clip)) is False
        report = voiceover.apply_voiceover(
            str(clip), str(narration), str(out), mode="mix")
        assert report["mode"] == "replace"
        assert report["mixed_original"] is False
        assert voiceover.has_audio_stream(str(out)) is True

    def test_mix_keeps_the_original_audio(self, tmp_path):
        clip, narration, out = (tmp_path / "c.mp4", tmp_path / "n.wav",
                                tmp_path / "o.mp4")
        self._video(clip, 4)
        self._tone(narration, 4)
        report = voiceover.apply_voiceover(
            str(clip), str(narration), str(out), mode="mix")
        assert report["mode"] == "mix"
        assert report["mixed_original"] is True

    def test_a_short_music_bed_is_looped_not_truncating(self, tmp_path):
        clip, narration, bed, out = (
            tmp_path / "c.mp4", tmp_path / "n.wav",
            tmp_path / "bed.wav", tmp_path / "o.mp4")
        self._video(clip, 8)
        self._tone(narration, 8)
        self._tone(bed, 2)
        report = voiceover.apply_voiceover(
            str(clip), str(narration), str(out), music_path=str(bed))
        # A 2s bed under an 8s clip must not shorten the output.
        assert abs(voiceover.probe_duration(str(out)) - 8) < 0.5
        assert report["music"] == "bed.wav"

    def test_video_stream_is_copied_not_reencoded(self, tmp_path):
        # Codec and dimensions unchanged is the observable signature of -c:v copy.
        clip, narration, out = (tmp_path / "c.mp4", tmp_path / "n.wav",
                                tmp_path / "o.mp4")
        self._video(clip, 4)
        self._tone(narration, 4)
        voiceover.apply_voiceover(str(clip), str(narration), str(out))

        def describe(path):
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=60)
            return res.stdout.strip()

        assert describe(clip) == describe(out)
