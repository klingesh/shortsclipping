"""The TTS backend abstraction.

These tests do not synthesize anything: loading Kokoro downloads an 82M model
and spaCy's en_core_web_sm, which does not belong in a unit suite (the CI job
installs no ML stack at all). Real audio is covered by verify_tts.py, which
generates a clip and asserts it is neither empty nor silent.

What matters here is the contract around the backends: selection, the refusal to
fall back between a free local engine and a paid API, voice validation, and that
no key can leak into an error message.
"""

import os
import sys

import pytest

import tts_backends


class TestBackendSelection:
    def test_default_is_the_local_engine(self, monkeypatch):
        # The free option must be what you get by doing nothing; defaulting to
        # a paid API would bill the user for forgetting to set an env var.
        monkeypatch.delenv("TTS_BACKEND", raising=False)
        assert tts_backends.active() == "kokoro"
        assert tts_backends.DEFAULT_BACKEND == "kokoro"

    @pytest.mark.parametrize("value,expected", [
        ("elevenlabs", "elevenlabs"),
        ("ELEVENLABS", "elevenlabs"),
        ("  kokoro  ", "kokoro"),
    ])
    def test_env_selects_and_normalises(self, monkeypatch, value, expected):
        monkeypatch.setenv("TTS_BACKEND", value)
        assert tts_backends.active() == expected

    def test_empty_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TTS_BACKEND", "")
        assert tts_backends.active() == "kokoro"

    def test_unknown_backend_raises(self, tmp_path):
        with pytest.raises(tts_backends.TTSUnavailable) as excinfo:
            tts_backends.synthesize("hello", str(tmp_path / "a.wav"),
                                    backend="festival")
        assert "festival" in str(excinfo.value)

    def test_elevenlabs_availability_tracks_the_key(self, monkeypatch):
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        assert tts_backends.is_available("elevenlabs") is False
        monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
        assert tts_backends.is_available("elevenlabs") is True


class TestNoSilentFallback:
    """Backends must not substitute for each other.

    transcribe_backends falls back parakeet -> whisper because both are free and
    local. Here one option costs money per call and the other changes the voice
    of the channel, so a failure has to surface.
    """

    def test_missing_key_raises_instead_of_using_kokoro(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        out = tmp_path / "vo.mp3"
        with pytest.raises(tts_backends.TTSUnavailable):
            tts_backends.synthesize("hello", str(out), backend="elevenlabs")
        assert not out.exists()

    def test_missing_kokoro_raises_instead_of_calling_the_api(
            self, monkeypatch, tmp_path):
        # Simulate kokoro not installed. The error must name the fix, and must
        # not quietly spend money on ElevenLabs instead.
        monkeypatch.setitem(sys.modules, "kokoro", None)
        tts_backends._pipelines.clear()
        with pytest.raises(tts_backends.TTSUnavailable) as excinfo:
            tts_backends.synthesize("hello", str(tmp_path / "vo.wav"),
                                    backend="kokoro")
        message = str(excinfo.value)
        assert "requirements.txt" in message or "espeak-ng" in message


class TestVoices:
    def test_kokoro_catalog_is_not_empty_and_is_serializable(self):
        import json
        catalog = tts_backends.voice_catalog("kokoro")
        assert catalog
        json.loads(json.dumps(catalog))

    def test_every_kokoro_voice_id_was_verified(self):
        # The list was produced by loading each id in the image, with a bogus
        # control that 404'd. Guard against an "Alex" style invention creeping
        # back in: ids follow Kokoro's [ab][fm]_name convention.
        for entry in tts_backends.voice_catalog("kokoro"):
            vid = entry["id"]
            assert vid[0] in {"a", "b"}, vid
            assert vid[1] in {"f", "m"}, vid
            assert vid[2] == "_", vid

    def test_labels_only_claim_what_the_id_encodes(self):
        for entry in tts_backends.voice_catalog("kokoro"):
            assert entry["accent"] in {"American", "British"}
            assert entry["gender"] in {"female", "male"}

    def test_there_is_no_alex_voice(self):
        # Specifically checked because it was asked for: am_alex 404s against
        # the Kokoro repo. Claiming it exists would produce a runtime failure
        # at synthesis time instead of an honest empty result here.
        ids = {e["id"] for e in tts_backends.voice_catalog("kokoro")}
        assert "am_alex" not in ids

    def test_defaults_are_inside_their_catalogs(self):
        for backend in ("kokoro", "elevenlabs"):
            ids = {e["id"] for e in tts_backends.voice_catalog(backend)}
            assert tts_backends.default_voice(backend) in ids

    def test_elevenlabs_catalog_matches_the_legacy_table(self):
        # saasshorts.DEFAULT_VOICES was the old source of truth; the ids must
        # carry over or existing saved jobs would point at nothing.
        import saasshorts
        legacy = set(saasshorts.DEFAULT_VOICES.values())
        migrated = {e["id"] for e in tts_backends.voice_catalog("elevenlabs")}
        assert legacy == migrated

    def test_unknown_kokoro_voice_is_rejected_before_loading(self, tmp_path):
        # Must fail on the name, not after a model download.
        with pytest.raises(tts_backends.TTSUnavailable) as excinfo:
            tts_backends.synthesize("hi", str(tmp_path / "a.wav"),
                                    backend="kokoro", voice="am_alex")
        assert "am_alex" in str(excinfo.value)

    @pytest.mark.parametrize("voice,expected", [
        ("af_heart", "a"), ("am_puck", "a"),
        ("bf_emma", "b"), ("bm_george", "b"),
        (None, "a"),
    ])
    def test_lang_code_comes_from_the_voice_id(self, voice, expected):
        assert tts_backends._lang_code_for(voice) == expected


class TestInputValidation:
    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_empty_text_is_rejected(self, text, tmp_path):
        with pytest.raises(ValueError):
            tts_backends.synthesize(text, str(tmp_path / "a.wav"))


class TestKeyHandling:
    def test_api_key_never_appears_in_an_error(self, monkeypatch, tmp_path):
        secret = "sk-do-not-leak-me"

        class FakeResponse:
            status_code = 401
            text = "unauthorized"

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, *a, **k):
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "Client", FakeClient)
        with pytest.raises(tts_backends.TTSUnavailable) as excinfo:
            tts_backends.synthesize("hello", str(tmp_path / "a.mp3"),
                                    backend="elevenlabs", api_key=secret)
        assert secret not in str(excinfo.value)


class TestSaasshortsIntegration:
    """generate_voiceover keeps its signature but routes through the abstraction."""

    def test_it_delegates_to_tts_backends(self, monkeypatch, tmp_path):
        import saasshorts

        seen = {}

        def fake_synthesize(text, output_path, voice=None, backend=None,
                            api_key=None):
            seen.update(text=text, output_path=output_path, voice=voice,
                        backend=backend, api_key=api_key)
            return {"path": output_path, "backend": backend, "voice": voice,
                    "sample_rate": 24000, "duration_sec": 1.0}

        monkeypatch.setattr(tts_backends, "synthesize", fake_synthesize)
        monkeypatch.delenv("TTS_BACKEND", raising=False)

        out = str(tmp_path / "vo.wav")
        returned = saasshorts.generate_voiceover("hello there", "key", out)
        assert returned == out
        assert seen["backend"] == "kokoro"
        assert seen["text"] == "hello there"

    def test_a_leftover_elevenlabs_voice_id_does_not_reach_kokoro(
            self, monkeypatch, tmp_path):
        # Saved UGC jobs carry ElevenLabs ids like 21m00Tcm4TlvDq8ikWAM. Passing
        # one to Kokoro would raise; it must be swapped for the local default.
        import saasshorts

        seen = {}

        def fake_synthesize(text, output_path, voice=None, backend=None,
                            api_key=None):
            seen["voice"] = voice
            return {"path": output_path, "backend": backend, "voice": voice,
                    "sample_rate": 24000, "duration_sec": 1.0}

        monkeypatch.setattr(tts_backends, "synthesize", fake_synthesize)
        monkeypatch.setenv("TTS_BACKEND", "kokoro")
        saasshorts.generate_voiceover(
            "hi", "key", str(tmp_path / "vo.wav"),
            voice_id="21m00Tcm4TlvDq8ikWAM")
        assert seen["voice"] == tts_backends.KOKORO_DEFAULT_VOICE

    def test_elevenlabs_voice_id_is_preserved_for_that_backend(
            self, monkeypatch, tmp_path):
        import saasshorts

        seen = {}

        def fake_synthesize(text, output_path, voice=None, backend=None,
                            api_key=None):
            seen["voice"] = voice
            seen["api_key"] = api_key
            return {"path": output_path, "backend": backend, "voice": voice,
                    "sample_rate": 44100, "duration_sec": 0.0}

        monkeypatch.setattr(tts_backends, "synthesize", fake_synthesize)
        monkeypatch.setenv("TTS_BACKEND", "elevenlabs")
        saasshorts.generate_voiceover(
            "hi", "my-key", str(tmp_path / "vo.mp3"),
            voice_id="TxGEqnHWrfWFTfGW9XjX")
        assert seen["voice"] == "TxGEqnHWrfWFTfGW9XjX"
        assert seen["api_key"] == "my-key"
