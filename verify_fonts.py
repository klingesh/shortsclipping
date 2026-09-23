"""Prove libass resolves every offered font instead of falling back.

Temporary verification script for the font work. libass reports its choice on
stderr at info level; a miss shows up as a "fontselect" line naming a different
family than the one requested, which is the failure mode issue #57 describes and
the reason captions could look right in the browser and wrong in the export.

Run inside the backend container:
    python /app/verify_fonts.py
"""

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import caption_presets
from subtitles import generate_ass

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def _transcript():
    words = [{"word": " HELLO", "start": 0.0, "end": 0.5},
             {"word": " WORLD", "start": 0.5, "end": 1.0}]
    return {"language": "en", "segments": [
        {"start": 0.0, "end": 1.0, "text": "HELLO WORLD", "words": words}]}


def check(family, workdir):
    ass_path = os.path.join(workdir, "probe.ass")
    generate_ass(_transcript(), 0, 2, ass_path, font_name=family, fontsize=44)

    # Confirm the name survived sanitisation and reached the file; a family with
    # characters _sanitize_font_name strips would never reach libass at all.
    ass_text = open(ass_path, encoding="utf-8-sig").read()
    if f"Fontname: {family}" not in ass_text.replace("Fontname:", "Fontname: "):
        style = [l for l in ass_text.splitlines() if l.startswith("Style: Default")]
        if family not in (style[0] if style else ""):
            return family, "NAME-LOST", f"{family!r} not in the ASS Style line"

    out = os.path.join(workdir, "out.mp4")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "info",
         "-f", "lavfi", "-i", "color=c=black:s=320x568:d=1",
         "-vf", f"ass=filename='{ass_path}':fontsdir='{FONTS_DIR}'",
         "-frames:v", "12", out],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return family, "FFMPEG-FAIL", proc.stderr.strip().splitlines()[-1][:160]

    # libass logs e.g. "fontselect: (Bebas Neue, 400, 0) -> BebasNeue-Regular.ttf, 0"
    picks = re.findall(r"fontselect:.*", proc.stderr)
    chose = " | ".join(p.strip() for p in picks) or "(no fontselect line)"
    fell_back = any("DejaVu" in p for p in picks)
    return family, ("FALLBACK" if fell_back else "OK"), chose


def main():
    families = [c["family"] for c in caption_presets.FONT_CHOICES]
    width = max(len(f) for f in families)
    failures = []
    with tempfile.TemporaryDirectory() as workdir:
        for family in families:
            name, status, detail = check(family, workdir)
            print(f"{name:<{width}}  {status:<12} {detail}")
            if status != "OK":
                failures.append(name)
    print()
    print(f"{len(families) - len(failures)}/{len(families)} resolved")
    if failures:
        print("NOT RESOLVED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
