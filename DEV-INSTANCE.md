# Running this clone alongside a production OpenShorts

This checkout is a development fork. If you already run OpenShorts from another
folder, start this one with `docker-compose.dev.yml`, never the default
`docker-compose.yml`.

```powershell
# this clone
docker compose -p shortsedit -f docker-compose.dev.yml up -d

# the production one, from ITS folder
docker compose up -d
```

|          | Production          | This clone            |
|----------|---------------------|-----------------------|
| Project  | `openshorts-main`   | `shortsedit`          |
| Dashboard| http://localhost:5175 | http://localhost:5185 |
| API      | http://localhost:8000 | http://localhost:8010 |
| Renderer | http://localhost:3100 | http://localhost:3110 |
| Containers | `openshorts-*`    | `shortsedit-*`        |

## Why the default file cannot be used twice

Compose takes its project name from the containing **directory**. A checkout is
usually called `openshorts-main` wherever it lives, so two clones look like one
project. `docker-compose.yml` also hardcodes `container_name:`, which is global to
the Docker daemon, and publishes fixed host ports.

The result is not two stacks. Running `docker compose up` in the second clone
**adopts the first one's containers and repoints them at the second clone's
source**. The production instance keeps serving on port 8000 while actually
running this fork's code, with no error to suggest anything happened.

`docker-compose.dev.yml` is a complete file rather than an override because
Compose merges `ports` as a list: an override adding `8010:8000` can publish 8000
as well and collide regardless.

## Checking which source a container is serving

```powershell
docker inspect shortsedit-backend --format "{{range .Mounts}}{{.Source}} -> {{.Destination}}`n{{end}}"
```

Worth running whenever something behaves unexpectedly — a container serving the
wrong folder looks completely healthy.

## Dev dependencies are not in the image

The runtime image has no `pytest`, and the local TTS backend needs packages the
image does not carry yet (`Dockerfile` and `requirements.txt` list them for the
next rebuild, but a rebuild is heavy). Install them into the running container:

```powershell
docker compose -p shortsedit -f docker-compose.dev.yml exec -u 0 backend `
  sh -c "apt-get update -qq && apt-get install -y -qq espeak-ng && pip install -q pytest kokoro soundfile"
```

These live in the container's writable layer. They survive a restart and a Docker
daemon crash, but **not** `down` or any recreate, so expect to repeat this after
`docker compose down`.

## Tests

```powershell
docker compose -p shortsedit -f docker-compose.dev.yml exec -u 0 backend `
  python -m pytest tests/ -q
```

The verification scripts need real media and are separate from the suite:
`verify_fonts.py`, `verify_placement.py`, `verify_tts.py`, `verify_voiceover.py`,
`verify_track.py`.

Tracking a 1440x2560 60fps clip end to end takes minutes on CPU. Cut a small
excerpt first:

```powershell
ffmpeg -t 4 -i input.mp4 -vf scale=540:960 -r 30 -an excerpt.mp4
```
