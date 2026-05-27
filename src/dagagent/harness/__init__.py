"""Tool harness.

- ``ToolHarness`` holds the registry of available tools.
- ``@harness.tool`` is the decorator for first-party tools.
- Third-party plugins are discovered via the ``dagagent.tools`` entry-point group
  at startup; the harness loads and registers them automatically.
"""
