"""Run subject tracking against a real clip and report what it found.

The unit tests cover selection, smoothing, interpolation and remapping against
hand-built payloads. None of that proves MediaPipe actually finds a face in a
real frame, and a generated test pattern is not a face, so detection is verified
here instead — on an actual vertical clip with an actual person in it.

Pass a clip path, or leave it and reference_vertical.mp4 is used:

    docker compose exec -u 0 backend python /app/verify_track.py [clip.mp4]
"""

import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subject_track as st

DEFAULT_CLIP = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "reference_vertical.mp4")


def main():
    clip = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CLIP
    # Bounded by default. Decoding a 1440x2560 60fps clip end to end takes
    # minutes on CPU, and a few seconds of it is enough to prove detection works
    # and the coordinates are in the right space.
    limit = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    if not os.path.isfile(clip):
        print(f"no clip at {clip}")
        return 1

    print(f"clip               {os.path.basename(clip)}")
    print(f"analysing          first {limit:g}s", flush=True)
    started = time.time()
    try:
        payload = st.track_clip(clip, max_seconds=limit)
    except st.TrackingUnavailable as exc:
        print(f"TRACKING FAILED: {exc}")
        return 1
    elapsed = time.time() - started

    samples = payload["samples"]
    print(f"resolution         {payload['width']}x{payload['height']}")
    print(f"fps                {payload['fps']}")
    print(f"duration           {payload['duration']}s")
    print(f"stride             {payload['stride']}")
    print(f"samples            {len(samples)}")
    print(f"detected           {payload['detected_samples']} "
          f"({payload['coverage'] * 100:.1f}% coverage)")
    analysed = min(limit, payload["duration"] or limit)
    print(f"tracking time      {elapsed:.1f}s for {analysed:g}s of video "
          f"({elapsed / max(analysed, 0.001):.2f}x realtime)")

    problems = []
    if payload["width"] <= 0 or payload["height"] <= 0:
        problems.append("could not read the frame size")
    if not samples:
        problems.append("no samples produced")
    if payload["coverage"] == 0:
        problems.append("no face found in any sampled frame — detection is "
                        "not working, or this clip has no face in it")

    detected = [s for s in samples if s.get("detected")]
    if detected:
        first = detected[0]
        print(f"first detection    t={first['t']}s box={first['box']}")
        print(f"keypoints          {sorted(first.get('points', {}))}")

        # Every value is a fraction of the frame, so anything outside 0..1 means
        # a coordinate-space mistake rather than an unusual face.
        out_of_range = 0
        for sample in detected:
            values = list(sample["box"])
            for point in (sample.get("points") or {}).values():
                values.extend(point)
            if any(v < -0.2 or v > 1.2 for v in values):
                out_of_range += 1
        print(f"out-of-frame       {out_of_range} samples")
        if out_of_range:
            problems.append(
                f"{out_of_range} samples fall outside the frame — coordinates "
                f"are not normalised the way the renderer will assume")

        if not any("mouth" in (s.get("points") or {}) for s in detected):
            problems.append("no mouth keypoint on any detection — the arrow "
                            "annotation has nothing to anchor to")

        # Interpolated lookups are what the renderer calls per frame.
        mouth_hits = 0
        checks = 0
        t = 0.0
        while t < analysed:
            checks += 1
            if st.point_at(payload, t, "mouth"):
                mouth_hits += 1
            t += 0.5
        print(f"mouth lookups      {mouth_hits}/{checks} resolved")

        # How far the tracked mouth travels tells us whether smoothing left it
        # usable: a dead-still value would mean it is not tracking at all.
        xs = [s["points"]["mouth"][0] for s in detected
              if "mouth" in (s.get("points") or {})]
        ys = [s["points"]["mouth"][1] for s in detected
              if "mouth" in (s.get("points") or {})]
        if xs:
            print(f"mouth x range      {min(xs):.3f} to {max(xs):.3f}")
            print(f"mouth y range      {min(ys):.3f} to {max(ys):.3f}")

    # Sidecar round trip, the way the renderer will load it.
    st.write(clip, payload)
    reloaded = st.read(clip)
    print(f"sidecar            {os.path.basename(st.sidecar_path(clip))} "
          f"({os.path.getsize(st.sidecar_path(clip))} bytes)")
    if reloaded is None or len(reloaded.get("samples", [])) != len(samples):
        problems.append("sidecar did not round trip")

    print()
    if problems:
        for problem in problems:
            print(f"PROBLEM: {problem}")
        return 1
    print("OK — subject tracked in output coordinates with mouth keypoints")
    return 0


if __name__ == "__main__":
    sys.exit(main())
