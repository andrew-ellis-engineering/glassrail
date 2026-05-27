"""Plan validation.

Topological sort, cycle detection, tool-name checking, branch-reference
sanity, subplan depth and count limits, decision nesting cap, and the
``context_needed > 3`` warning. The fresh-context invariant is property-tested
against this module under ``tests/property``.
"""
