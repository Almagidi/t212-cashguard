"""Service package.

Keep this module intentionally lightweight.

Do not import heavy service classes here: several services depend on broker and
execution modules, and eager imports from this package can create circular
imports during CLI/smoke-test startup.
"""

__all__: list[str] = []
