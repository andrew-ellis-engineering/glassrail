"""LLM provider abstraction.

- ``base`` defines the ``LLMProvider`` Protocol — a single streaming
  ``complete()`` method. Non-streaming callers collect the iterator.
- ``router`` defines ``TierRouter``, which owns timeout/fallthrough across
  an ordered list of providers. Providers themselves stay simple.
- Concrete providers (``openai_compat``, ``anthropic``, ``mlx``) implement
  the Protocol.
"""
