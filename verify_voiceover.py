"""End-to-end voiceover chain on a synthetic clip.

Exercises the real path a request takes: synthesize narration, mux it over the
clip's video stream, re-transcribe the generated speech, and burn captions from
those timings. No fixtures and no job required — the clip is generated with
lavfi, so this runs anywhere ffmpeg and the TTS backend exist.

What it proves that the unit tests cannot: the clip keeps its length, the words
recovered from the narration actually fall inside it, and the captioned result is
a playable file.

Run inside the backend container:
    docker compose exec -u 0 backend python /app/verify_voiceover.py
"""

import os
import subprocess
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import caption_presets
import tts_backends
import voiceover
from subtitles import burn_subtitles, generate_ass

SCRIPT = ("He risked the whole stream on one clip. "
          "Watch what happens right at the end.")
CLIP_SECONDS = 9
PRESET = "streamer_lime"


def make_clip(path, seconds):
    """A vertical clip with its own audio, standing in for a real short."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size=540x960:duration={seconds}:rate=30",
         "-f", "lavfi", "-i", f"sine=frequency=180:duration={seconds}",
         "-t", str(seconds), "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True, capture_output=True)


def main():
    problems = []
    backend = tts_backends.active()
    print(f"tts backend        {backend}")
    if not tts_backends.is_available(backend):
        print("backend unavailable — nothing to verify")
        return 1

    with tempfile.TemporaryDirectory() as workdir:
        clip = os.path.join(workdir, "clip.mp4")
        narration = os.path.join(workdir, "narration.wav")
        voiced = os.path.join(workdir, "voiced.mp4")
        captioned = os.path.join(workdir, "captioned.mp4")
        ass_path = os.path.join(workdir, "subs.ass")

        make_clip(clip, CLIP_SECONDS)
        clip_duration = voiceover.probe_duration(clip)
        print(f"clip duration      {clip_duration:.2f}s "
              f"(audio: {voiceover.has_audio_stream(clip)})")

        tts = tts_backends.synthesize(SCRIPT, narration)
        print(f"narration          {tts['duration_sec']}s via {tts['voice']}")

        report = voiceover.apply_voiceover(
            clip, narration, voiced, mode="mix", original_volume=0.12)
        out_duration = voiceover.probe_duration(voiced)
        print(f"voiced duration    {out_duration:.2f}s "
              f"(mode {report['mode']}, mixed {report['mixed_original']})")
        print(f"truncated          {report['narration_truncated_sec']}s")

        if abs(out_duration - clip_duration) > 0.5:
            problems.append(
                f"voiceover changed the clip length: {clip_duration:.2f}s -> "
                f"{out_duration:.2f}s. Caption timings and hook gating computed "
                f"against the original would all be wrong.")

        transcript = voiceover.narration_transcript(
            narration, clip_duration=clip_duration)
        words = [w for s in transcript.get("segments", [])
                 for w in s.get("words", [])]
        print(f"words recovered    {len(words)}")
        print(f"heard              {transcript.get('text', '').strip()[:80]}")
        if not words:
            problems.append("no word timings recovered from the narration")
        else:
            last = max(float(w["end"]) for w in words)
            print(f"last word ends     {last:.2f}s")
            if last > clip_duration + 0.5:
                problems.append(
                    f"a caption would be shown at {last:.2f}s on a "
                    f"{clip_duration:.2f}s clip")

        style = caption_presets.resolve(PRESET)
        ok = generate_ass(
            transcript, 0, clip_duration, ass_path,
            max_chars=style["max_chars"], max_duration=style["max_duration"],
            alignment=style["alignment"], fontsize=style["font_size"],
            font_name=style["font_name"], font_color=style["font_color"],
            border_color=style["border_color"],
            border_width=style["border_width"],
            highlight_color=style["highlight_color"], effect=style["effect"],
            base_opacity=style["base_opacity"], uppercase=style["uppercase"],
            margin_v=style["margin_v"])
        if not ok:
            problems.append("caption generation produced no events")
        else:
            events = [l for l in open(ass_path, encoding="utf-8-sig")
                      if l.startswith("Dialogue:")]
            print(f"caption events     {len(events)} (preset {PRESET})")
            burn_subtitles(
                voiced, ass_path, captioned,
                alignment=style["alignment"], fontsize=style["font_size"],
                font_name=style["font_name"], font_color=style["font_color"],
                border_color=style["border_color"],
                border_width=style["border_width"],
                margin_v=style["margin_v"])
            final_duration = voiceover.probe_duration(captioned)
            size = os.path.getsize(captioned)
            print(f"final              {final_duration:.2f}s, {size} bytes")
            if abs(final_duration - clip_duration) > 0.5:
                problems.append("captioning changed the clip length")
            if size < 10000:
                problems.append("final file is implausibly small")

    print()
    if problems:
        for p in problems:
            print(f"PROBLEM: {p}")
        return 1
    print("OK — script spoken over the clip, captions synced to the narration, "
          "clip length unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
