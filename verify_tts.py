"""Generate real speech through tts_backends and prove it is usable.

The unit suite deliberately never loads a model, so this is what actually
confirms the local engine works: it synthesizes, checks the audio is neither
empty nor silent, and re-transcribes it with the same faster-whisper path the
pipeline uses for captions. That last step is the one that matters — a voiceover
is only useful here if word timings can be recovered from it.

Run inside the backend container:
    docker compose exec -u 0 backend python /app/verify_tts.py
"""

import os
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tts_backends

SCRIPT = ("This guy actually risked everything on stream. "
          "Nobody expected what happened next.")


def main():
    backend = tts_backends.active()
    print(f"backend            {backend}")
    print(f"available          {tts_backends.is_available(backend)}")
    catalog = tts_backends.voice_catalog(backend)
    print(f"voices             {len(catalog)}")
    print(f"default voice      {tts_backends.default_voice(backend)}")

    if not tts_backends.is_available(backend):
        print("\nbackend unavailable — nothing to verify")
        return 1

    with tempfile.TemporaryDirectory() as workdir:
        out = os.path.join(workdir, "voiceover.wav")

        t0 = time.time()
        try:
            result = tts_backends.synthesize(SCRIPT, out)
        except tts_backends.TTSUnavailable as exc:
            print(f"\nSYNTHESIS FAILED: {exc}")
            return 1
        took = time.time() - t0

        size = os.path.getsize(result["path"])
        dur = result["duration_sec"]
        print()
        print(f"wrote              {os.path.basename(result['path'])} ({size} bytes)")
        print(f"voice              {result['voice']}")
        print(f"sample rate        {result['sample_rate']}")
        print(f"duration           {dur}s")
        print(f"synthesis time     {took:.2f}s "
              f"({took / dur:.2f}x realtime)" if dur else "")

        problems = []
        if size < 1000:
            problems.append("file is implausibly small")
        if dur and dur < 1.0:
            problems.append(f"only {dur}s of audio for {len(SCRIPT)} chars")

        # The real test: can the caption pipeline recover word timings from it?
        print()
        try:
            from transcribe_backends import transcribe_media
            t0 = time.time()
            transcript = transcribe_media(result["path"])
            words = [w for seg in transcript.get("segments", [])
                     for w in seg.get("words", [])]
            print(f"re-transcribed     {len(words)} words in {time.time() - t0:.1f}s")
            print(f"heard              {transcript.get('text', '').strip()[:90]}")
            if not words:
                problems.append("no word timings recovered — captions could "
                                "not be synced to this audio")
            else:
                monotonic = all(
                    words[i]["start"] <= words[i + 1]["start"]
                    for i in range(len(words) - 1))
                spans = all(w["end"] > w["start"] for w in words)
                print(f"timings monotonic  {monotonic}")
                print(f"all spans positive {spans}")
                if not monotonic:
                    problems.append("word timings are not monotonic")
                if not spans:
                    problems.append("some words have zero or negative duration")
        except Exception as exc:
            problems.append(f"re-transcription failed: {type(exc).__name__}: {exc}")

        print()
        if problems:
            for problem in problems:
                print(f"PROBLEM: {problem}")
            return 1
        print("OK — speech generated locally and word timings recovered")
        return 0


if __name__ == "__main__":
    sys.exit(main())
