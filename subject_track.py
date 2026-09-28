"""Where the subject's face is, in a FINISHED clip's own coordinates.

Annotations (a circle round someone's head, an arrow at their mouth) have to
stay on target while the shot moves. The obvious place to get that from is the
reframing pass, but it is the wrong place twice over: ``reframe_v2.render``
computes its crop trajectory into a temp sendcmd file and deletes it in a
``finally``, and even if it did not, the source-to-output transform differs for
every one of the six layouts (TRACK, GENERAL, SPLIT, INSET, SCREENCAST,
ALTERNATE) — SPLIT in particular scales two halves independently and stacks
them.

So this tracks the rendered clip instead. One extra detection pass buys
coordinates that are already in the output frame, identical for every layout,
and it needs to know nothing about how the clip was made.

Coordinates are NORMALISED fractions of frame width/height, matching the
convention ``reframe_v2.apply_crop_overrides`` already uses for hand-placed
framing: fractions survive a re-encode at another resolution, pixels do not.

The result travels as a sidecar ``<clip>.track.json``, the same way
layout_ranges carries its data, and ``remap`` carries it across a recut.

This module deliberately does NOT import ``main``. main.py loads YOLO and the
torch stack at module scope, which would make an annotation preview pay for a
detector it never uses, and would stop these functions being importable in CI.
MediaPipe's face detector alone is cheap, so it is constructed here with the
same parameters main.py uses (``model_selection=1``, ``min_detection_confidence
=0.5``) so the two agree about what a face is.
"""

import json
import os
import threading

SIDECAR_SUFFIX = ".track.json"
SCHEMA_VERSION = 1

# BlazeFace returns exactly these six keypoints, in this order. MediaPipe
# computes them for every detection; main.py reads only the bounding box and
# drops them, which is why annotations could not anchor to a feature before.
# The box centre sits around the nose bridge — not where an arrow pointing at
# someone's mouth belongs.
KEYPOINTS = ("right_eye", "left_eye", "nose", "mouth",
             "right_ear", "left_ear")

# How many times a second to detect, then interpolate between. Expressed in Hz
# rather than a frame stride on purpose: a fixed stride does twice the work on a
# 60fps clip as on a 30fps one for no extra accuracy, and short-form sources are
# routinely 60fps. A face does not move far in 1/10th of a second.
DEFAULT_SAMPLE_HZ = 10.0

# Detection runs on a downscaled copy. BlazeFace resizes its input to a small
# square internally, so handing it a 1440x2560 frame costs real time and buys
# nothing; main.py downscales for the same reason (_detection_frame). This does
# not affect the output at all, because MediaPipe reports results as fractions
# of the frame rather than pixels.
ANALYSIS_MAX_WIDTH = 640

# Longest gap in seconds that is bridged by interpolation. Past this the subject
# has genuinely left (a cut, a turn away), and an annotation should hide rather
# than hover over whoever is there now.
MAX_GAP_SECONDS = 0.7

# Exponential smoothing on the tracked point. Raw per-frame detection jitters by
# a few pixels, which is invisible on a crop that moves slowly and very visible
# on a circle pinned to a face.
SMOOTHING = 0.45

# A new detection further than this (as a fraction of frame width) from the
# previous one is treated as a different person rather than a jump, so the
# annotation does not leap between faces in a two-shot.
SAME_SUBJECT_MAX_JUMP = 0.25

_detector = None
_detector_lock = threading.Lock()


class TrackingUnavailable(RuntimeError):
    pass


def sidecar_path(video_path):
    return video_path + SIDECAR_SUFFIX


def _get_detector():
    """One shared MediaPipe detector. Not thread-safe, hence the lock."""
    global _detector
    with _detector_lock:
        if _detector is None:
            try:
                import mediapipe as mp
            except ImportError as exc:
                raise TrackingUnavailable(
                    "mediapipe is not installed") from exc
            _detector = mp.solutions.face_detection.FaceDetection(
                model_selection=1, min_detection_confidence=0.5)
        return _detector


def stride_for(fps, sample_hz=DEFAULT_SAMPLE_HZ):
    """Frames to skip between detections to hit roughly ``sample_hz``.

    Never returns less than 1. Rounding may land slightly above the requested
    rate, which is the harmless direction: more samples, not fewer.
    """
    try:
        fps = float(fps)
        sample_hz = max(0.1, float(sample_hz))
    except (TypeError, ValueError):
        return 1
    if fps <= 0:
        return 1
    return max(1, int(round(fps / sample_hz)))


def _downscale(frame):
    """Shrink to ANALYSIS_MAX_WIDTH for detection. Results stay comparable
    because MediaPipe answers in fractions, not pixels."""
    import cv2

    height, width = frame.shape[:2]
    if width <= ANALYSIS_MAX_WIDTH:
        return frame
    scale = ANALYSIS_MAX_WIDTH / float(width)
    return cv2.resize(frame, (ANALYSIS_MAX_WIDTH, max(1, int(height * scale))),
                      interpolation=cv2.INTER_AREA)


def _detect(frame):
    """Faces in one BGR frame as normalised boxes and keypoints."""
    import cv2

    detector = _get_detector()
    rgb = cv2.cvtColor(_downscale(frame), cv2.COLOR_BGR2RGB)
    with _detector_lock:
        results = detector.process(rgb)
    if not results.detections:
        return []

    faces = []
    for detection in results.detections:
        box = detection.location_data.relative_bounding_box
        # MediaPipe already speaks in fractions, so there is no scaling to do
        # and no chance of an off-by-a-downscale-factor error.
        entry = {
            "box": [round(float(box.xmin), 5), round(float(box.ymin), 5),
                    round(float(box.width), 5), round(float(box.height), 5)],
            "area": float(box.width) * float(box.height),
            "points": {},
        }
        for name, kp in zip(KEYPOINTS,
                            detection.location_data.relative_keypoints or ()):
            entry["points"][name] = [round(float(kp.x), 5),
                                     round(float(kp.y), 5)]
        faces.append(entry)
    return faces


def _anchor(face):
    """The point used to decide whether two detections are the same subject."""
    points = face.get("points") or {}
    if "nose" in points:
        return points["nose"]
    x, y, w, h = face["box"]
    return [x + w / 2, y + h / 2]


def _pick(faces, previous):
    """Choose one face per frame, preferring continuity over size.

    Taking the largest face every frame reads well until a two-shot, where the
    pick flips whenever the other person leans in and the annotation jumps
    across the frame. Staying with whoever is nearest the previous pick holds it
    on one subject; size only breaks the initial tie.
    """
    if not faces:
        return None
    if previous is None:
        return max(faces, key=lambda f: f["area"])

    px, py = _anchor(previous)
    best, best_distance = None, None
    for face in faces:
        fx, fy = _anchor(face)
        distance = ((fx - px) ** 2 + (fy - py) ** 2) ** 0.5
        if best_distance is None or distance < best_distance:
            best, best_distance = face, distance

    if best_distance is not None and best_distance > SAME_SUBJECT_MAX_JUMP:
        # Too far to be the same person moving. Treat it as a new subject and
        # fall back to the most prominent face.
        return max(faces, key=lambda f: f["area"])
    return best


def _smooth(current, previous, alpha=SMOOTHING):
    """EMA a face's box and points toward the previous sample."""
    if previous is None:
        return current
    out = {
        "box": [c * alpha + p * (1 - alpha)
                for c, p in zip(current["box"], previous["box"])],
        "area": current["area"],
        "points": {},
    }
    prev_points = previous.get("points") or {}
    for name, point in (current.get("points") or {}).items():
        if name in prev_points:
            out["points"][name] = [
                c * alpha + p * (1 - alpha)
                for c, p in zip(point, prev_points[name])
            ]
        else:
            out["points"][name] = point
    return out


def track_clip(video_path, sample_hz=DEFAULT_SAMPLE_HZ, max_seconds=None,
               stride=None):
    """Detect the subject across ``video_path``.

    ``sample_hz`` sets detections per second; the frame stride is derived from
    the clip's own rate so a 60fps source costs the same as a 30fps one. Pass
    ``stride`` to override it directly.

    Returns the sidecar payload. Samples carry ``t`` in the clip's own timeline,
    which is the timeline annotations and captions are laid on.
    """
    import cv2

    if not os.path.isfile(video_path):
        raise TrackingUnavailable(f"no such clip: {video_path}")

    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        raise TrackingUnavailable(f"could not open {video_path}")

    try:
        fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0:
            # A container without a usable rate would make every timestamp
            # meaningless, so refuse rather than emit nonsense times.
            raise TrackingUnavailable(f"no frame rate reported for {video_path}")

        if stride is None:
            stride = stride_for(fps, sample_hz)
        stride = max(1, int(stride))
        frame_limit = int(max_seconds * fps) if max_seconds else None

        samples = []
        previous = None
        index = -1
        while True:
            ok = capture.grab()
            if not ok:
                break
            index += 1
            if frame_limit is not None and index > frame_limit:
                break
            if index % stride:
                continue
            ok, frame = capture.retrieve()
            if not ok or frame is None:
                continue

            chosen = _pick(_detect(frame), previous)
            if chosen is None:
                samples.append({"t": round(index / fps, 3), "frame": index,
                                "detected": False})
                continue

            smoothed = _smooth(chosen, previous)
            previous = smoothed
            samples.append({
                "t": round(index / fps, 3),
                "frame": index,
                "detected": True,
                "box": [round(v, 5) for v in smoothed["box"]],
                "points": {k: [round(v, 5) for v in p]
                           for k, p in smoothed["points"].items()},
            })
    finally:
        capture.release()

    detected = sum(1 for s in samples if s.get("detected"))
    return {
        "v": SCHEMA_VERSION,
        "width": width,
        "height": height,
        "fps": round(float(fps), 4),
        "frames": total,
        "stride": stride,
        "duration": round((total / fps) if fps else 0.0, 3),
        "samples": samples,
        "detected_samples": detected,
        "coverage": round(detected / len(samples), 4) if samples else 0.0,
    }


def write(video_path, payload):
    """Best effort, like layout_ranges.write: losing the sidecar must never
    fail a render that already produced a clip."""
    try:
        with open(sidecar_path(video_path), "w") as handle:
            json.dump(payload, handle)
    except OSError:
        pass
    return payload


def read(video_path):
    """The track recorded for this file, or None."""
    try:
        with open(sidecar_path(video_path)) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        return None
    return data


def _lerp(a, b, ratio):
    return a + (b - a) * ratio


def point_at(payload, t, name="mouth"):
    """Interpolated position of ``name`` at time ``t``, or None.

    None means "do not draw": either nothing was detected around ``t``, or the
    nearest detections are far enough apart that the subject genuinely left.
    Guessing would pin an arrow to whoever happens to be on screen instead.
    """
    if not payload:
        return None
    samples = [s for s in payload.get("samples") or []
               if s.get("detected") and name in (s.get("points") or {})]
    if not samples:
        return None

    if t <= samples[0]["t"]:
        return list(samples[0]["points"][name])
    if t >= samples[-1]["t"]:
        return list(samples[-1]["points"][name])

    for before, after in zip(samples, samples[1:]):
        if before["t"] <= t <= after["t"]:
            span = after["t"] - before["t"]
            if span > MAX_GAP_SECONDS:
                return None
            ratio = 0.0 if span <= 0 else (t - before["t"]) / span
            p0 = before["points"][name]
            p1 = after["points"][name]
            return [_lerp(p0[0], p1[0], ratio), _lerp(p0[1], p1[1], ratio)]
    return None


def box_at(payload, t):
    """Interpolated face box at ``t`` as [x, y, w, h] fractions, or None."""
    if not payload:
        return None
    samples = [s for s in payload.get("samples") or []
               if s.get("detected") and s.get("box")]
    if not samples:
        return None
    if t <= samples[0]["t"]:
        return list(samples[0]["box"])
    if t >= samples[-1]["t"]:
        return list(samples[-1]["box"])
    for before, after in zip(samples, samples[1:]):
        if before["t"] <= t <= after["t"]:
            span = after["t"] - before["t"]
            if span > MAX_GAP_SECONDS:
                return None
            ratio = 0.0 if span <= 0 else (t - before["t"]) / span
            return [_lerp(a, b, ratio)
                    for a, b in zip(before["box"], after["box"])]
    return None


def remap(payload, segments):
    """Carry a track across a cut-and-concat, like layout_ranges.remap.

    A recut keeps some stretches of the clip and drops the rest, so a sample's
    time only survives if its stretch did, shifted to where that stretch landed.
    Without this a recut clip would carry a track describing frames that are no
    longer in it, and every annotation would sit on the wrong moment.
    """
    if not payload:
        return None
    out = []
    offset = 0.0
    for seg in segments or []:
        try:
            seg_start, seg_end = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if seg_end <= seg_start:
            continue
        for sample in payload.get("samples") or []:
            t = float(sample.get("t", 0))
            if seg_start <= t < seg_end:
                moved = dict(sample)
                moved["t"] = round(t - seg_start + offset, 3)
                out.append(moved)
        offset += seg_end - seg_start

    out.sort(key=lambda s: s["t"])
    detected = sum(1 for s in out if s.get("detected"))
    remapped = dict(payload)
    remapped["samples"] = out
    remapped["duration"] = round(offset, 3)
    remapped["detected_samples"] = detected
    remapped["coverage"] = round(detected / len(out), 4) if out else 0.0
    return remapped
