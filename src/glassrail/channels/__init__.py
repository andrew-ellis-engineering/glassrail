"""Channel abstraction.

Three channels, each with its own toolset, guardrails, and execution policy:

- ``chat`` — cheap conversational interface, ``read_memory`` + ``mark_memory``
  only, slash commands injected as user-role context.
- ``task`` — full DAG flow with HITL approval and configurable update streaming.
- ``job`` — static pre-written plans on a schedule, no replans, no subplans.
"""
