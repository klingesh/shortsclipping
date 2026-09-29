import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { X, Loader2, AlertCircle, Play, Pause, Trash2, Circle,
         ArrowDown, Crosshair, RotateCcw } from 'lucide-react';
import { getApiUrl } from '../config';
import { apiJson } from '../lib/api';

// Placing the red circles and arrows the reference edits use.
//
// The hard part is not drawing them, it is saying WHEN and WHERE, and a still
// frame answers neither. So this works against the clip itself: scrub to the
// moment, drag the shape onto the thing being pointed at, and the in/out times
// come from the playhead rather than from typing numbers.
//
// Two placement modes, because both are needed:
//   - fixed      the shape stays put. Right for pointing at a scoreboard, a
//                chat message, anything nailed to the frame.
//   - follow     the shape rides a face keypoint. Right for pointing at whoever
//                is talking, which is what the reference clips do.
//
// Coordinates are fractions of the frame, never pixels, so they survive the
// re-encode that burns them in and any later change of resolution.

// Geometry mirrored from annotations.py. The preview is worthless if it does not
// agree with the burn, and these are the numbers the burn uses:
// _draw_circle insets the ring by half its stroke; _draw_arrow puts a 0.72-wide,
// 0.52-tall head on a short shaft with the tip at 0.98 down.
const ARROW_HEAD_W = 0.72;
const ARROW_HEAD_H = 0.52;
const ARROW_TIP_Y = 0.98;
const ARROW_SHAFT_MIN = 0.26;
const ARROW_TAIL_Y = 0.04;

const SIZE_MIN = 0.02;
const SIZE_MAX = 1.5;
const THICK_MIN = 0.01;
const THICK_MAX = 0.5;

const DEFAULT_COLOR = '#FF2D2D';
const DEFAULT_SIZE = 0.28;
const DEFAULT_THICKNESS = 0.09;
const MAX_ANNOTATIONS = 20;   // the backend rejects more

// The reference edits are all red, but a red ring vanishes on red content.
const SWATCHES = ['#FF2D2D', '#FFD400', '#00E5FF', '#39FF14', '#FFFFFF', '#FF00E5'];

const KEYPOINT_LABELS = {
    mouth: 'mouth',
    nose: 'nose',
    left_eye: 'left eye',
    right_eye: 'right eye',
    left_ear: 'left ear',
    right_ear: 'right ear',
};

const fmt = (s) => {
    const whole = Math.max(0, s || 0);
    const m = Math.floor(whole / 60);
    const r = whole % 60;
    return `${m}:${r.toFixed(1).padStart(4, '0')}`;
};

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// The same rotation PIL applies, in the same y-down coordinates: a downward
// vector (0, +d) must land on (d·sin θ, d·cos θ). Used to find where the anchor
// ends up after the shape is turned, since that is the point which has to sit on
// the target.
const rotatePoint = ([px, py], [cx, cy], degrees) => {
    const rad = (degrees * Math.PI) / 180;
    const cos = Math.cos(rad);
    const sin = Math.sin(rad);
    const dx = px - cx;
    const dy = py - cy;
    return [cx + dx * cos + dy * sin, cy - dx * sin + dy * cos];
};

// Anchor within the shape's own square, as a fraction of it: the point that
// lands on the target. A circle points with its centre, an arrow with its tip.
const anchorOf = (type) => (type === 'arrow' ? [0.5, ARROW_TIP_Y] : [0.5, 0.5]);

// Interpolate a tracked keypoint client-side, matching subject_track.point_at:
// null means "not detected around here", and a gap wider than the backend
// tolerates also means null rather than a guess.
const MAX_GAP_SECONDS = 0.7;
const pointAt = (track, t, name) => {
    const samples = (track?.samples || []).filter(
        (s) => s.detected && s.points && name in s.points);
    if (!samples.length) return null;
    if (t <= samples[0].t) return samples[0].points[name];
    if (t >= samples[samples.length - 1].t) return samples[samples.length - 1].points[name];
    for (let i = 0; i < samples.length - 1; i += 1) {
        const a = samples[i];
        const b = samples[i + 1];
        if (a.t <= t && t <= b.t) {
            const span = b.t - a.t;
            if (span > MAX_GAP_SECONDS) return null;
            const ratio = span <= 0 ? 0 : (t - a.t) / span;
            return [
                a.points[name][0] + (b.points[name][0] - a.points[name][0]) * ratio,
                a.points[name][1] + (b.points[name][1] - a.points[name][1]) * ratio,
            ];
        }
    }
    return null;
};

// Where the shape's anchor sits at time t, as frame fractions. Tracked shapes
// read the keypoint and add their offset; fixed ones use x/y. Mirrors
// annotations.resolve_positions, including its fallback to x/y when the track
// has nothing to say.
const anchorPointAt = (annotation, track, t) => {
    const [ox, oy] = annotation.offset || [0, 0];
    if (annotation.track && track) {
        const point = pointAt(track, t, annotation.track);
        if (point) return [point[0] + ox, point[1] + oy];
    }
    return [annotation.x + ox, annotation.y + oy];
};

let seq = 0;
const makeAnnotation = (type, atTime, duration) => {
    seq += 1;
    const start = clamp(atTime, 0, Math.max(0, duration - 0.3));
    return {
        key: `a${seq}`,           // local only, stripped before POST
        type,
        start,
        end: Math.min(duration || start + 1.5, start + 1.5),
        size: DEFAULT_SIZE,
        color: DEFAULT_COLOR,
        thickness: DEFAULT_THICKNESS,
        rotation: 0,
        x: 0.5,
        y: 0.5,
        track: null,
        offset: [0, 0],
    };
};

export default function AnnotationEditor({ jobId, clipIndex, clipTitle, videoUrl,
                                           existing, onClose, onAnnotated }) {
    const videoRef = useRef(null);
    const frameRef = useRef(null);

    // Reopening must show what is already burned in, otherwise saving again
    // would silently drop every earlier shape: the endpoint replaces the whole
    // set rather than appending to it.
    const [items, setItems] = useState(() => (existing || []).map((a) => {
        seq += 1;
        return { ...a, key: `a${seq}`, offset: a.offset || [0, 0] };
    }));
    const [selected, setSelected] = useState(null);
    const [time, setTime] = useState(0);
    const [duration, setDuration] = useState(0);
    const [playing, setPlaying] = useState(false);
    const [dragging, setDragging] = useState(false);

    const [track, setTrack] = useState(null);
    const [trackState, setTrackState] = useState('idle');   // idle|loading|ready|failed
    const [trackError, setTrackError] = useState(null);

    const [saving, setSaving] = useState(false);
    const [removing, setRemoving] = useState(false);
    const [error, setError] = useState(null);

    const current = items.find((a) => a.key === selected) || null;

    const update = useCallback((key, patch) => {
        setItems((list) => list.map((a) => (a.key === key ? { ...a, ...patch } : a)));
    }, []);

    // Face positions cost a detection pass over the clip, so they are fetched
    // only when someone actually asks a shape to follow the subject.
    const ensureTrack = useCallback(async () => {
        if (trackState === 'ready' || trackState === 'loading') return track;
        setTrackState('loading');
        setTrackError(null);
        try {
            const res = await apiJson(`/api/clip/${jobId}/${clipIndex}/track`);
            setTrack(res);
            setTrackState('ready');
            return res;
        } catch (e) {
            setTrackError(e?.detail || e?.message || 'Could not find a face to follow.');
            setTrackState('failed');
            return null;
        }
    }, [jobId, clipIndex, track, trackState]);

    // Playback drives the playhead; the shapes are filtered by it, so what is on
    // screen while playing is what the burn will show.
    useEffect(() => {
        const v = videoRef.current;
        if (!v) return undefined;
        const onTime = () => setTime(v.currentTime);
        const onMeta = () => setDuration(v.duration || 0);
        const onEnd = () => setPlaying(false);
        v.addEventListener('timeupdate', onTime);
        v.addEventListener('loadedmetadata', onMeta);
        v.addEventListener('ended', onEnd);
        return () => {
            v.removeEventListener('timeupdate', onTime);
            v.removeEventListener('loadedmetadata', onMeta);
            v.removeEventListener('ended', onEnd);
        };
    }, []);

    const seek = useCallback((t) => {
        const v = videoRef.current;
        const next = clamp(t, 0, duration || 0);
        setTime(next);
        if (v) v.currentTime = next;
    }, [duration]);

    const togglePlay = useCallback(() => {
        const v = videoRef.current;
        if (!v) return;
        if (playing) { v.pause(); setPlaying(false); }
        else { v.play().then(() => setPlaying(true)).catch(() => {}); }
    }, [playing]);

    const addShape = useCallback((type) => {
        if (items.length >= MAX_ANNOTATIONS) {
            setError(`That is the limit (${MAX_ANNOTATIONS}). Remove one first.`);
            return;
        }
        setError(null);
        const created = makeAnnotation(type, time, duration || 1.5);
        setItems((list) => [...list, created]);
        setSelected(created.key);
    }, [items.length, time, duration]);

    const removeShape = useCallback((key) => {
        setItems((list) => list.filter((a) => a.key !== key));
        setSelected((s) => (s === key ? null : s));
    }, []);

    // Selecting a shape jumps to it. Otherwise the panel would be editing
    // something invisible, and the drag target would be nowhere on screen.
    const select = useCallback((key) => {
        setSelected(key);
        const found = items.find((a) => a.key === key);
        if (found && (time < found.start || time > found.end)) seek(found.start);
    }, [items, time, seek]);

    // Dragging writes x/y for a fixed shape, and the offset from the keypoint
    // for a tracked one — the two things the backend actually reads in each
    // mode. Writing x/y while tracking would look like it worked and change
    // nothing.
    const placeAt = useCallback((clientX, clientY) => {
        const el = frameRef.current;
        if (!el || !current) return;
        const rect = el.getBoundingClientRect();
        const fx = clamp((clientX - rect.left) / rect.width, 0, 1);
        const fy = clamp((clientY - rect.top) / rect.height, 0, 1);
        if (current.track && track) {
            const anchor = pointAt(track, time, current.track);
            if (anchor) {
                update(current.key, { offset: [fx - anchor[0], fy - anchor[1]] });
                return;
            }
        }
        update(current.key, { x: fx, y: fy, offset: [0, 0] });
    }, [current, track, time, update]);

    useEffect(() => {
        if (!dragging) return undefined;
        const move = (e) => {
            const p = e.touches ? e.touches[0] : e;
            placeAt(p.clientX, p.clientY);
            if (e.cancelable) e.preventDefault();
        };
        const up = () => setDragging(false);
        window.addEventListener('mousemove', move);
        window.addEventListener('mouseup', up);
        window.addEventListener('touchmove', move, { passive: false });
        window.addEventListener('touchend', up);
        return () => {
            window.removeEventListener('mousemove', move);
            window.removeEventListener('mouseup', up);
            window.removeEventListener('touchmove', move);
            window.removeEventListener('touchend', up);
        };
    }, [dragging, placeAt]);

    const visible = useMemo(
        () => items.filter((a) => time >= a.start && time <= a.end),
        [items, time]);

    const handleSave = async () => {
        if (!items.length || saving) return;
        setSaving(true);
        setError(null);
        try {
            // `key` is a local handle for React lists only; the backend rejects
            // unknown fields' worth of noise, so it is dropped here.
            const payload = items.map(({ key: _key, ...a }) => ({
                ...a,
                start: Number(a.start.toFixed(3)),
                end: Number(a.end.toFixed(3)),
                size: Number(a.size.toFixed(4)),
                thickness: Number(a.thickness.toFixed(4)),
                rotation: Number(a.rotation.toFixed(1)),
                x: Number(a.x.toFixed(4)),
                y: Number(a.y.toFixed(4)),
                offset: [Number((a.offset?.[0] || 0).toFixed(4)),
                         Number((a.offset?.[1] || 0).toFixed(4))],
            }));
            const res = await apiJson('/api/annotations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    job_id: jobId, clip_index: clipIndex, annotations: payload,
                }),
            });
            if (onAnnotated) onAnnotated(clipIndex, res);
            onClose();
        } catch (e) {
            setError(e?.detail || e?.message || 'Burning the annotations failed.');
        } finally {
            setSaving(false);
        }
    };

    const handleRemoveAll = async () => {
        if (removing) return;
        setRemoving(true);
        setError(null);
        try {
            const res = await apiJson('/api/annotations/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_id: jobId, clip_index: clipIndex }),
            });
            if (onAnnotated) onAnnotated(clipIndex, { ...res, annotations: [] });
            onClose();
        } catch (e) {
            setError(e?.detail || e?.message || 'Could not remove the annotations.');
        } finally {
            setRemoving(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 bg-black/80 flex items-end sm:items-center justify-center p-0 sm:p-4">
            <div className="card w-full max-w-4xl max-h-[92vh] sm:max-h-[90vh] flex flex-col rounded-b-none sm:rounded-card animate-sheet-up sm:animate-none">
                <div className="flex items-center justify-between p-4 border-b border-rule">
                    <div className="flex items-center gap-2.5 min-w-0">
                        <Circle size={18} className="text-brass shrink-0" />
                        <div className="min-w-0">
                            <h2 className="text-base font-medium text-ink lowercase truncate">annotations</h2>
                            {clipTitle && <p className="text-xs text-muted truncate">{clipTitle}</p>}
                        </div>
                    </div>
                    <button onClick={onClose} className="p-1.5 hover:bg-paper3 rounded-input transition-colors">
                        <X size={18} className="text-muted" />
                    </button>
                </div>

                <div className="flex-1 overflow-y-auto overscroll-contain custom-scrollbar p-4">
                    <p className="text-xs text-muted leading-relaxed mb-4">
                        Scrub to the moment, add a circle or an arrow, then drag it onto
                        what it should point at. Times come from the playhead, so what you
                        see while playing is what gets burned in.
                    </p>

                    {error && (
                        <div className="flex items-start gap-2 text-xs text-warn border border-warn/40 rounded-input p-3 mb-4">
                            <AlertCircle size={14} className="shrink-0 mt-0.5" />
                            <span>{error}</span>
                        </div>
                    )}

                    <div className="flex flex-col sm:flex-row gap-4">
                        {/* The frame. Shapes sit on top of the real clip, at the real
                            fractions, so placement is judged against the picture. */}
                        <div className="sm:w-1/2 shrink-0">
                            <div
                                ref={frameRef}
                                onMouseDown={(e) => {
                                    if (!current) return;
                                    setDragging(true);
                                    placeAt(e.clientX, e.clientY);
                                }}
                                onTouchStart={(e) => {
                                    if (!current) return;
                                    setDragging(true);
                                    const p = e.touches[0];
                                    placeAt(p.clientX, p.clientY);
                                }}
                                className={`relative overflow-hidden rounded-input select-none bg-black border ${
                                    current ? 'border-brass cursor-crosshair' : 'border-rule'}`}
                            >
                                <video
                                    ref={videoRef}
                                    src={getApiUrl(videoUrl)}
                                    playsInline
                                    preload="metadata"
                                    className="w-full block max-h-[46vh] mx-auto pointer-events-none"
                                />

                                {visible.map((a) => (
                                    <ShapeOverlay
                                        key={a.key}
                                        annotation={a}
                                        track={track}
                                        time={time}
                                        selected={a.key === selected}
                                    />
                                ))}

                                {/* Where the tracked keypoint actually is, so a
                                    follow offset can be judged rather than guessed. */}
                                {current?.track && track && (
                                    <KeypointMarker
                                        point={pointAt(track, time, current.track)}
                                    />
                                )}
                            </div>

                            <div className="flex items-center gap-2 mt-2">
                                <button
                                    onClick={togglePlay}
                                    className="p-1.5 rounded-input hover:bg-paper3 text-ink2 hover:text-brass transition-colors"
                                    title="play the clip"
                                >
                                    {playing ? <Pause size={14} /> : <Play size={14} />}
                                </button>
                                <input
                                    type="range"
                                    min={0}
                                    max={Math.max(0.1, duration)}
                                    step={0.05}
                                    value={time}
                                    onChange={(e) => seek(Number(e.target.value))}
                                    className="flex-1 accent-brass"
                                    aria-label="playhead"
                                />
                                <span className="readout text-[11px] text-muted tabular-nums w-20 text-right">
                                    {fmt(time)} / {fmt(duration)}
                                </span>
                            </div>

                            <div className="flex items-center gap-2 mt-3">
                                <button
                                    onClick={() => addShape('circle')}
                                    className="btn-quiet flex-1 py-2 text-xs flex items-center justify-center gap-1.5"
                                >
                                    <Circle size={14} /> circle
                                </button>
                                <button
                                    onClick={() => addShape('arrow')}
                                    className="btn-quiet flex-1 py-2 text-xs flex items-center justify-center gap-1.5"
                                >
                                    <ArrowDown size={14} /> arrow
                                </button>
                            </div>
                        </div>

                        {/* The list, and the controls for whichever shape is selected. */}
                        <div className="sm:w-1/2 min-w-0 space-y-3">
                            {items.length === 0 && (
                                <p className="text-xs text-muted border border-dashed border-rule rounded-input p-4 text-center">
                                    no annotations yet — add a circle or an arrow
                                </p>
                            )}

                            {items.map((a, i) => (
                                <div key={a.key} className="space-y-2">
                                    <button
                                        onClick={() => select(a.key)}
                                        className={`w-full flex items-center gap-2 text-left px-2.5 py-2 rounded-input border transition-colors ${
                                            a.key === selected
                                                ? 'border-brass bg-paper3'
                                                : 'border-rule hover:bg-paper3'}`}
                                    >
                                        {a.type === 'arrow'
                                            ? <ArrowDown size={13} style={{ color: a.color }} className="shrink-0" />
                                            : <Circle size={13} style={{ color: a.color }} className="shrink-0" />}
                                        <span className="readout text-[11px] text-ink2 truncate">
                                            {a.type} {i + 1} · {fmt(a.start)}–{fmt(a.end)}
                                            {a.track ? ` · follows ${KEYPOINT_LABELS[a.track] || a.track}` : ''}
                                        </span>
                                        <span
                                            role="button"
                                            tabIndex={0}
                                            onClick={(e) => { e.stopPropagation(); removeShape(a.key); }}
                                            onKeyDown={(e) => {
                                                if (e.key === 'Enter' || e.key === ' ') {
                                                    e.stopPropagation();
                                                    removeShape(a.key);
                                                }
                                            }}
                                            className="ml-auto p-1 rounded text-muted hover:text-warn transition-colors shrink-0"
                                            title="delete this annotation"
                                        >
                                            <Trash2 size={13} />
                                        </span>
                                    </button>

                                    {a.key === selected && (
                                        <ShapeControls
                                            annotation={a}
                                            time={time}
                                            track={track}
                                            trackState={trackState}
                                            trackError={trackError}
                                            onChange={(patch) => update(a.key, patch)}
                                            onSeek={seek}
                                            onEnableTrack={async (name) => {
                                                const got = await ensureTrack();
                                                if (!got) return;
                                                // Keep the shape exactly where it looks
                                                // right now, so turning follow on — or
                                                // switching keypoint — never makes it
                                                // jump. Measured from its current visual
                                                // position, which covers both cases;
                                                // reading x/y would be stale for a shape
                                                // that was already following something.
                                                const [px, py] = anchorPointAt(a, track, time);
                                                const anchor = pointAt(got, time, name);
                                                update(a.key, anchor
                                                    ? { track: name, offset: [px - anchor[0], py - anchor[1]] }
                                                    : { track: name });
                                            }}
                                            onDisableTrack={() => {
                                                // Freeze it where it currently appears,
                                                // rather than snapping back to a stale x/y.
                                                const [px, py] = anchorPointAt(a, track, time);
                                                update(a.key, { track: null, x: clamp(px, 0, 1), y: clamp(py, 0, 1), offset: [0, 0] });
                                            }}
                                        />
                                    )}
                                </div>
                            ))}
                        </div>
                    </div>
                </div>

                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 border-t border-rule">
                    <div className="flex items-center gap-3">
                        <span className="text-xs text-muted">
                            {items.length === 0
                                ? 'nothing to burn in'
                                : `${items.length} annotation${items.length > 1 ? 's' : ''}`}
                        </span>
                        {(existing || []).length > 0 && (
                            <button
                                onClick={handleRemoveAll}
                                disabled={removing}
                                className="text-xs text-muted hover:text-warn transition-colors flex items-center gap-1 disabled:opacity-40"
                            >
                                {removing
                                    ? <Loader2 size={12} className="animate-spin" />
                                    : <RotateCcw size={12} />}
                                remove burned-in
                            </button>
                        )}
                    </div>
                    <div className="flex items-center gap-2 [&>button]:flex-1 sm:[&>button]:flex-none">
                        <button onClick={onClose} className="btn-quiet py-2 px-4 text-sm">cancel</button>
                        <button
                            onClick={handleSave}
                            disabled={!items.length || saving}
                            className="btn-primary py-2 px-4 text-sm disabled:opacity-40"
                        >
                            {saving ? <Loader2 size={16} className="animate-spin" /> : null}
                            {saving ? 'burning in…' : 'apply annotations'}
                        </button>
                    </div>
                </div>
                <div className="sm:hidden safe-bottom" />
            </div>
        </div>
    );
}


// One shape drawn over the frame.
//
// Sized as a fraction of frame WIDTH and rotated about its own centre, both to
// match render_shape. The box is then placed so the ROTATED anchor lands on the
// target: rotating about the centre moves the anchor, and it is the anchor that
// has to be on the thing being pointed at.
function ShapeOverlay({ annotation, track, time, selected }) {
    const [ax, ay] = anchorPointAt(annotation, track, time);
    const anchor = anchorOf(annotation.type);

    // Where the anchor ends up once the shape is turned. Rotation is about the
    // centre, so the anchor swings with it.
    const [rax, ray] = rotatePoint(anchor, [0.5, 0.5], annotation.rotation);

    // CSS rotate turns clockwise in y-down coordinates; PIL's rotate turns the
    // picture the other way, so the sign flips here.
    const cssRotation = -annotation.rotation;

    return (
        <div
            className="absolute pointer-events-none"
            style={{
                // left/top are percentages of the FRAME, so they put the box's
                // corner on the target point…
                left: `${ax * 100}%`,
                top: `${ay * 100}%`,
                // …and a percentage translate is relative to the ELEMENT, so it
                // pulls the box back by where its own anchor sits. That is the
                // one bit of CSS that can mix the two units correctly.
                transform: `translate(${-rax * 100}%, ${-ray * 100}%)`,
                // Size is a fraction of frame WIDTH, matching render_shape, and
                // aspect-ratio keeps the box square in pixels — a percentage
                // height would be a fraction of the frame's height instead.
                width: `${annotation.size * 100}%`,
                aspectRatio: '1 / 1',
            }}
        >
            <ShapeSvg
                type={annotation.type}
                color={annotation.color}
                thickness={annotation.thickness}
                rotation={cssRotation}
                selected={selected}
            />
        </div>
    );
}


// The shape itself, in a 0..100 box. Proportions copied from annotations.py so
// the preview and the burn cannot drift apart.
function ShapeSvg({ type, color, thickness, rotation, selected }) {
    const stroke = thickness * 100;
    const content = type === 'arrow'
        ? (() => {
            const headW = ARROW_HEAD_W * 100;
            const headH = ARROW_HEAD_H * 100;
            const tipY = ARROW_TIP_Y * 100;
            const headTop = tipY - headH;
            const shaftW = Math.max(stroke, ARROW_SHAFT_MIN * 100);
            return (
                <>
                    <polygon
                        points={`${50 - headW / 2},${headTop} ${50 + headW / 2},${headTop} 50,${tipY}`}
                        fill={color}
                    />
                    <rect
                        x={50 - shaftW / 2}
                        y={ARROW_TAIL_Y * 100}
                        width={shaftW}
                        height={headTop - ARROW_TAIL_Y * 100 + 1}
                        fill={color}
                    />
                </>
            );
        })()
        : (
            <circle
                cx={50}
                cy={50}
                r={(100 - stroke) / 2}
                fill="none"
                stroke={color}
                strokeWidth={stroke}
            />
        );

    return (
        <svg
            viewBox="0 0 100 100"
            className="w-full h-full overflow-visible"
            style={{
                transform: `rotate(${rotation}deg)`,
                transformOrigin: 'center',
                filter: selected ? 'drop-shadow(0 0 3px rgba(255,255,255,0.9))' : undefined,
            }}
            aria-hidden="true"
        >
            {content}
        </svg>
    );
}


// A small cross on the keypoint a shape is following, so the offset being
// dragged is visible rather than inferred.
function KeypointMarker({ point }) {
    if (!point) {
        return (
            <span className="absolute top-1 left-1 text-[10px] px-1 rounded bg-warn text-paper lowercase pointer-events-none">
                no face here
            </span>
        );
    }
    return (
        <div
            className="absolute pointer-events-none"
            style={{
                left: `${point[0] * 100}%`,
                top: `${point[1] * 100}%`,
                transform: 'translate(-50%, -50%)',
            }}
        >
            <Crosshair size={16} className="text-white drop-shadow" />
        </div>
    );
}


// Controls for the selected shape. Times are set from the playhead rather than
// typed, since the playhead is the thing that was just watched.
function ShapeControls({ annotation, time, track, trackState, trackError,
                         onChange, onSeek, onEnableTrack, onDisableTrack }) {
    const a = annotation;
    const keypoints = track?.keypoints || Object.keys(KEYPOINT_LABELS);

    return (
        <div className="border border-rule rounded-input p-3 space-y-3 bg-paper2">
            {/* timing */}
            <div className="space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] text-muted lowercase">in / out</span>
                    <span className="readout text-[11px] text-ink2 tabular-nums">
                        {fmt(a.start)}–{fmt(a.end)}
                    </span>
                </div>
                <div className="flex items-center gap-2">
                    <button
                        onClick={() => onChange({ start: Math.min(time, a.end - 0.1) })}
                        className="btn-quiet flex-1 py-1.5 text-[11px]"
                        title="start this annotation at the playhead"
                    >
                        in here
                    </button>
                    <button
                        onClick={() => onChange({ end: Math.max(time, a.start + 0.1) })}
                        className="btn-quiet flex-1 py-1.5 text-[11px]"
                        title="end this annotation at the playhead"
                    >
                        out here
                    </button>
                    <button
                        onClick={() => onSeek(a.start)}
                        className="btn-quiet py-1.5 px-2 text-[11px]"
                        title="jump to where it starts"
                    >
                        <Play size={11} />
                    </button>
                </div>
            </div>

            <Slider
                label="size"
                value={a.size}
                min={SIZE_MIN}
                max={SIZE_MAX}
                step={0.01}
                format={(v) => `${Math.round(v * 100)}% of width`}
                onChange={(v) => onChange({ size: v })}
            />

            <Slider
                label="thickness"
                value={a.thickness}
                min={THICK_MIN}
                max={THICK_MAX}
                step={0.005}
                format={(v) => v.toFixed(3)}
                onChange={(v) => onChange({ thickness: v })}
            />

            {/* Rotation only means something for an arrow: a ring looks the same
                whichever way up it is. */}
            {a.type === 'arrow' && (
                <Slider
                    label="direction"
                    value={a.rotation}
                    min={-180}
                    max={180}
                    step={5}
                    format={(v) => `${Math.round(v)}°`}
                    onChange={(v) => onChange({ rotation: v })}
                />
            )}

            {/* colour */}
            <div className="space-y-1.5">
                <span className="text-[11px] text-muted lowercase">colour</span>
                <div className="flex items-center gap-1.5 flex-wrap">
                    {SWATCHES.map((hex) => (
                        <button
                            key={hex}
                            onClick={() => onChange({ color: hex })}
                            className={`w-6 h-6 rounded-full border-2 transition-transform ${
                                a.color.toUpperCase() === hex ? 'border-ink scale-110' : 'border-rule'}`}
                            style={{ backgroundColor: hex }}
                            title={hex}
                            aria-label={`colour ${hex}`}
                        />
                    ))}
                    <input
                        type="color"
                        value={a.color.slice(0, 7)}
                        onChange={(e) => onChange({ color: e.target.value.toUpperCase() })}
                        className="w-6 h-6 rounded-full bg-transparent border border-rule cursor-pointer"
                        aria-label="custom colour"
                    />
                </div>
            </div>

            {/* placement mode */}
            <div className="space-y-1.5 pt-1 border-t border-rule">
                <span className="text-[11px] text-muted lowercase">placement</span>
                <div className="flex items-center gap-2">
                    <button
                        onClick={onDisableTrack}
                        className={`flex-1 py-1.5 text-[11px] rounded-input border transition-colors ${
                            !a.track ? 'border-brass text-brass' : 'border-rule text-muted hover:text-ink2'}`}
                    >
                        fixed
                    </button>
                    <button
                        onClick={() => onEnableTrack(a.track || 'mouth')}
                        disabled={trackState === 'loading'}
                        className={`flex-1 py-1.5 text-[11px] rounded-input border transition-colors flex items-center justify-center gap-1 disabled:opacity-50 ${
                            a.track ? 'border-brass text-brass' : 'border-rule text-muted hover:text-ink2'}`}
                    >
                        {trackState === 'loading'
                            ? <Loader2 size={11} className="animate-spin" />
                            : <Crosshair size={11} />}
                        {trackState === 'loading' ? 'finding…' : 'follow'}
                    </button>
                </div>

                {a.track && trackState === 'ready' && (
                    <>
                        <select
                            value={a.track}
                            onChange={(e) => onEnableTrack(e.target.value)}
                            className="w-full text-[11px] bg-paper border border-rule rounded-input px-2 py-1.5 text-ink2"
                            aria-label="keypoint to follow"
                        >
                            {keypoints.map((k) => (
                                <option key={k} value={k}>{KEYPOINT_LABELS[k] || k}</option>
                            ))}
                        </select>
                        {typeof track?.coverage === 'number' && (
                            <p className="text-[10px] text-muted leading-snug">
                                a face was found in {Math.round(track.coverage * 100)}% of
                                this clip. Where it was not, the shape holds its last
                                position.
                            </p>
                        )}
                    </>
                )}

                {trackState === 'failed' && (
                    <p className="text-[10px] text-warn leading-snug">{trackError}</p>
                )}

                <p className="text-[10px] text-muted leading-snug">
                    {a.track
                        ? 'drag on the frame to nudge it away from the face'
                        : 'drag on the frame to move it'}
                </p>
            </div>

            {/* Nudge buttons: a drag cannot reliably hit a single pixel on a
                phone, and small corrections are the common case. */}
            <Nudger annotation={a} onChange={onChange} />
        </div>
    );
}


function Nudger({ annotation, onChange }) {
    const step = 0.01;
    const move = (dx, dy) => {
        if (annotation.track) {
            const [ox, oy] = annotation.offset || [0, 0];
            onChange({ offset: [ox + dx, oy + dy] });
        } else {
            onChange({
                x: clamp(annotation.x + dx, 0, 1),
                y: clamp(annotation.y + dy, 0, 1),
            });
        }
    };
    const btn = 'w-7 h-7 rounded-input border border-rule text-muted hover:text-brass hover:border-brass transition-colors text-[11px] leading-none';
    return (
        <div className="flex items-center gap-2">
            <span className="text-[11px] text-muted lowercase">nudge</span>
            <div className="flex items-center gap-1 ml-auto">
                <button className={btn} onClick={() => move(-step, 0)} aria-label="nudge left">←</button>
                <button className={btn} onClick={() => move(0, -step)} aria-label="nudge up">↑</button>
                <button className={btn} onClick={() => move(0, step)} aria-label="nudge down">↓</button>
                <button className={btn} onClick={() => move(step, 0)} aria-label="nudge right">→</button>
            </div>
        </div>
    );
}


function Slider({ label, value, min, max, step, format, onChange }) {
    return (
        <div className="space-y-1">
            <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] text-muted lowercase">{label}</span>
                <span className="readout text-[11px] text-ink2 tabular-nums">{format(value)}</span>
            </div>
            <input
                type="range"
                min={min}
                max={max}
                step={step}
                value={value}
                onChange={(e) => onChange(Number(e.target.value))}
                className="w-full accent-brass"
                aria-label={label}
            />
        </div>
    );
}
