"""Plan generation.

Single LLM call (via the ``TierRouter``) with JSON-mode output. Validates
the result, replans once on validation failure, then surfaces the error.
"""
