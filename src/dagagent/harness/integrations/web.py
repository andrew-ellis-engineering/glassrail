"""Web integration — page fetch (search lives alongside it).

Needs the ``web`` extra (``trafilatura``, which pulls in ``lxml``). The
trafilatura import is lazy so the base install neither requires nor imports it
until the integration is enabled; enabling without the extra raises a clear
:class:`ToolRegistrationError`.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from dagagent.core import ToolRegistrationError

if TYPE_CHECKING:
    from dagagent.config import WebToolConfig
    from dagagent.harness.registry import ToolHarness

log = logging.getLogger(__name__)

_USER_AGENT = "dagagent-web-fetch/0.1 (+https://github.com/andrewellis/dagagent)"


def _require_trafilatura() -> Any:
    """Import trafilatura, or raise a clear install hint."""
    try:
        return importlib.import_module("trafilatura")
    except ModuleNotFoundError as exc:
        raise ToolRegistrationError(
            "The web integration needs the 'web' extra. Install it with "
            "`pip install dagagent[web]` (or `uv sync --extra web`)."
        ) from exc


def extract_main_text(html: str, *, url: str | None = None) -> tuple[str | None, str | None]:
    """Extract a page's title and main body text from raw HTML.

    Uses trafilatura's main-content extraction (boilerplate/nav/ads removed),
    favouring recall so summaries see the full substance. Returns
    ``(title, text)``; either may be ``None`` if nothing usable was found.
    """
    trafilatura = _require_trafilatura()
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    title: str | None = None
    metadata = trafilatura.extract_metadata(html)
    if metadata is not None:
        title = metadata.title
    return title, text


async def web_fetch(
    url: str,
    *,
    timeout_s: float = 20.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET ``url`` and return its extracted main text.

    Returns ``{url, title, text, status}`` on success, or ``{url, error}`` on a
    transport error or when no content could be extracted. ``client`` is
    injectable for tests; when omitted a short-lived client is created.
    """
    headers = {"User-Agent": _USER_AGENT}
    try:
        if client is not None:
            resp = await client.get(url, headers=headers, follow_redirects=True, timeout=timeout_s)
        else:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as owned:
                resp = await owned.get(url, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}

    title, text = extract_main_text(resp.text, url=url)
    if not text:
        return {"url": url, "status": resp.status_code, "error": "no extractable content"}
    return {"url": url, "title": title, "text": text, "status": resp.status_code}


# ── Search ───────────────────────────────────────────────────────────────────


def _require_lxml() -> Any:
    """Import lxml.html (ships with the 'web' extra via trafilatura)."""
    try:
        return importlib.import_module("lxml.html")
    except ModuleNotFoundError as exc:
        raise ToolRegistrationError(
            "DuckDuckGo search needs the 'web' extra: pip install dagagent[web]."
        ) from exc


class SearchProvider(Protocol):
    """A web-search backend: query in, normalized result rows out."""

    name: str

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> list[dict[str, Any]]: ...


def _decode_ddg_href(href: str | None) -> str:
    """DuckDuckGo HTML results wrap the real URL in a ``/l/?uddg=`` redirect."""
    if not href:
        return ""
    parsed = urlparse(href)
    uddg = parse_qs(parsed.query).get("uddg")
    if uddg:
        return uddg[0]
    return f"https:{href}" if href.startswith("//") else href


def parse_ddg_html(html: str, *, max_results: int) -> list[dict[str, Any]]:
    """Parse DuckDuckGo's HTML results page into ``{title, url, snippet}`` rows."""
    lxml_html = _require_lxml()
    doc = lxml_html.fromstring(html)
    rows: list[dict[str, Any]] = []
    for anchor in doc.xpath('//a[contains(@class, "result__a")]'):
        snippet_nodes = anchor.xpath(
            './ancestor::div[contains(@class, "links_main") '
            'or contains(@class, "result__body")][1]'
            '//a[contains(@class, "result__snippet")]'
        )
        rows.append(
            {
                "title": anchor.text_content().strip(),
                "url": _decode_ddg_href(anchor.get("href")),
                "snippet": snippet_nodes[0].text_content().strip() if snippet_nodes else "",
            }
        )
        if len(rows) >= max_results:
            break
    return rows


class DuckDuckGoProvider:
    """Scrapes DuckDuckGo's HTML endpoint. No key, but brittle by nature."""

    name = "duckduckgo"
    _URL = "https://html.duckduckgo.com/html/"

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> list[dict[str, Any]]:
        headers = {"User-Agent": _USER_AGENT}
        data = {"q": query}
        if client is not None:
            resp = await client.post(
                self._URL, data=data, headers=headers, timeout=timeout_s, follow_redirects=True
            )
        else:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as owned:
                resp = await owned.post(self._URL, data=data, headers=headers)
        resp.raise_for_status()
        return parse_ddg_html(resp.text, max_results=max_results)


class SearxngProvider:
    """Queries a self-hosted SearXNG instance's JSON API. Robust, needs a box."""

    name = "searxng"

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> list[dict[str, Any]]:
        headers = {"User-Agent": _USER_AGENT}
        params = {"q": query, "format": "json"}
        url = f"{self._base_url}/search"
        if client is not None:
            resp = await client.get(url, params=params, headers=headers, timeout=timeout_s)
        else:
            async with httpx.AsyncClient(timeout=timeout_s) as owned:
                resp = await owned.get(url, params=params, headers=headers)
        resp.raise_for_status()
        results = resp.json().get("results", [])[:max_results]
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in results
        ]


def _build_search_provider(config: WebToolConfig) -> SearchProvider:
    if config.search == "duckduckgo":
        _require_lxml()  # fail fast with the install hint
        return DuckDuckGoProvider()
    if config.search == "searxng":
        return SearxngProvider(config.searxng_url)
    raise ToolRegistrationError(f"Unknown web search provider: {config.search!r}")


async def web_search(
    query: str,
    *,
    provider: SearchProvider,
    max_results: int = 5,
    timeout_s: float = 20.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Run a web search through ``provider``.

    Returns ``{query, provider, results}`` on success, or ``{query, provider,
    error}`` on a transport error. ``client`` is injectable for tests.
    """
    try:
        results = await provider.search(
            query, max_results=max_results, timeout_s=timeout_s, client=client
        )
    except httpx.HTTPError as exc:
        return {"query": query, "provider": provider.name, "error": f"{type(exc).__name__}: {exc}"}
    return {"query": query, "provider": provider.name, "results": results}


# ── Registration ───────────────────────────────────────────────────────────


def register_web(harness: ToolHarness, config: WebToolConfig) -> None:
    """Register the enabled web tools on ``harness`` per ``config``."""
    if config.fetch:
        _require_trafilatura()  # fail fast with the install hint
        timeout_s = config.timeout_s

        async def _web_fetch(url: str) -> dict[str, Any]:
            return await web_fetch(url, timeout_s=timeout_s)

        harness.tool(
            name="web_fetch",
            description=(
                "Fetch a web page by URL and return its main text content "
                "(boilerplate removed). Use for reading or summarising a page."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The absolute URL to fetch"},
                },
                "required": ["url"],
            },
        )(_web_fetch)
        log.info("Registered web tool: web_fetch")

    if config.search != "none":
        provider = _build_search_provider(config)
        max_results = config.max_results
        timeout_s = config.timeout_s

        async def _web_search(query: str) -> dict[str, Any]:
            return await web_search(
                query, provider=provider, max_results=max_results, timeout_s=timeout_s
            )

        harness.tool(
            name="web_search",
            description=(
                "Search the web and return a list of results (title, url, snippet). "
                "Use to find pages; pair with web_fetch to read one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                },
                "required": ["query"],
            },
        )(_web_search)
        log.info("Registered web tool: web_search (provider=%s)", provider.name)
