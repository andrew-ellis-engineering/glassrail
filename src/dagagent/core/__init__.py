"""Core domain types shared by every other subpackage.

Will contain: ``Plan``, ``Node``, ``NodeStatus``, ``NodeType``, ``TaskId``,
``ExecutionState``, and the project-wide error hierarchy.

Nothing in ``core`` may import from any other ``dagagent`` subpackage.
"""
