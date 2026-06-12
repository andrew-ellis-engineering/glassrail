"""Tests for the web integration (web_fetch + web_search).

Skipped unless the ``web`` extra (trafilatura, lxml) is installed.
"""

from __future__ import annotations

import httpx
import pytest

from glassrail.core import ToolExecutionError

pytest.importorskip("trafilatura")
pytest.importorskip("lxml")

from glassrail.config import WebToolConfig
from glassrail.harness import ToolHarness
from glassrail.harness.integrations.web import (
    DuckDuckGoProvider,
    SearxngProvider,
    extract_main_text,
    parse_ddg_html,
    register_web,
    web_fetch,
    web_search,
)


def _public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


_ARTICLE_HTML = """<!DOCTYPE html><html><head><title>Streaming Protocols</title></head><body>
<header><nav>Home | Blog | Contact</nav></header>
<main><article>
<h1>Choosing a Streaming Protocol</h1>
<p>When you need real-time delivery, the two dominant choices are TCP-based
streaming and UDP-based streaming.</p>
<p>UDP trades reliability for latency, which is why it underpins live voice and video.</p>
</article></main>
<aside>Related: Buffering, Jitter</aside>
<footer>(c) 2026 Example Media</footer>
</body></html>"""


def test_extract_main_text_keeps_body_drops_chrome() -> None:
    title, text = extract_main_text(_ARTICLE_HTML, url="https://example.com/streaming")
    assert title == "Choosing a Streaming Protocol"
    assert text is not None
    assert "UDP trades reliability for latency" in text
    # Navigation / aside / footer chrome is stripped.
    assert "Home | Blog | Contact" not in text
    assert "Example Media" not in text


async def test_web_fetch_returns_extracted_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=_ARTICLE_HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_fetch(
            "https://example.com/streaming", client=client, resolver=_public_resolver
        )

    assert "error" not in result
    assert result["status"] == 200
    assert result["title"] == "Choosing a Streaming Protocol"
    assert "UDP trades reliability for latency" in result["text"]


async def test_web_fetch_http_error_returns_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, html="not found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_fetch(
            "https://example.com/missing", client=client, resolver=_public_resolver
        )

    assert "error" in result
    assert "text" not in result


async def test_web_fetch_rejects_non_http_scheme() -> None:
    with pytest.raises(ToolExecutionError, match="http and https"):
        await web_fetch("ftp://example.com/archive")


async def test_web_fetch_rejects_loopback_target() -> None:
    with pytest.raises(ToolExecutionError, match="private or reserved"):
        await web_fetch("http://127.0.0.1/x")


async def test_web_fetch_rejects_link_local_target() -> None:
    with pytest.raises(ToolExecutionError, match="private or reserved"):
        await web_fetch("http://169.254.169.254/meta")


async def test_web_fetch_allows_private_target_when_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=_ARTICLE_HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_fetch("http://127.0.0.1/x", client=client, allow_private_hosts=True)

    assert "error" not in result
    assert result["status"] == 200


async def test_web_fetch_rejects_body_over_size_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 12)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ToolExecutionError, match="max_fetch_bytes=8"):
            await web_fetch(
                "https://example.com/large",
                client=client,
                resolver=_public_resolver,
                max_fetch_bytes=8,
            )


def test_register_web_registers_fetch_when_enabled() -> None:
    harness = ToolHarness()
    register_web(harness, WebToolConfig(fetch=True))
    assert "web_fetch" in harness.all_names()


def test_register_web_skips_fetch_when_disabled() -> None:
    harness = ToolHarness()
    register_web(harness, WebToolConfig(fetch=False))
    assert "web_fetch" not in harness.all_names()


# ── Search ───────────────────────────────────────────────────────────────────

_DDG_HTML = """<html><body>
<div class="result results_links web-result">
  <div class="links_main result__body">
    <h2 class="result__title">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fcap&amp;rut=x">CAP Theorem</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fcap">Pick two of three.</a>
  </div>
</div>
<div class="result results_links web-result">
  <div class="links_main result__body">
    <h2 class="result__title">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpacelc">PACELC</a>
    </h2>
    <a class="result__snippet">An extension of CAP.</a>
  </div>
</div>
</body></html>"""  # noqa: E501


def test_parse_ddg_html_decodes_redirect_urls() -> None:
    rows = parse_ddg_html(_DDG_HTML, max_results=5)
    assert len(rows) == 2
    assert rows[0] == {
        "title": "CAP Theorem",
        "url": "https://example.com/cap",
        "snippet": "Pick two of three.",
    }
    assert rows[1]["url"] == "https://example.org/pacelc"


def test_parse_ddg_html_respects_max_results() -> None:
    assert len(parse_ddg_html(_DDG_HTML, max_results=1)) == 1


async def test_web_search_duckduckgo() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, html=_DDG_HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_search("cap theorem", provider=DuckDuckGoProvider(), client=client)

    assert result["provider"] == "duckduckgo"
    assert result["results"][0]["url"] == "https://example.com/cap"


async def test_web_search_searxng_reads_json() -> None:
    payload = {"results": [{"title": "CAP", "url": "https://x/cap", "content": "two of three"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json=payload)

    provider = SearxngProvider("http://localhost:8888")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_search("cap", provider=provider, client=client)

    assert result["provider"] == "searxng"
    assert result["results"] == [
        {"title": "CAP", "url": "https://x/cap", "snippet": "two of three"}
    ]


async def test_web_search_http_error_returns_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_search("x", provider=DuckDuckGoProvider(), client=client)

    assert "error" in result
    assert "results" not in result


async def test_web_search_ddg_block_surfaces_as_error() -> None:
    """A 202 anti-bot challenge is reported, not silently parsed to 0 results."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, html="<html><body>anomaly challenge</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_search("x", provider=DuckDuckGoProvider(), client=client)

    assert "results" not in result
    assert "202" in result["error"]


def test_register_web_registers_search_when_enabled() -> None:
    harness = ToolHarness()
    register_web(harness, WebToolConfig(search="duckduckgo"))
    assert "web_search" in harness.all_names()
