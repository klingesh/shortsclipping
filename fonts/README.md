# Bundled fonts

Caption burns pass `fontsdir=` at this directory (`subtitles.burn_subtitles`), so
libass resolves any family whose TTF lives here without needing a fontconfig
alias. `openshorts-fontmap.conf` covers the opposite case: names the UI offers
that are *not* here and must be redirected onto a family the image installs.

Family names are declared in `caption_presets.BUNDLED_FONTS`, not derived from
filenames. The two disagree for every multi-word family — `BebasNeue-Regular.ttf`
reports `Bebas Neue` — and libass matches on the family, so a filename-derived
name resolves to nothing and silently falls back to DejaVu (issue #57).

| Family | File | License | Upstream |
|---|---|---|---|
| Anton | `Anton-Regular.ttf` | SIL Open Font License 1.1 | [google/fonts ofl/anton](https://github.com/google/fonts/tree/main/ofl/anton) |
| Noto Serif | `NotoSerif-Bold.ttf` | SIL Open Font License 1.1 | [google/fonts ofl/notoserif](https://github.com/google/fonts/tree/main/ofl/notoserif) |
| Bebas Neue | `BebasNeue-Regular.ttf` | SIL Open Font License 1.1 — `LICENSE-BebasNeue-OFL.txt` | [google/fonts ofl/bebasneue](https://github.com/google/fonts/tree/main/ofl/bebasneue) |
| Archivo Black | `ArchivoBlack-Regular.ttf` | SIL Open Font License 1.1 — `LICENSE-ArchivoBlack-OFL.txt` | [google/fonts ofl/archivoblack](https://github.com/google/fonts/tree/main/ofl/archivoblack) |
| Luckiest Guy | `LuckiestGuy-Regular.ttf` | Apache License 2.0 — `LICENSE-LuckiestGuy-Apache.txt` | [google/fonts apache/luckiestguy](https://github.com/google/fonts/tree/main/apache/luckiestguy) |

Anton and Noto Serif predate this note and shipped without their license text.
Both are OFL 1.1 from the same upstream; add the files if you redistribute.

## Adding a font

1. Drop the static TTF here. Avoid variable fonts — libass support is unreliable.
2. Confirm the family name the file actually reports, rather than assuming:
   ```sh
   docker compose exec backend fc-scan /app/fonts | grep -E 'file:|family:'
   ```
3. Register it in `caption_presets.BUNDLED_FONTS` keyed by that family name, and
   add it to `FONT_CHOICES` if the UI should offer it.
4. Include the upstream license file and add a row above.

`tests/test_caption_presets.py` then enforces that every preset and every offered
font resolves, and that each bundled file is really an sfnt font rather than a
failed download. Before the font list moved server-side nothing could check this:
the only guard covered the single default family, so a font offered by the UI but
missing from the image would burn as DejaVu with no error.

`verify_fonts.py` is the stronger check when touching this directory. It burns a
probe caption per offered family and reads libass's own `fontselect` output, so it
reports the family that was actually used instead of the one that was asked for.

The `Dockerfile` copies `*.ttf` and the fontmap into the image at build time, so
a **new alias needs an image rebuild**. A new TTF does not: `fontsdir=` reads
this directory through the compose bind mount at run time.
