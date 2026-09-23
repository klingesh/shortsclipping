import React, { useState, useEffect } from 'react';
import { Loader2 } from 'lucide-react';
import { apiFetch } from '../lib/api';
import RemotionPreview from './RemotionPreview';
import Modal from './ui/Modal';
import SegmentedControl from './ui/SegmentedControl';

// Fonts come from GET /api/caption-presets alongside the presets. As a hardcoded
// array here, nothing could check that an offered family was one the image can
// actually resolve — libass falls back to DejaVu silently (issue #57), so a bad
// entry would look right in this preview and wrong in the export. The server now
// owns the list and a test asserts every entry resolves.
const FONT_FALLBACK = [{ value: 'Anton', label: 'Anton' }];

const COLOR_PRESETS = [
    { color: '#FFFFFF', label: 'White' },
    { color: '#FFFF00', label: 'Yellow' },
    { color: '#00FFFF', label: 'Cyan' },
    { color: '#00FF00', label: 'Green' },
    { color: '#FF0000', label: 'Red' },
    { color: '#FF69B4', label: 'Pink' },
];

const HIGHLIGHT_PRESETS = [
    { color: '#FFDD00', label: 'Gold' },
    { color: '#FF4444', label: 'Red' },
    { color: '#00FF88', label: 'Green' },
    { color: '#00BBFF', label: 'Blue' },
    { color: '#FF69B4', label: 'Pink' },
];

const ANIMATION_OPTIONS = [
    { value: 'pop', label: 'Pop' },
    { value: 'word-highlight', label: 'Glow' },
    { value: 'karaoke', label: 'Karaoke' },
    { value: 'none', label: 'None' },
];

const POSITION_OPTIONS = [
    { value: 'top', label: 'top' },
    { value: 'middle', label: 'middle' },
    { value: 'bottom', label: 'bottom' },
];

// Caption presets are fetched from GET /api/caption-presets, which serves the
// same table caption_presets.py burns from. They used to be hardcoded here, and
// the copy could disagree with the server — the preview then advertised a look
// the export never produced.
const PREVIEW_HEIGHT = 1920;

// The ASS generator measures font size in PlayResY=288 units and then scales by
// 0.85 (subtitles.generate_ass). The Remotion preview measures in CSS pixels on
// a 1080x1920 canvas. Converting between them is the only way the preview can
// show the size that actually gets burned: the previous hardcoded `* 2.2` fudge
// rendered the default 44 at ~53px instead of ~249px, roughly five times small.
const ASS_PLAY_RES_Y = 288;
const ASS_FONTSIZE_SCALE = 0.85;

// generate_ass clamps fontsize to 10..200; staying inside that range means the
// slider can never ask for a size the burn will quietly change.
const ASS_FONTSIZE_MIN = 16;
const ASS_FONTSIZE_MAX = 90;

// MarginV is clamped 0..200 by both generators. 220 measured as the point where
// a bottom-anchored caption reaches the upper third, so 200 is ample.
const ASS_MARGIN_MIN = 0;
const ASS_MARGIN_MAX = 200;

const assSizeToPreviewPx = (assFontSize, canvasHeight = PREVIEW_HEIGHT) =>
    Math.round((assFontSize * ASS_FONTSIZE_SCALE / ASS_PLAY_RES_Y) * canvasHeight);

// Where the caption sits in the preview, as a fraction of frame height, so the
// Remotion overlay lands where libass will put it. MarginV measures to the text
// edge nearest the anchor, not its centre.
const marginToPreviewFraction = (marginV, anchor) => {
    const offset = marginV / ASS_PLAY_RES_Y;
    if (anchor === 'top') return offset;
    return 1 - offset;
};

const swatchClass = (selected) =>
    `w-6 h-6 rounded-full transition-all ${selected
        ? 'ring-2 ring-[color:var(--color-accent)] ring-offset-2 ring-offset-[color:var(--color-paper-2)]'
        : 'ring-1 ring-[color:var(--color-rule-2)] hover:ring-[color:var(--color-accent)]'}`;

export default function SubtitleModal({ isOpen, onClose, onGenerate, onApplyAll, onRemove, isProcessing, videoUrl, jobId, clipIndex, existingHook, bulkCount = 0, bulkProgress }) {
    const [position, setPosition] = useState('bottom');
    // ASS units (PlayResY=288), matching what the server burns — not pixels.
    // Was a frozen useState(24) with no setter, so a preset's size was ignored.
    const [fontSize, setFontSize] = useState(44);
    // Offset from the anchored edge, also in ASS units. 43 = SAFE_MARGIN_V.
    const [marginV, setMarginV] = useState(43);
    const [fontName, setFontName] = useState('Verdana');
    const [fontColor, setFontColor] = useState('#FFFFFF');
    const [highlightColor, setHighlightColor] = useState('#FFDD00');
    const [borderColor, setBorderColor] = useState('#000000');
    const [borderWidth, setBorderWidth] = useState(2);
    const [bgColor, setBgColor] = useState('#000000');
    const [bgOpacity, setBgOpacity] = useState(0.0);
    const [animation, setAnimation] = useState('pop');
    const [showTextEditor, setShowTextEditor] = useState(false);

    // Karaoke (server-side ASS burn) state
    const [style, setStyle] = useState('classic'); // classic | karaoke
    const [effect, setEffect] = useState('none'); // none | glow | pop | box
    const [baseOpacity, setBaseOpacity] = useState(1.0);
    const [uppercase, setUppercase] = useState(false);
    const [activePreset, setActivePreset] = useState(null);
    const [presets, setPresets] = useState([]);
    const [fontOptions, setFontOptions] = useState(FONT_FALLBACK);

    // One fetch per mount. The server owns both tables; this component only
    // renders them and mirrors the chosen preset into the individual controls so
    // the user can keep nudging from there.
    useEffect(() => {
        let cancelled = false;
        apiFetch('/api/caption-presets')
            .then((res) => (res.ok ? res.json() : null))
            .then((data) => {
                if (cancelled || !data) return;
                if (Array.isArray(data.presets)) setPresets(data.presets);
                if (Array.isArray(data.fonts) && data.fonts.length > 0) {
                    setFontOptions(data.fonts.map((f) => ({
                        value: f.family, label: f.label || f.family, group: f.group,
                    })));
                }
            })
            .catch(() => { /* preset grid stays empty; manual controls still work */ });
        return () => { cancelled = true; };
    }, []);

    // `p` is a server entry: { id, label, group, style: { ...snake_case } }.
    const applyPreset = (p) => {
        const s = p.style || {};
        setActivePreset(p.id);
        setStyle(s.style);
        setEffect(s.effect);
        setPosition(s.alignment);
        setFontName(s.font_name);
        setFontSize(s.font_size);
        setFontColor(s.font_color);
        setHighlightColor(s.highlight_color);
        setBorderColor(s.border_color);
        setBorderWidth(s.border_width);
        setBgColor(s.bg_color);
        setBgOpacity(s.bg_opacity);
        setBaseOpacity(s.base_opacity);
        setUppercase(s.uppercase);
        if (typeof s.margin_v === 'number') setMarginV(s.margin_v);
        // Keep the Remotion preview roughly in sync with the burned look
        setAnimation(s.style === 'karaoke' ? (s.effect === 'pop' ? 'pop' : s.effect === 'glow' ? 'word-highlight' : 'karaoke') : 'none');
    };

    // Remotion preview state
    const [captions, setCaptions] = useState([]);
    const [originalCaptions, setOriginalCaptions] = useState([]);
    const [editableText, setEditableText] = useState('');
    const [durationSec, setDurationSec] = useState(30);
    const [captionsLoading, setCaptionsLoading] = useState(false);
    const [useRemotionPreview, setUseRemotionPreview] = useState(false);

    // Fetch word-level captions when modal opens
    useEffect(() => {
        if (!isOpen || !jobId || clipIndex === undefined) return;

        setCaptionsLoading(true);
        apiFetch(`/api/clip/${jobId}/${clipIndex}/transcript`)
            .then((res) => res.ok ? res.json() : null)
            .then((data) => {
                if (data && data.captions && data.captions.length > 0) {
                    setCaptions(data.captions);
                    setOriginalCaptions(data.captions);
                    setEditableText(data.captions.map(c => c.text).join(' '));
                    setDurationSec(data.durationSec || 30);
                    setUseRemotionPreview(true);
                } else {
                    setUseRemotionPreview(false);
                }
            })
            .catch(() => setUseRemotionPreview(false))
            .finally(() => setCaptionsLoading(false));
    }, [isOpen, jobId, clipIndex]);

    // When user edits text, redistribute words across original timestamps
    const handleTextEdit = (newText) => {
        setEditableText(newText);
        const newWords = newText.split(/\s+/).filter(w => w.length > 0);
        if (newWords.length === 0 || originalCaptions.length === 0) {
            setCaptions([]);
            return;
        }

        // Distribute new words across the time span of original captions
        const totalDurationMs = originalCaptions[originalCaptions.length - 1].endMs - originalCaptions[0].startMs;
        const startMs = originalCaptions[0].startMs;
        const wordDurationMs = totalDurationMs / newWords.length;

        const newCaptions = newWords.map((word, i) => ({
            text: word,
            startMs: Math.round(startMs + i * wordDurationMs),
            endMs: Math.round(startMs + (i + 1) * wordDurationMs),
        }));
        setCaptions(newCaptions);
    };

    if (!isOpen) return null;

    // Build subtitle config for Remotion
    const subtitleConfig = {
        captions,
        position,
        // Same ASS units the burn uses, so the preview moves with the slider
        // instead of sitting at a fixed percentage.
        marginV,
        style: {
            fontFamily: fontName,
            // Convert ASS units to preview pixels rather than guessing at a
            // multiplier, so the preview matches the burn at every size.
            fontSize: assSizeToPreviewPx(fontSize),
            fontColor,
            highlightColor,
            borderColor,
            borderWidth: borderWidth * 1.5,
            bgColor,
            bgOpacity,
            animation,
            // Karaoke look reflected live in the playable preview.
            baseOpacity: style === 'karaoke' ? baseOpacity : 1,
            uppercase: style === 'karaoke' ? uppercase : false,
        },
    };

    // Fallback: static CSS preview (same as original)
    const bw = Math.max(borderWidth, 0);
    const bc = borderColor;
    const outlineShadow = bw > 0 ? [
        `-${bw}px -${bw}px 0 ${bc}`, `${bw}px -${bw}px 0 ${bc}`,
        `-${bw}px ${bw}px 0 ${bc}`, `${bw}px ${bw}px 0 ${bc}`,
        `0 -${bw}px 0 ${bc}`, `0 ${bw}px 0 ${bc}`,
        `-${bw}px 0 0 ${bc}`, `${bw}px 0 0 ${bc}`,
    ].join(', ') : 'none';

    const fallbackPreviewStyle = {
        fontFamily: fontName,
        color: fontColor,
        fontSize: '20px',
        fontWeight: 'bold',
        maxWidth: '85%',
        padding: '6px 12px',
        borderRadius: '4px',
        textAlign: 'center',
        lineHeight: '1.3',
        ...(bgOpacity > 0
            ? {
                backgroundColor: `${bgColor}${Math.round(bgOpacity * 255).toString(16).padStart(2, '0')}`,
                textShadow: 'none',
            }
            : { textShadow: outlineShadow }
        ),
    };

    return (
        <Modal isOpen={isOpen} onClose={onClose} size="xl" eyebrow="EDITOR · SUBTITLES" title="subtitles">
            <div className="flex flex-col md:flex-row gap-6">
                {/* Left: Preview */}
                <div className="flex-1 flex flex-col items-center justify-center bg-black rounded-card border border-rule overflow-hidden relative aspect-[9/16] max-h-[600px]">
                    {captionsLoading ? (
                        <div className="flex items-center gap-2 text-muted">
                            <Loader2 size={16} className="animate-spin" />
                            <span className="text-sm lowercase">Loading preview...</span>
                        </div>
                    ) : useRemotionPreview ? (
                        <RemotionPreview
                            videoUrl={videoUrl}
                            durationInSeconds={durationSec}
                            subtitles={subtitleConfig}
                            hook={existingHook || null}
                        />
                    ) : (
                        <>
                            <video src={videoUrl} className="w-full h-full object-contain opacity-50" muted playsInline />
                            <div className={`absolute w-full px-8 text-center transition-all duration-300 pointer-events-none flex flex-col items-center justify-center
                                ${position === 'top' ? 'top-20' : ''}
                                ${position === 'middle' ? 'top-0 bottom-0' : ''}
                                ${position === 'bottom' ? 'bottom-20' : ''}
                            `}>
                                <span style={fallbackPreviewStyle}>
                                    This is how your subtitles<br/>will appear on the video
                                </span>
                            </div>
                        </>
                    )}
                </div>

                {/* Right: Controls */}
                <div className="w-full md:w-80 flex flex-col">
                    <div className="space-y-5 flex-1 overflow-y-auto custom-scrollbar pr-1">
                        {/* Caption presets (server-side karaoke burn) */}
                        <div>
                            <p className="eyebrow mb-2">Preset</p>
                            <div className="grid grid-cols-3 gap-1.5">
                                {presets.map((p) => (
                                    <button
                                        key={p.id}
                                        onClick={() => applyPreset(p)}
                                        className={`px-2 py-1.5 rounded-input border text-xs transition-colors flex items-center gap-1.5 justify-center
                                            ${activePreset === p.id
                                                ? 'border-[color:var(--color-accent)] text-ink'
                                                : 'border-rule2 text-muted hover:border-[color:var(--color-accent)]'}`}
                                        title={p.label}
                                    >
                                        {/* Swatch shows the colour the word actually reads as: for the
                                            one-word looks that is font_color, since there is no inactive
                                            text for a highlight to contrast against. */}
                                        <span
                                            className="w-2 h-2 rounded-full shrink-0"
                                            style={{ backgroundColor: p.style?.max_chars === 1 ? p.style?.font_color : p.style?.highlight_color }}
                                        />
                                        {p.label}
                                    </button>
                                ))}
                            </div>
                            {style === 'karaoke' && (
                                <div className="mt-3 space-y-3 animate-fade">
                                    <div className="flex items-center justify-between">
                                        <span className="readout">UPPERCASE</span>
                                        <label className="relative inline-flex items-center cursor-pointer">
                                            <input type="checkbox" checked={uppercase} onChange={(e) => setUppercase(e.target.checked)} className="sr-only peer" />
                                            <div className="w-8 h-4 rounded-full bg-paper3 peer-checked:bg-brass transition-colors after:content-[''] after:absolute after:top-0 after:left-0 after:h-4 after:w-4 after:rounded-full after:bg-ink after:transition-all peer-checked:after:translate-x-full"></div>
                                        </label>
                                    </div>
                                    <div>
                                        <div className="flex justify-between mb-1">
                                            <span className="readout">Dim inactive words</span>
                                            <span className="readout">{Math.round(baseOpacity * 100)}%</span>
                                        </div>
                                        <input
                                            type="range"
                                            min="30"
                                            max="100"
                                            value={Math.round(baseOpacity * 100)}
                                            onChange={(e) => setBaseOpacity(parseInt(e.target.value) / 100)}
                                            className="w-full accent-[var(--color-accent)]"
                                        />
                                    </div>
                                </div>
                            )}
                        </div>

                        {/* Size. Stored in ASS units to match the burn; shown as
                            a share of frame height, which is the only number
                            that means anything across resolutions. */}
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <p className="eyebrow">Size</p>
                                <span className="readout">
                                    {(assSizeToPreviewPx(fontSize) / PREVIEW_HEIGHT * 100).toFixed(1)}% of height
                                </span>
                            </div>
                            <input
                                type="range"
                                min={ASS_FONTSIZE_MIN}
                                max={ASS_FONTSIZE_MAX}
                                step={1}
                                value={fontSize}
                                onChange={(e) => setFontSize(Number(e.target.value))}
                                className="w-full"
                                aria-label="Caption size"
                            />
                        </div>

                        {/* Position Selector */}
                        <div>
                            <p className="eyebrow mb-2">Position</p>
                            <SegmentedControl
                                options={POSITION_OPTIONS}
                                value={position}
                                onChange={setPosition}
                                size="sm"
                            />
                            {/* Free-form offset from whichever edge Position
                                anchored to. ASS ignores MarginV for a middle
                                anchor, so the slider is only meaningful for top
                                and bottom and is hidden otherwise rather than
                                sitting there doing nothing. */}
                            {position !== 'middle' && (
                                <div className="mt-3">
                                    <div className="flex items-center justify-between mb-2">
                                        <span className="readout">
                                            offset from {position}
                                        </span>
                                        <span className="readout">
                                            {(marginV / ASS_PLAY_RES_Y * 100).toFixed(0)}%
                                        </span>
                                    </div>
                                    <input
                                        type="range"
                                        min={ASS_MARGIN_MIN}
                                        max={ASS_MARGIN_MAX}
                                        step={1}
                                        value={marginV}
                                        onChange={(e) => setMarginV(Number(e.target.value))}
                                        className="w-full"
                                        aria-label={`Caption offset from ${position}`}
                                    />
                                </div>
                            )}
                        </div>

                        {/* Animation Style (new) */}
                        <div>
                            <p className="eyebrow mb-2">Animation</p>
                            <SegmentedControl
                                options={ANIMATION_OPTIONS}
                                value={animation}
                                onChange={setAnimation}
                                columns={2}
                                size="sm"
                            />
                        </div>

                        {/* Editable Transcript (collapsible) */}
                        {useRemotionPreview && (
                            <div>
                                <button
                                    type="button"
                                    onClick={() => setShowTextEditor(!showTextEditor)}
                                    className="w-full flex items-center justify-between mb-2"
                                >
                                    <span className="eyebrow">Edit text ({captions.length} words)</span>
                                    <span className={`text-muted transition-transform ${showTextEditor ? 'rotate-180' : ''}`}>▾</span>
                                </button>
                                {showTextEditor && (
                                    <textarea
                                        value={editableText}
                                        onChange={(e) => handleTextEdit(e.target.value)}
                                        rows={5}
                                        className="input-field resize-none leading-relaxed animate-fade"
                                        placeholder="Edit subtitle text..."
                                    />
                                )}
                            </div>
                        )}

                        {/* Font Family */}
                        <div>
                            <p className="eyebrow mb-2">Font</p>
                            <select
                                value={fontName}
                                onChange={(e) => setFontName(e.target.value)}
                                className="input-field"
                            >
                                {fontOptions.map((f) => (
                                    <option key={f.value} value={f.value} style={{ fontFamily: f.value }}>{f.label}</option>
                                ))}
                            </select>
                        </div>

                        {/* Text Color */}
                        <div>
                            <p className="eyebrow mb-2">Text color</p>
                            <div className="flex flex-wrap items-center gap-2.5">
                                {COLOR_PRESETS.map((c) => (
                                    <button
                                        key={c.color}
                                        onClick={() => setFontColor(c.color)}
                                        className={swatchClass(fontColor === c.color)}
                                        style={{ backgroundColor: c.color }}
                                        title={c.label}
                                    />
                                ))}
                                <label className="w-6 h-6 rounded-full border border-dashed border-rule2 cursor-pointer flex items-center justify-center hover:border-brass transition-colors overflow-hidden relative" title="Custom color">
                                    <span className="text-xs text-muted leading-none">+</span>
                                    <input type="color" value={fontColor} onChange={(e) => setFontColor(e.target.value)} className="absolute inset-0 opacity-0 cursor-pointer" />
                                </label>
                            </div>
                        </div>

                        {/* Highlight Color (new) */}
                        <div>
                            <p className="eyebrow mb-2">Highlight</p>
                            <div className="flex flex-wrap items-center gap-2.5">
                                {HIGHLIGHT_PRESETS.map((c) => (
                                    <button
                                        key={c.color}
                                        onClick={() => setHighlightColor(c.color)}
                                        className={swatchClass(highlightColor === c.color)}
                                        style={{ backgroundColor: c.color }}
                                        title={c.label}
                                    />
                                ))}
                            </div>
                        </div>

                        {/* Border / Outline */}
                        <div>
                            <p className="eyebrow mb-2">Border</p>
                            <div className="flex items-center gap-3">
                                <label className="relative w-8 h-8 rounded-input border border-rule2 cursor-pointer overflow-hidden shrink-0" title="Border color">
                                    <div className="w-full h-full" style={{ backgroundColor: borderColor }} />
                                    <input type="color" value={borderColor} onChange={(e) => setBorderColor(e.target.value)} className="absolute inset-0 opacity-0 cursor-pointer" />
                                </label>
                                <div className="flex-1">
                                    <input
                                        type="range"
                                        min="0"
                                        max="5"
                                        value={borderWidth}
                                        onChange={(e) => setBorderWidth(parseInt(e.target.value))}
                                        className="w-full accent-[var(--color-accent)]"
                                    />
                                    <div className="flex justify-between">
                                        <span className="readout">None</span>
                                        <span className="readout">Thick</span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        {/* Background Box */}
                        <div>
                            <div className="flex items-center justify-between mb-2">
                                <p className="eyebrow">Background</p>
                                <label className="relative inline-flex items-center cursor-pointer">
                                    <input type="checkbox" checked={bgOpacity > 0} onChange={(e) => setBgOpacity(e.target.checked ? 0.5 : 0)} className="sr-only peer" />
                                    <div className="w-8 h-4 rounded-full bg-paper3 peer-checked:bg-brass transition-colors after:content-[''] after:absolute after:top-0 after:left-0 after:h-4 after:w-4 after:rounded-full after:bg-ink after:transition-all peer-checked:after:translate-x-full"></div>
                                </label>
                            </div>
                            {bgOpacity > 0 && (
                                <div className="space-y-3 animate-fade">
                                    <div className="flex items-center gap-3">
                                        <label className="relative w-8 h-8 rounded-input border border-rule2 cursor-pointer overflow-hidden shrink-0" title="Background color">
                                            <div className="w-full h-full" style={{ backgroundColor: bgColor }} />
                                            <input type="color" value={bgColor} onChange={(e) => setBgColor(e.target.value)} className="absolute inset-0 opacity-0 cursor-pointer" />
                                        </label>
                                        <div className="flex-1">
                                            <input
                                                type="range"
                                                min="10"
                                                max="100"
                                                value={Math.round(bgOpacity * 100)}
                                                onChange={(e) => setBgOpacity(parseInt(e.target.value) / 100)}
                                                className="w-full accent-[var(--color-accent)]"
                                            />
                                            <div className="flex justify-between">
                                                <span className="readout">Transparent</span>
                                                <span className="readout">{Math.round(bgOpacity * 100)}%</span>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            )}
                        </div>
                    </div>

                    <div className="mt-5 shrink-0 space-y-2">
                        {(() => {
                            // Text edits must survive the server render path too
                            // (issue #69): send the edited words whenever the text
                            // differs from what the transcript produced.
                            const textEdited = originalCaptions.length > 0
                                && editableText.trim() !== originalCaptions.map((c) => c.text).join(' ').trim();
                            const styleOptions = {
                                position, fontSize, fontName, fontColor, borderColor, borderWidth, bgColor, bgOpacity,
                                // Karaoke burn (server-side ASS render)
                                style, effect, baseOpacity, uppercase, highlightColor, marginV,
                                // The individual fields above already carry the preset's values,
                                // so the id is not redundant for styling — it is how the server
                                // reaches max_chars/max_duration, which have no request field and
                                // are what produce the one-word-per-screen streamer looks.
                                preset: activePreset,
                                // Remotion data
                                remotion: useRemotionPreview ? subtitleConfig : null,
                                captions: textEdited ? captions : null,
                            };
                            const bulkRunning = bulkProgress?.running;
                            return (
                                <>
                                    <div className="flex gap-2">
                                        <button onClick={onClose} className="btn-ghost">
                                            cancel
                                        </button>
                                        <button
                                            onClick={() => onGenerate(styleOptions)}
                                            disabled={isProcessing}
                                            className="btn-primary flex-1"
                                        >
                                            {(isProcessing && !bulkRunning) && <Loader2 size={16} className="animate-spin text-brassink" />}
                                            {(isProcessing && !bulkRunning) ? 'generating...' : 'apply to this clip'}
                                        </button>
                                    </div>
                                    {onApplyAll && bulkCount > 1 && (
                                        <button
                                            onClick={() => onApplyAll({ ...styleOptions, captions: null })}
                                            disabled={isProcessing}
                                            className="btn-ghost w-full flex items-center justify-center gap-2"
                                        >
                                            {bulkRunning
                                                ? <><Loader2 size={16} className="animate-spin" />applying to all… {bulkProgress.current}/{bulkProgress.total}</>
                                                : `apply this style to all ${bulkCount} clips`}
                                        </button>
                                    )}
                                    {/* Clips ship captioned by default, so the way
                                        out has to be here — otherwise a user who
                                        doesn't want captions is stuck with them. */}
                                    {onRemove && (
                                        <button
                                            onClick={onRemove}
                                            disabled={isProcessing}
                                            className="text-xs text-muted underline underline-offset-2 lowercase hover:text-ink2 disabled:opacity-50"
                                        >
                                            remove captions from this clip
                                        </button>
                                    )}
                                </>
                            );
                        })()}
                    </div>
                </div>
            </div>
        </Modal>
    );
}
