# Music beds

Drop audio files here and they become selectable as a background bed on
`POST /api/voiceover`. Listed by `voiceover.music_catalog()` and served to the
dashboard, keyed by bare filename.

Recognised extensions: `.mp3`, `.m4a`, `.aac`, `.wav`, `.ogg`, `.flac`.

This directory is inside the repo on purpose. The container mounts the repo at
`/app`, so a track dropped here is usable immediately. Pointing `MUSIC_DIR` at a
folder elsewhere on the host would need a new compose volume and a container
recreate, which is a much bigger ask for a few files.

A bed shorter than the clip is looped (`-stream_loop -1`) and the result is hard
clamped to the clip's length, so a 30s track covers a 90s clip without changing
the output duration.

Only a bare filename is accepted, never a path: the value arrives over HTTP and
a path would let a request read any file the process can reach.

## Licensing

Nothing is bundled here, because music is the one asset in this project that is
almost never freely redistributable. Whatever you add is yours to clear —
background music is a common trigger for automated copyright claims on TikTok,
Reels and YouTube Shorts even when the video itself is original.
