# SearXNG — local search backend for glassrail

A self-hosted [SearXNG](https://searxng.github.io/searxng/) instance that
glassrail's `SearxngProvider` talks to instead of scraping DuckDuckGo.

## First-time setup

`settings.yml` holds a real `secret_key`, so it is gitignored. Create it from
the template and set a secret before the first run:

```bash
cd deploy/searxng/searxng
cp settings.yml.example settings.yml
# set `secret_key:` to the output of:
openssl rand -hex 32
```

## Start

```bash
cd deploy/searxng
docker compose up -d
```

The container binds to `127.0.0.1:8888` only (localhost, never public).

## Stop

```bash
cd deploy/searxng
docker compose down
```

## Wiring it into glassrail

In your `config.toml` (project root):

```toml
[tools.web]
search = "searxng"
searxng_url = "http://localhost:8888"
```

Or via environment variables:

```
GLASSRAIL_TOOLS__WEB__SEARCH=searxng
GLASSRAIL_TOOLS__WEB__SEARXNG_URL=http://localhost:8888
```

These match the defaults in `WebToolConfig`, so if you haven't overridden them
you don't need to set them explicitly — just flip `search` to `"searxng"`.

## JSON API gotcha

SearXNG disables its JSON API by default; requests with `&format=json` return
**HTTP 403** unless `json` is explicitly listed in `search.formats` in
`settings.yml`. This config already includes it:

```yaml
search:
  formats:
    - html
    - json
```

If you ever reset to a fresh `settings.yml`, add that block or the provider
will silently fail.

## Verify the API manually

```bash
curl -s "http://localhost:8888/search?q=test&format=json" | python3 -m json.tool | head -40
```

You should see a JSON object with a non-empty `results` array, not a 403 or
HTML error page.
