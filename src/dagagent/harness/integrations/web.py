"""Web integration — page fetch (search lives alongside it).

Needs the ``web`` extra (``trafilatura``, which pulls in ``lxml``). The
trafilatura import is lazy so the base install neither requires nor imports it
until the integration is enabled; enabling without the extra raises a clear
:class:`ToolRegistrationError`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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
        import trafilatura  # noqa: PLC0415  (deferred: optional 'web' extra)
    except ModuleNotFoundError as exc:
        raise ToolRegistrationError(
            "The web integration needs the 'web' extra. Install it with "
            "`pip install dagagent[web]` (or `uv sync --extra web`)."
        ) from exc
    return trafilatura


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
