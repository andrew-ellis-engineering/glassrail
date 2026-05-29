# Deployment

The repository ships a production `Dockerfile` that serves the REST gateway.
(`docker-compose.yml` is for local development only — it bind-mounts the source
and runs with `--reload`.)

The image is multi-stage: a builder resolves the locked dependencies with `uv`
and installs the project as a wheel, then a slim `python:3.12-slim` runtime
gets only the resulting virtualenv. It runs as an unprivileged user
(`dagagent`, uid 10001), exposes port 8000, and is ~60 MB.

## Build and run

```bash
docker build -t dagagent:latest .
docker run --rm -p 8000:8000 dagagent:latest
```

```bash
curl http://localhost:8000/health        # {"status": "ok"}
curl -X POST http://localhost:8000/task -H 'content-type: application/json' \
  -d '{"request": "what do I have today?"}'
```

The container has a built-in `HEALTHCHECK` that polls `/health`, so
orchestrators (Compose, Kubernetes, ECS) can read liveness without extra
wiring.

## Configuration

All configuration is via `DAGAGENT_`-prefixed environment variables (the same
[settings](https://github.com/andrewellis/dagagent) the app reads from `.env`
/ `config.toml` locally). Common ones:

```bash
docker run --rm -p 8000:8000 \
  -e DAGAGENT_LOG_LEVEL=INFO \
  -e DAGAGENT_LOG_JSON=true \
  -e DAGAGENT_TIER0__BASE_URL=http://my-llm:8080/v1 \
  -e DAGAGENT_TIER0__MODEL=my-model \
  dagagent:latest
```

The default wiring keeps task state in memory. To persist across restarts,
configure the SQLite store path (`DAGAGENT_STATE_PATH`) and mount a volume for
it. The `sqlite` extra is already included in the image.

## CI

CI builds the image on every change and smoke-tests it: it starts the
container, waits for `/health`, checks `/tools`, and asserts the process runs
as the non-root uid. That job is independent of the distribution build.
