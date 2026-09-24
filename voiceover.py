"""Attach script-driven narration to a finished clip.

Generates speech with tts_backends, then muxes it onto the clip's existing video
stream. The video is stream-copied (``-c:v copy``), so adding a voiceover costs
one audio encode and no quality loss — it is not a re-render.

Duration policy: the VIDEO length is authoritative. Narration shorter than the
clip is padded with silence; narration longer than the clip is cut at the end,
and the overflow is reported back rather than swallowed, because a script that
runs past the clip is a scripting problem the user needs to see. Nothing here
changes the clip's length, which keeps the caption timings, hook gating and
layout ranges that were computed against it valid.

Music beds are read from MUSIC_DIR (default ``assets/music`` inside the repo, so
it arrives through the compose bind mount without a config change). A bed shorter
than the clip is looped.
"""

import os
import re
import subprocess

from ffmpeg_utils import LOUDNORM_FILTER, METADATA_SCRUB

# Where music beds live. Inside the repo by default: the container mounts the
# repo at /app, so a track dropped here works immediately, whereas an arbitrary
# host folder would need a new compose volume and a container recreate.
DEFAULT_MUSIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "music")

MUSIC_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")

# Mixing defaults. Narration has to sit clearly on top, so the clip's own audio
# drops well back and music sits further back still.
DEFAULT_VOICE_VOLUME = 1.0
DEFAULT_ORIGINAL_VOLUME = 0.15
DEFAULT_MUSIC_VOLUME = 0.10

MODES = ("replace", "mix")


class VoiceoverError(RuntimeError):
    pass


def music_dir():
    return os.environ.get("MUSIC_DIR") or DEFAULT_MUSIC_DIR


def music_catalog():
    """Available music beds, by bare filename."""
    directory = music_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [
        {"name": name, "label": os.path.splitext(name)[0]}
        for name in names
        if name.lower().endswith(MUSIC_EXTENSIONS)
    ]


def resolve_music(name):
    """Absolute path for a music bed, or None.

    Only a bare filename from the catalog is accepted. Taking a path would let a
    request read any file the process can reach, and this value arrives over
    HTTP.
    """
    if not name:
        return None
    if os.path.basename(name) != name or name.startswith("."):
        raise VoiceoverError(f"invalid music name {name!r}")
    path = os.path.join(music_dir(), name)
    if not os.path.isfile(path):
        raise VoiceoverError(f"no such music bed {name!r}")
    return path


def probe_duration(path):
    """Container duration in seconds, or 0.0 when it cannot be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60,
        )
        return max(0.0, float((out.stdout or "").strip()))
    except (subprocess.SubprocessError, ValueError, TypeError):
        return 0.0


def has_audio_stream(path):
    """True when the file carries at least one audio stream."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        )
        return bool((out.stdout or "").strip())
    except subprocess.SubprocessError:
        return False


def _volume(value, fallback):
    try:
        return max(0.0, min(4.0, float(value)))
    except (TypeError, ValueError):
        return fallback


def loudness_enabled():
    """Same switch ffmpeg_utils.audio_encode_args honours."""
    return os.environ.get("AUDIO_NORMALIZE", "1").strip() != "0"


def build_audio_graph(*, mode, has_original, has_music,
                      voice_volume, original_volume, music_volume,
                      video_duration, normalize=True):
    """The -filter_complex string and the label carrying the final audio.

    Returned separately from the ffmpeg call so it can be unit tested without
    touching a media file.

    Input order is fixed by the caller: 0 = clip, 1 = narration, 2 = music.
    ``apad`` on every source plus ``-t`` on the output is what holds the result
    to the video's length regardless of which source is longest.
    """
    if mode not in MODES:
        raise VoiceoverError(f"unknown mode {mode!r}; expected one of {MODES}")

    parts = [f"[1:a]volume={voice_volume:g},apad[vo]"]
    sources = ["[vo]"]

    if mode == "mix" and has_original:
        parts.append(f"[0:a]volume={original_volume:g},apad[og]")
        sources.append("[og]")
    if has_music:
        parts.append(f"[2:a]volume={music_volume:g},apad[bed]")
        sources.append("[bed]")

    if len(sources) == 1:
        label = "[vo]"
    else:
        # normalize=0 keeps the per-source volume values meaningful; amix's
        # default divides every input by their count, which would quietly undo
        # them.
        parts.append(f"{''.join(sources)}amix=inputs={len(sources)}"
                     f":duration=longest:normalize=0[mix]")
        label = "[mix]"

    if normalize:
        # Loudness normalisation has to live INSIDE the complex graph. The rest
        # of the pipeline gets it from ffmpeg_utils.audio_encode_args, which
        # applies it as `-af`, and ffmpeg rejects simple `-af` filtering on a
        # stream fed by a complex filtergraph:
        #   "Simple and complex filtering cannot be used together for the same
        #    stream"
        # Appending it here keeps delivered loudness consistent with every other
        # encode instead of silently skipping it on voiced clips.
        parts.append(f"{label}{LOUDNORM_FILTER}[aout]")
        label = "[aout]"

    return ";".join(parts), label


def apply_voiceover(video_path, narration_path, output_path, *,
                    mode="replace",
                    voice_volume=DEFAULT_VOICE_VOLUME,
                    original_volume=DEFAULT_ORIGINAL_VOLUME,
                    music_path=None,
                    music_volume=DEFAULT_MUSIC_VOLUME):
    """Mux ``narration_path`` onto ``video_path``'s video stream.

    Returns a report dict with the durations involved and how much narration, if
    any, had to be cut.
    """
    if not os.path.isfile(video_path):
        raise VoiceoverError(f"clip not found: {video_path}")
    if not os.path.isfile(narration_path):
        raise VoiceoverError(f"narration not found: {narration_path}")

    video_duration = probe_duration(video_path)
    narration_duration = probe_duration(narration_path)
    if video_duration <= 0:
        raise VoiceoverError(f"could not read a duration from {video_path}")

    has_original = has_audio_stream(video_path)
    if mode == "mix" and not has_original:
        # A silent source has nothing to mix against. Falling back is right here
        # (unlike the TTS backends) because the outcome is identical either way.
        mode = "replace"

    voice_volume = _volume(voice_volume, DEFAULT_VOICE_VOLUME)
    original_volume = _volume(original_volume, DEFAULT_ORIGINAL_VOLUME)
    music_volume = _volume(music_volume, DEFAULT_MUSIC_VOLUME)

    graph, out_label = build_audio_graph(
        mode=mode, has_original=has_original, has_music=bool(music_path),
        voice_volume=voice_volume, original_volume=original_volume,
        music_volume=music_volume, video_duration=video_duration,
        normalize=loudness_enabled(),
    )

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path, "-i", narration_path]
    if music_path:
        # Loop the bed so a 30s track covers a 60s clip. -stream_loop precedes
        # its input; apad alone cannot extend a file, only pad it with silence.
        cmd += ["-stream_loop", "-1", "-i", music_path]

    cmd += [
        "-filter_complex", graph,
        "-map", "0:v",
        "-map", out_label,
        # The whole point of this pass is audio, so the video is copied through
        # untouched: no generation loss and no encode time.
        "-c:v", "copy",
        # Deliberately NOT ffmpeg_utils.audio_encode_args(): it adds loudnorm as
        # `-af`, which ffmpeg refuses to combine with -filter_complex. The same
        # filter is appended inside the graph above instead.
        "-c:a", "aac",
        # Hard clamp to the video. Without it a looped bed would run forever and
        # a long narration would stretch the clip, invalidating every timing
        # already computed against it.
        "-t", f"{video_duration:.3f}",
        *METADATA_SCRUB,
        "-movflags", "+faststart",
        output_path,
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0 or not os.path.exists(output_path):
        raise VoiceoverError(
            f"ffmpeg failed: {(proc.stderr or '').strip()[-400:]}")

    overflow = max(0.0, narration_duration - video_duration)
    return {
        "output": output_path,
        "mode": mode,
        "video_duration": round(video_duration, 3),
        "narration_duration": round(narration_duration, 3),
        # Surfaced, not swallowed: the caller shows this so a script that runs
        # long is visible instead of mysteriously cut off mid-sentence.
        "narration_truncated_sec": round(overflow, 3),
        "mixed_original": mode == "mix" and has_original,
        "music": os.path.basename(music_path) if music_path else None,
    }


def narration_transcript(narration_path, clip_duration=None):
    """Word-level transcript of generated narration, for caption sync.

    Re-transcribing synthesized speech looks wasteful when the script is already
    known, but it is what makes captions land on the right frames: the script
    has no timings. It also keeps this independent of the TTS provider, since
    not every engine returns timestamps.
    """
    from transcribe_backends import transcribe_media

    transcript = transcribe_media(narration_path)
    if clip_duration:
        # Drop anything past the clip; those words were cut by the -t clamp and
        # captioning them would show text over silence.
        for segment in transcript.get("segments", []):
            segment["words"] = [
                w for w in (segment.get("words") or [])
                if float(w.get("start", 0)) < clip_duration
            ]
        transcript["segments"] = [
            s for s in transcript.get("segments", []) if s.get("words")
        ]
    return transcript
