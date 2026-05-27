"""Persistence layer.

- ``base`` defines the ``StateStore`` Protocol with grouped methods
  (tasks, branches, memory) and an async ``transaction()`` context manager.
- ``memory`` provides an in-process implementation for tests and ephemeral use.
- ``sqlite`` provides the default durable implementation.

Postgres lands when distributed deployments require it.
"""
