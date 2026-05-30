"""First-party tool integrations, registered from settings.

Each integration is bundled in-tree and toggled under ``settings.tools``. This
is distinct from third-party ``dagagent.tools`` entry-point plugins (gated by
``load_tool_plugins``): integrations ship here and carry their own config.

Integration modules are imported lazily — only when enabled — so optional
extras (e.g. ``web`` → trafilatura) aren't required by the base install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dagagent.config import Settings
    from dagagent.harness.registry import ToolHarness


def register_integrations(harness: ToolHarness, settings: Settings) -> None:
    """Register every enabled first-party integration on ``harness``."""
    web = settings.tools.web
    if web.fetch or web.search != "none":
        # Deferred on purpose: only import the web module (and its optional
        # 'web' extra) when the integration is actually enabled.
        from dagagent.harness.integrations.web import register_web  # noqa: PLC0415

        register_web(harness, web)
