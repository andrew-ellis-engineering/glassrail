"""Tests for the web integration (web_fetch).

Skipped unless the ``web`` extra (trafilatura) is installed.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("trafilatura")

from dagagent.config import WebToolConfig
from dagagent.harness import ToolHarness
from dagagent.harness.integrations.web import (
    extract_main_text,
    register_web,
    web_fetch,
)

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
        result = await web_fetch("https://example.com/streaming", client=client)

    assert "error" not in result
    assert result["status"] == 200
    assert result["title"] == "Choosing a Streaming Protocol"
    assert "UDP trades reliability for latency" in result["text"]


async def test_web_fetch_http_error_returns_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, html="not found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await web_fetch("https://example.com/missing", client=client)

    assert "error" in result
    assert "text" not in result


def test_register_web_registers_fetch_when_enabled() -> None:
    harness = ToolHarness()
    register_web(harness, WebToolConfig(fetch=True))
    assert "web_fetch" in harness.all_names()


def test_register_web_skips_fetch_when_disabled() -> None:
    harness = ToolHarness()
    register_web(harness, WebToolConfig(fetch=False))
    assert "web_fetch" not in harness.all_names()
