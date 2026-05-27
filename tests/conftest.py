"""Project-wide pytest fixtures.

Subpackage-specific fixtures live in their own conftest.py. Anything needed
by more than one subtree (event bus, ULID seeding, etc.) belongs here.
"""

from __future__ import annotations
