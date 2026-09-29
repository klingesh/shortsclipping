import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { X, Loader2, AlertCircle, AtSign, RotateCcw, Grid3x3, Move } from 'lucide-react';
import { getApiUrl } from '../config';
import { apiJson } from '../lib/api';

// The creator's own handle, placed on the clip.
//
// Nothing here is the OpenShorts free-plan mark — that one is burned by the
// pipeline at a position chosen to be expensive to crop, and WatermarkModal is
// the upsell notice about it. This is the user's name, so it has to be movable,
// restylable and removable.
//
// The preview draws the handle as HTML text over the real clip rather than
// fetching a rendered PNG, which keeps it instant while a slider moves. That
// means the browser and Pillow are laying out the same text independently, so the
// preview is close but not pixel-exact — the font, the size fraction and the
// anchor arithmetic all match, but glyph metrics and the outline will differ
// slightly. Said plainly in the UI rather than implied to be a final render.

// Mirrors username_mark.ANCHORS, in reading order so the 3x3 grid maps directly.
const ANCHOR_GRID = [
    ['top_left', 'top_center', 'top_right'],
    ['middle_left', 'center', 'middle_right'],
    ['bottom_left', 'bottom_center', 'bottom_right'],
];

const SWATCHES = ['#00E5FF', '#FFFFFF', '#FFD400', '#FF2D2D', '#39FF14', '#000000'];

// Fallbacks used only until GET /api/watermark/options answers. Kept minimal on
// purpose: the server owns these values, and duplicating the full set here is
// how the two drift apart.
const FALLBACK = {
    anchors: ANCHOR_GRID.flat(),
    default_anchor: 'bottom_left',
    fonts: [{ family: 'Anton', label: 'Anton', group: 'display' }],
    defaults: {
        size: 0.045, color: '#00E5FF', opacity: 0.9, outline_width: 0.12,
        outline_color: '#000000', margin: 0.05, font: 'Anton',
    },
    limits: {
        size: [0.01, 0.30], opacity: [0.05, 1.0], outline_width: [0.0, 0.5],
        margin: [0.0, 0.45], text_length: 40,
    },
};

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export default function UsernameMarkModal({ jobId, clipIndex, clipTitle, videoUrl,
                                            existing, onClose, onMarked }) {
    const frameRef = useRef(null);
    const videoRef = useRef(null);

    const [options, setOptions] = useState(FALLBACK);
    const [loadingOptions, setLoadingOptions] = useState(true);

    // Reopening shows what is already burned in, so restyling starts from the
    // current look rather than from the defaults.
    const [mark, setMark] = useState(() => ({
        text: existing?.text || '',
        font: existing?.font || FALLBACK.defaults.font,
        size: existing?.size ?? FALLBACK.defaults.size,
        color: existing?.color || FALLBACK.defaults.color,
        opacity: existing?.opacity ?? FALLBACK.defaults.opacity,
        outline_width: existing?.outline_width ?? FALLBACK.defaults.outline_width,
        outline_color: existing?.outline_color || FALLBACK.defaults.outline_color,
        anchor: existing?.anchor || FALLBACK.default_anchor,
        margin: existing?.margin ?? FALLBACK.defaults.margin,
        x: existing?.x ?? null,
        y: existing?.y ?? null,
    }));

    const [dragging, setDragging] = useState(false);
    const [saving, setSaving] = useState(false);
    const [removing, setRemoving] = useState(false);
    const [error, setError] = useState(null);

    const set = useCallback((patch) => setMark((m) => ({ ...m, ...patch })), []);

    useEffect(() => {
        let alive = true;
        (async () => {
            try {
                const res = await apiJson('/api/watermark/options');
                if (!alive) return;
                setOptions(res);
                // Only fill in what the user has not already got a value for, so
                // reopening an existing mark does not get reset by the defaults
                // arriving a moment later.
                setMark((m) => ({
                    ...m,
                    font: existing?.font || m.font || res.defaults.font,
                    anchor: existing?.anchor || m.anchor || res.default_anchor,
                }));
            } catch {
                // The fallbacks are usable; a failed options fetch should not
                // block someone from typing their name.
            } finally {
                if (alive) setLoadingOptions(false);
            }
        })();
        return () => { alive = false; };
    }, [existing]);

    const limits = options.limits || FALLBACK.limits;
    const freePlacement = mark.x !== null && mark.y !== null;

    // Dragging switches to free placement, since a dragged point is not an
    // anchor. The anchor is remembered, so "snap back" still works.
    const placeAt = useCallback((clientX, clientY) => {
        const el = frameRef.current;
        if (!el) return;
        const rect = el.getBoundingClientRect();
        set({
            x: clamp((clientX - rect.left) / rect.width, 0, 1),
            y: clamp((clientY - rect.top) / rect.height, 0, 1),
        });
    }, [set]);

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

    // Where the preview text sits, as CSS. Mirrors username_mark.mark_position:
    // an anchor puts the mark's EDGE against the frame's, inset by the margin;
    // free x/y centres it on the point.
    //
    // The inset is in cqw — 1% of the frame's own width — because the backend's
    // margin is a fraction of WIDTH on BOTH axes. A percentage `top` would be a
    // fraction of HEIGHT instead, which on a 9:16 frame makes the vertical inset
    // nearly twice the horizontal one and the preview a lie. cqw needs
    // container-type on the frame, which is set where the frame is declared.
    const placement = useMemo(() => {
        if (freePlacement) {
            return {
                left: `${mark.x * 100}%`,
                top: `${mark.y * 100}%`,
                transform: 'translate(-50%, -50%)',
            };
        }
        // "center" is the one anchor without an underscore.
        const parts = mark.anchor.split('_');
        const vertical = parts.length === 2 ? parts[0] : 'middle';
        const horizontal = parts.length === 2 ? parts[1] : 'center';
        const inset = `${mark.margin * 100}cqw`;

        const style = {};
        const shift = [];
        if (horizontal === 'left') style.left = inset;
        else if (horizontal === 'right') style.right = inset;
        else { style.left = '50%'; shift.push('translateX(-50%)'); }

        if (vertical === 'top') style.top = inset;
        else if (vertical === 'bottom') style.bottom = inset;
        else { style.top = '50%'; shift.push('translateY(-50%)'); }

        return { ...style, transform: shift.join(' ') || undefined };
    }, [freePlacement, mark.x, mark.y, mark.anchor, mark.margin]);

    const canSave = mark.text.trim().length > 0 && !saving;

    const handleSave = async () => {
        if (!canSave) return;
        setSaving(true);
        setError(null);
        try {
            const payload = {
                job_id: jobId,
                clip_index: clipIndex,
                text: mark.text.trim(),
                font: mark.font,
                size: Number(mark.size.toFixed(4)),
                color: mark.color,
                opacity: Number(mark.opacity.toFixed(3)),
                outline_width: Number(mark.outline_width.toFixed(3)),
                outline_color: mark.outline_color,
                anchor: mark.anchor,
                margin: Number(mark.margin.toFixed(4)),
            };
            // Sent only when free placement is on. Sending them as null would be
            // fine, but sending 0 would silently move the mark to the corner, so
            // they are omitted rather than defaulted.
            if (freePlacement) {
                payload.x = Number(mark.x.toFixed(4));
                payload.y = Number(mark.y.toFixed(4));
            }
            const res = await apiJson('/api/watermark', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (onMarked) onMarked(clipIndex, res);
            onClose();
        } catch (e) {
            setError(e?.detail || e?.message || 'Burning the watermark failed.');
        } finally {
            setSaving(false);
        }
    };

    const handleRemove = async () => {
        if (removing) return;
        setRemoving(true);
        setError(null);
        try {
            const res = await apiJson('/api/watermark/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_id: jobId, clip_index: clipIndex }),
            });
            if (onMarked) onMarked(clipIndex, { ...res, watermark: null });
            onClose();
        } catch (e) {
            setError(e?.detail || e?.message || 'Could not remove the watermark.');
        } finally {
            setRemoving(false);
        }
    };

    return (
        <div className="fixed inset-0 z-50 bg-black/80 flex items-end sm:items-center justify-center p-0 sm:p-4">
            <div className="card w-full max-w-3xl max-h-[92vh] sm:max-h-[90vh] flex flex-col rounded-b-none sm:rounded-card animate-sheet-up sm:animate-none">
                <div className="flex items-center justify-between p-4 border-b border-rule">
                    <div className="flex items-center gap-2.5 min-w-0">
                        <AtSign size={18} className="text-brass shrink-0" />
                        <div className="min-w-0">
                            <h2 className="text-base font-medium text-ink lowercase truncate">your handle</h2>
                            {clipTitle && <p className="text-xs text-muted truncate">{clipTitle}</p>}
                        </div>
                    </div>
                    <button onClick={onClose} className="p-1.5 hover:bg-paper3 rounded-input transition-colors">
                        <X size={18} className="text-muted" />
                    </button>
                </div>

                <div className="flex-1 overflow-y-auto overscroll-contain custom-scrollbar p-4">
                    {error && (
                        <div className="flex items-start gap-2 text-xs text-warn border border-warn/40 rounded-input p-3 mb-4">
                            <AlertCircle size={14} className="shrink-0 mt-0.5" />
                            <span>{error}</span>
                        </div>
                    )}

                    <div className="flex flex-col sm:flex-row gap-4">
                        {/* preview */}
                        <div className="sm:w-1/2 shrink-0">
                            <div
                                ref={frameRef}
                                onMouseDown={(e) => { setDragging(true); placeAt(e.clientX, e.clientY); }}
                                onTouchStart={(e) => {
                                    setDragging(true);
                                    const p = e.touches[0];
                                    placeAt(p.clientX, p.clientY);
                                }}
                                className="relative overflow-hidden rounded-input select-none bg-black border border-brass cursor-crosshair"
                                // Makes 1cqw mean 1% of THIS box's width, which
                                // is what every size and inset below is a
                                // fraction of. inline-size is enough for cqw and
                                // is cheaper than full size containment.
                                style={{ containerType: 'inline-size' }}
                            >
                                <video
                                    ref={videoRef}
                                    src={getApiUrl(videoUrl)}
                                    playsInline
                                    muted
                                    preload="metadata"
                                    className="w-full block max-h-[46vh] mx-auto pointer-events-none"
                                />
                                {mark.text.trim() && (
                                    <span
                                        className="absolute whitespace-nowrap pointer-events-none leading-none"
                                        style={{
                                            ...placement,
                                            // A fraction of frame WIDTH, matching
                                            // render_mark.
                                            fontSize: `${mark.size * 100}cqw`,
                                            fontFamily: `'${mark.font}', sans-serif`,
                                            color: mark.color,
                                            opacity: mark.opacity,
                                            // Pillow's stroke_width grows OUTWARD
                                            // from the glyph; -webkit-text-stroke
                                            // is centred on it, so half would be
                                            // hidden inside. Doubled to match the
                                            // weight actually visible outside.
                                            WebkitTextStrokeWidth: mark.outline_width > 0
                                                ? `${mark.outline_width * mark.size * 200}cqw`
                                                : undefined,
                                            WebkitTextStrokeColor: mark.outline_color,
                                            paintOrder: 'stroke fill',
                                        }}
                                    >
                                        {mark.text.trim()}
                                    </span>
                                )}
                            </div>

                            <p className="text-[10px] text-muted leading-snug mt-2">
                                A close preview, not the final render — the browser
                                and the export lay the text out separately, so the
                                outline and spacing shift a little when it burns in.
                            </p>

                            <div className="flex items-center gap-2 mt-3">
                                <button
                                    onClick={() => set({ x: null, y: null })}
                                    disabled={!freePlacement}
                                    className={`btn-quiet flex-1 py-1.5 text-[11px] flex items-center justify-center gap-1.5 disabled:opacity-40 ${
                                        !freePlacement ? 'text-brass' : ''}`}
                                    title="snap back to a corner"
                                >
                                    <Grid3x3 size={12} /> snap to a corner
                                </button>
                                <span className={`text-[11px] flex items-center gap-1 ${
                                    freePlacement ? 'text-brass' : 'text-muted'}`}>
                                    <Move size={12} /> {freePlacement ? 'placed by hand' : 'anchored'}
                                </span>
                            </div>
                        </div>

                        {/* controls */}
                        <div className="sm:w-1/2 min-w-0 space-y-3">
                            <div className="space-y-1">
                                <label htmlFor="wm-text" className="text-[11px] text-muted lowercase">
                                    handle
                                </label>
                                <input
                                    id="wm-text"
                                    type="text"
                                    value={mark.text}
                                    maxLength={limits.text_length}
                                    onChange={(e) => set({ text: e.target.value })}
                                    placeholder="@yourchannel"
                                    className="w-full bg-paper border border-rule rounded-input px-2.5 py-2 text-sm text-ink placeholder:text-muted focus:border-brass outline-none"
                                />
                                <p className="text-[10px] text-muted">
                                    {mark.text.length}/{limits.text_length} · one line
                                </p>
                            </div>

                            <div className="space-y-1">
                                <label htmlFor="wm-font" className="text-[11px] text-muted lowercase">
                                    font
                                </label>
                                <select
                                    id="wm-font"
                                    value={mark.font}
                                    onChange={(e) => set({ font: e.target.value })}
                                    disabled={loadingOptions}
                                    className="w-full text-xs bg-paper border border-rule rounded-input px-2 py-2 text-ink2 disabled:opacity-50"
                                >
                                    {(options.fonts || []).map((f) => (
                                        <option key={f.family} value={f.family}>{f.label}</option>
                                    ))}
                                </select>
                            </div>

                            <Slider
                                label="size"
                                value={mark.size}
                                min={limits.size[0]}
                                max={limits.size[1]}
                                step={0.005}
                                format={(v) => `${(v * 100).toFixed(1)}% of width`}
                                onChange={(v) => set({ size: v })}
                            />

                            <Slider
                                label="opacity"
                                value={mark.opacity}
                                min={limits.opacity[0]}
                                max={limits.opacity[1]}
                                step={0.05}
                                format={(v) => `${Math.round(v * 100)}%`}
                                onChange={(v) => set({ opacity: v })}
                            />

                            <Slider
                                label="outline"
                                value={mark.outline_width}
                                min={limits.outline_width[0]}
                                max={limits.outline_width[1]}
                                step={0.01}
                                format={(v) => (v === 0 ? 'none' : v.toFixed(2))}
                                onChange={(v) => set({ outline_width: v })}
                            />

                            {!freePlacement && (
                                <Slider
                                    label="inset from the edge"
                                    value={mark.margin}
                                    min={limits.margin[0]}
                                    max={limits.margin[1]}
                                    step={0.01}
                                    format={(v) => `${(v * 100).toFixed(0)}% of width`}
                                    onChange={(v) => set({ margin: v })}
                                />
                            )}

                            <div className="space-y-1.5">
                                <span className="text-[11px] text-muted lowercase">colour</span>
                                <div className="flex items-center gap-1.5 flex-wrap">
                                    {SWATCHES.map((hex) => (
                                        <button
                                            key={hex}
                                            onClick={() => set({ color: hex })}
                                            className={`w-6 h-6 rounded-full border-2 transition-transform ${
                                                mark.color.toUpperCase() === hex ? 'border-ink scale-110' : 'border-rule'}`}
                                            style={{ backgroundColor: hex }}
                                            title={hex}
                                            aria-label={`colour ${hex}`}
                                        />
                                    ))}
                                    <input
                                        type="color"
                                        value={mark.color.slice(0, 7)}
                                        onChange={(e) => set({ color: e.target.value.toUpperCase() })}
                                        className="w-6 h-6 rounded-full bg-transparent border border-rule cursor-pointer"
                                        aria-label="custom colour"
                                    />
                                </div>
                            </div>

                            <div className="space-y-1.5 pt-1 border-t border-rule">
                                <span className="text-[11px] text-muted lowercase">corner</span>
                                <div className="grid grid-cols-3 gap-1 w-24">
                                    {ANCHOR_GRID.flat().map((anchor) => (
                                        <button
                                            key={anchor}
                                            onClick={() => set({ anchor, x: null, y: null })}
                                            className={`h-7 rounded-input border transition-colors ${
                                                !freePlacement && mark.anchor === anchor
                                                    ? 'border-brass bg-brass/20'
                                                    : 'border-rule hover:border-ink2'}`}
                                            title={anchor.replace('_', ' ')}
                                            aria-label={anchor.replace('_', ' ')}
                                        />
                                    ))}
                                </div>
                                <p className="text-[10px] text-muted leading-snug">
                                    or drag on the frame to put it anywhere
                                </p>
                            </div>
                        </div>
                    </div>
                </div>

                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 border-t border-rule">
                    {existing?.text ? (
                        <button
                            onClick={handleRemove}
                            disabled={removing}
                            className="text-xs text-muted hover:text-warn transition-colors flex items-center gap-1 disabled:opacity-40"
                        >
                            {removing ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />}
                            remove the handle
                        </button>
                    ) : <span className="text-xs text-muted">not on the clip yet</span>}
                    <div className="flex items-center gap-2 [&>button]:flex-1 sm:[&>button]:flex-none">
                        <button onClick={onClose} className="btn-quiet py-2 px-4 text-sm">cancel</button>
                        <button
                            onClick={handleSave}
                            disabled={!canSave}
                            className="btn-primary py-2 px-4 text-sm disabled:opacity-40"
                        >
                            {saving ? <Loader2 size={16} className="animate-spin" /> : null}
                            {saving ? 'burning in…' : 'apply handle'}
                        </button>
                    </div>
                </div>
                <div className="sm:hidden safe-bottom" />
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
