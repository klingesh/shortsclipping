"""Text-to-speech backends: Kokoro (local, free) with ElevenLabs as an option.

Mirrors transcribe_backends / llm_backend: one entry point, a backend chosen by
env var, and lazy imports so an uninstalled optional backend costs nothing until
someone asks for it.

Every caller goes through synthesize(), which returns:

    {
      "path": str,            # the audio file that was written
      "backend": str,         # "kokoro" | "elevenlabs"
      "voice": str,           # voice id actually used
      "sample_rate": int,
      "duration_sec": float,  # 0.0 when the backend cannot report it
    }

TTS_BACKEND env: "kokoro" (default) | "elevenlabs".

Word-level timings are deliberately NOT part of this contract. The pipeline
already gets them by re-transcribing generated audio with faster-whisper
(saasshorts.generate_tiktok_subs), which works the same for every backend and
avoids depending on provider-specific timestamp APIs.

Kokoro notes, measured in this image rather than assumed:
  - Construction of a KPipeline costs ~3s warm and ~37s cold (it downloads the
    82M model plus spaCy's en_core_web_sm), so pipelines are cached per
    lang_code. Do not build one per request.
  - Synthesis on CPU ran at ~0.64x realtime, i.e. faster than playback.
  - Output is float32 at 24 kHz.
  - Voice ids encode language and gender: first letter a=American, b=British;
    second letter f=female, m=male. That convention is Kokoro's, so the accent
    and gender labels below are derived from it rather than invented.
  - espeak-ng must be installed (misaki falls back to it for out-of-dictionary
    words). It is an apt package, so a fresh image needs a rebuild.
"""

import os
import subprocess
import tempfile
import threading

KOKORO_REPO_ID = "hexgrad/Kokoro-82M"
KOKORO_SAMPLE_RATE = 24000
ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"

DEFAULT_BACKEND = "kokoro"

# Verified by loading each one in this image; am_alex was included as a control
# and 404s, which is how I know the list is real and not recalled. There is no
# "Alex" voice in Kokoro.
_KOKORO_VOICE_IDS = [
    # American female
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    # American male
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    # British female
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    # British male
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]

KOKORO_DEFAULT_VOICE = "af_heart"

# ElevenLabs stock voices, carried over from saasshorts.DEFAULT_VOICES so the
# UGC pipeline's existing choices keep working through the abstraction.
_ELEVENLABS_VOICES = [
    ("21m00Tcm4TlvDq8ikWAM", "Rachel", "female"),
    ("29vD33N1CtxCmqQRPOHJ", "Drew", "male"),
    ("EXAVITQu4vr4xnSDxMaL", "Bella", "female"),
    ("ErXwobaYiN019PkySvjV", "Antoni", "male"),
    ("TxGEqnHWrfWFTfGW9XjX", "Josh", "male"),
    ("yoZ06aMxZJJ28mfd3POQ", "Sam", "male"),
]

ELEVENLABS_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"

_ACCENTS = {"a": "American", "b": "British"}
_GENDERS = {"f": "female", "m": "male"}

# One pipeline per lang_code, and one synthesis at a time. The torch model is
# not documented as thread-safe and the repo already serialises its ASR models
# for the same reason (transcribe_backends._ASR_GATE).
_pipelines = {}
_pipeline_lock = threading.Lock()
_synth_lock = threading.Lock()


class TTSUnavailable(RuntimeError):
    """Raised when the requested backend cannot run in this environment."""


def _log(message):
    print(f"[tts] {message}", flush=True)


# ---------------------------------------------------------------------------
# Voice catalog
# ---------------------------------------------------------------------------

def _kokoro_voice_entry(voice_id):
    accent = _ACCENTS.get(voice_id[0], "")
    gender = _GENDERS.get(voice_id[1], "")
    name = voice_id.split("_", 1)[-1].capitalize()
    label = f"{name} ({accent} {gender})".replace("  ", " ").strip()
    return {
        "id": voice_id,
        "label": label,
        "accent": accent,
        "gender": gender,
        "backend": "kokoro",
    }


def voice_catalog(backend=None):
    """Voices for the given backend, or the active one.

    Labels carry only what the voice id actually encodes — accent and gender.
    Personality descriptions would be guesswork: the voices have not been
    auditioned here, so pick by listening rather than by label.
    """
    backend = (backend or active()).lower()
    if backend == "elevenlabs":
        return [
            {"id": vid, "label": name, "accent": "", "gender": gender,
             "backend": "elevenlabs"}
            for vid, name, gender in _ELEVENLABS_VOICES
        ]
    return [_kokoro_voice_entry(v) for v in _KOKORO_VOICE_IDS]


def default_voice(backend=None):
    backend = (backend or active()).lower()
    return (ELEVENLABS_DEFAULT_VOICE if backend == "elevenlabs"
            else KOKORO_DEFAULT_VOICE)


def active():
    """The configured backend name, whether or not it can actually run."""
    return (os.environ.get("TTS_BACKEND") or DEFAULT_BACKEND).strip().lower()


def is_available(backend=None):
    """True when the backend's dependencies are importable / configured."""
    backend = (backend or active()).lower()
    if backend == "elevenlabs":
        return bool(os.environ.get("ELEVENLABS_API_KEY"))
    try:
        import importlib.util
        return all(importlib.util.find_spec(m) is not None
                   for m in ("kokoro", "soundfile", "numpy"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Kokoro
# ---------------------------------------------------------------------------

def _lang_code_for(voice):
    """Kokoro encodes the language in the first letter of the voice id."""
    first = (voice or KOKORO_DEFAULT_VOICE)[:1].lower()
    return first if first in _ACCENTS else "a"


def _kokoro_pipeline(lang_code):
    with _pipeline_lock:
        if lang_code in _pipelines:
            return _pipelines[lang_code]
        try:
            from kokoro import KPipeline
        except ImportError as exc:
            raise TTSUnavailable(
                "kokoro is not installed. Add `kokoro` and `soundfile` to "
                "requirements.txt and `espeak-ng` to the Dockerfile, then "
                "rebuild, or set TTS_BACKEND=elevenlabs."
            ) from exc
        _log(f"loading Kokoro pipeline (lang_code={lang_code}) — "
             f"first run downloads the model")
        # repo_id is passed explicitly: omitting it works but logs a warning on
        # every construction.
        _pipelines[lang_code] = KPipeline(
            lang_code=lang_code, repo_id=KOKORO_REPO_ID)
        return _pipelines[lang_code]


def _synthesize_kokoro(text, output_path, voice):
    import numpy as np
    import soundfile as sf

    voice = voice or KOKORO_DEFAULT_VOICE
    if voice not in _KOKORO_VOICE_IDS:
        raise TTSUnavailable(
            f"unknown Kokoro voice {voice!r}; see tts_backends.voice_catalog()")

    pipeline = _kokoro_pipeline(_lang_code_for(voice))

    with _synth_lock:
        chunks = []
        for item in pipeline(text, voice=voice):
            # Older releases yield (graphemes, phonemes, audio); newer ones
            # yield an object. Support both so a dependency bump is not a break.
            audio = item[2] if isinstance(item, tuple) else item.audio
            chunks.append(np.asarray(audio, dtype="float32"))

    if not chunks:
        raise TTSUnavailable("Kokoro produced no audio for the given text")

    audio = np.concatenate(chunks)
    if float(np.abs(audio).max()) < 1e-4:
        raise TTSUnavailable("Kokoro produced silence")

    # soundfile writes WAV natively; anything else goes through ffmpeg so the
    # caller gets the extension it asked for rather than a surprise.
    if output_path.lower().endswith(".wav"):
        sf.write(output_path, audio, KOKORO_SAMPLE_RATE)
    else:
        with tempfile.TemporaryDirectory() as workdir:
            wav_path = os.path.join(workdir, "tts.wav")
            sf.write(wav_path, audio, KOKORO_SAMPLE_RATE)
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", wav_path, output_path],
                check=True, capture_output=True)

    return {
        "path": output_path,
        "backend": "kokoro",
        "voice": voice,
        "sample_rate": KOKORO_SAMPLE_RATE,
        "duration_sec": round(audio.size / KOKORO_SAMPLE_RATE, 3),
    }


# ---------------------------------------------------------------------------
# ElevenLabs
# ---------------------------------------------------------------------------

def _synthesize_elevenlabs(text, output_path, voice, api_key):
    import httpx

    api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise TTSUnavailable(
            "ElevenLabs needs an API key (ELEVENLABS_API_KEY or the "
            "X-ElevenLabs-Key header). Use TTS_BACKEND=kokoro to stay local.")

    voice = voice or ELEVENLABS_DEFAULT_VOICE
    url = f"{ELEVENLABS_API_BASE}/text-to-speech/{voice}"
    body = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.4,
            "use_speaker_boost": True,
        },
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            url,
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code != 200:
            # Deliberately does not echo the key, which is in the request
            # headers and must not reach a log or an API response body.
            raise TTSUnavailable(
                f"ElevenLabs TTS error ({resp.status_code}): {resp.text[:300]}")
        with open(output_path, "wb") as handle:
            handle.write(resp.content)

    return {
        "path": output_path,
        "backend": "elevenlabs",
        "voice": voice,
        # mp3 from the API; probing it for a duration is the caller's business
        # and ffprobe already runs downstream.
        "sample_rate": 44100,
        "duration_sec": 0.0,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def synthesize(text, output_path, voice=None, backend=None, api_key=None):
    """Render ``text`` to speech at ``output_path``.

    Raises TTSUnavailable rather than falling back between backends: swapping a
    local voice for a paid API call (or the reverse) behind the caller's back
    would either cost money silently or change the voice mid-channel. The ASR
    side falls back because both options are free and local; this one does not.
    """
    if not str(text or "").strip():
        raise ValueError("cannot synthesize empty text")

    backend = (backend or active()).lower()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    _log(f"{backend}: {len(text)} chars -> {os.path.basename(output_path)}")
    if backend == "elevenlabs":
        return _synthesize_elevenlabs(text, output_path, voice, api_key)
    if backend == "kokoro":
        return _synthesize_kokoro(text, output_path, voice)
    raise TTSUnavailable(
        f"unknown TTS backend {backend!r}; expected 'kokoro' or 'elevenlabs'")
