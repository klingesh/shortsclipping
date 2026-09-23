"""Measure where burned captions actually land, per path and per alignment.

Reads pixels instead of trusting the alignment constant. burn_subtitles maps
top->6 and middle->10, which are legacy SSA codes; libass force_style takes ASS
v4+ numpad codes, where 6 is middle-RIGHT and 10 is out of range. This script
says whether that is a real defect or a harmless historical detail.

Reports the text's bounding box as a fraction of frame height and width, so
"top" should sit near 0.0 and "bottom" near 1.0, both horizontally centred.

Run inside the backend container:
    python /app/verify_placement.py
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image

from subtitles import SAFE_MARGIN_V, generate_ass, generate_srt, burn_subtitles

W, H = 320, 568


def _transcript():
    words = [{"word": " XX", "start": 0.0, "end": 2.0}]
    return {"language": "en", "segments": [
        {"start": 0.0, "end": 2.0, "text": "XX", "words": words}]}


def _blank_video(path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c=black:s={W}x{H}:d=2", "-frames:v", "48", path],
        check=True, capture_output=True)


def _text_box(video_path, workdir, tag):
    """Bounding box of non-black pixels in a mid-clip frame, normalised."""
    frame = os.path.join(workdir, f"{tag}.png")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", video_path,
         "-frames:v", "1", frame],
        check=True, capture_output=True)
    img = Image.open(frame).convert("L")
    box = img.point(lambda p: 255 if p > 40 else 0).getbbox()
    if box is None:
        return None
    left, top, right, bottom = box
    return {
        "y_mid": (top + bottom) / 2 / H,
        "x_mid": (left + right) / 2 / W,
        "top": top / H,
        "bottom": bottom / H,
    }


def run(workdir):
    src = os.path.join(workdir, "src.mp4")
    _blank_video(src)
    rows = []

    for alignment in ("top", "middle", "bottom"):
        # --- karaoke path: styling baked into the .ass, force_style skipped ---
        ass_path = os.path.join(workdir, f"k_{alignment}.ass")
        generate_ass(_transcript(), 0, 2, ass_path,
                     alignment=alignment, fontsize=44)
        out = os.path.join(workdir, f"k_{alignment}.mp4")
        burn_subtitles(src, ass_path, out, alignment=alignment, fontsize=44)
        rows.append(("karaoke/.ass", alignment, _text_box(out, workdir, f"k_{alignment}")))

        # --- classic path: alignment applied via libass force_style ---
        srt_path = os.path.join(workdir, f"c_{alignment}.srt")
        generate_srt(_transcript(), 0, 2, srt_path)
        out = os.path.join(workdir, f"c_{alignment}.mp4")
        burn_subtitles(src, srt_path, out, alignment=alignment, fontsize=44)
        rows.append(("classic/.srt", alignment, _text_box(out, workdir, f"c_{alignment}")))

    print(f"{'path':<14} {'align':<8} {'y_mid':>7} {'x_mid':>7}  verdict")
    print("-" * 62)
    for path, alignment, box in rows:
        if box is None:
            print(f"{path:<14} {alignment:<8} {'-':>7} {'-':>7}  NO TEXT RENDERED")
            continue
        expected = {"top": 0.15, "middle": 0.5, "bottom": 0.85}[alignment]
        off = abs(box["y_mid"] - expected)
        centred = abs(box["x_mid"] - 0.5) < 0.1
        verdict = "ok" if off < 0.18 and centred else "MISPLACED"
        if not centred:
            verdict += f" (x_mid {box['x_mid']:.2f}, not centred)"
        print(f"{path:<14} {alignment:<8} {box['y_mid']:>7.3f} "
              f"{box['x_mid']:>7.3f}  {verdict}")

    # --- margin_v sweep on BOTH paths ---
    # The classic path used to hardcode MarginV=SAFE_MARGIN_V into force_style,
    # so its column stayed frozen at one value no matter what was asked for.
    # Both columns should now move together.
    print()
    print(f"margin_v sweep (bottom anchor, SAFE_MARGIN_V={SAFE_MARGIN_V}, PlayResY=288)")
    print(f"{'margin_v':>9} {'karaoke':>9} {'classic':>9}  {'edge frac':>10}")
    print("-" * 44)
    frozen_check = []
    for margin in (0, 43, 100, 160, 220):
        ass_path = os.path.join(workdir, f"m_{margin}.ass")
        generate_ass(_transcript(), 0, 2, ass_path, alignment="bottom",
                     fontsize=44, margin_v=margin)
        out_k = os.path.join(workdir, f"mk_{margin}.mp4")
        burn_subtitles(src, ass_path, out_k, alignment="bottom", fontsize=44,
                       margin_v=margin)
        box_k = _text_box(out_k, workdir, f"mk_{margin}")

        srt_path = os.path.join(workdir, f"m_{margin}.srt")
        generate_srt(_transcript(), 0, 2, srt_path)
        out_c = os.path.join(workdir, f"mc_{margin}.mp4")
        burn_subtitles(src, srt_path, out_c, alignment="bottom", fontsize=44,
                       margin_v=margin)
        box_c = _text_box(out_c, workdir, f"mc_{margin}")

        k = f"{box_k['y_mid']:.3f}" if box_k else "-"
        c = f"{box_c['y_mid']:.3f}" if box_c else "-"
        frozen_check.append(box_c["y_mid"] if box_c else None)
        print(f"{margin:>9} {k:>9} {c:>9}  {1 - margin / 288:>10.3f}")

    moved = len({round(v, 2) for v in frozen_check if v is not None})
    print()
    print(f"classic path took {moved} distinct positions across 5 margins "
          f"({'OK' if moved >= 4 else 'STILL FROZEN'})")


def main():
    with tempfile.TemporaryDirectory() as workdir:
        run(workdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
