"""Plan execution.

Topological node iteration. Each node runs with a fresh context derived from
its declared ``context_needed`` — nothing leaks across the boundary. Branch
nodes choose a path; non-selected branches are skipped. HITL approval gates
pause and resume the run.
"""
