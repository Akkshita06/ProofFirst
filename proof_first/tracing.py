"""
Thin wrapper around Neatlogs' `span` decorator.

Neatlogs is an optional sponsor-tool integration (see NEATLOGS_API_KEY in
the README). `orchestrator.py` and `agents/reflection_agent.py` import
`span` from here rather than from `neatlogs` directly, so that:

1. If the `neatlogs` package isn't installed at all (it's an optional
   dependency - see requirements.txt), importing this module still
   succeeds and every decorated function runs completely unchanged.
2. If `neatlogs.init()` was never called (NEATLOGS_API_KEY unset - the
   default), `neatlogs.span` itself already degrades gracefully (it logs
   a "no TracerProvider configured" warning and runs the wrapped function
   normally - verified against the installed neatlogs package). This
   wrapper additionally guards against any *other* unexpected error
   `neatlogs.span(...)` might raise while building/wrapping the decorator,
   so a bug or breaking change in the optional dependency can never take
   down the (required) pipeline it's just observing.

Either way, `@span(kind=...)` here is always safe to leave in place with
NEATLOGS_API_KEY unset - decorated functions run identically to how they
would with no decorator at all.
"""
from __future__ import annotations

from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

try:
    import neatlogs as _neatlogs

    def span(kind: str, **kwargs: Any) -> Callable[[F], F]:
        try:
            return _neatlogs.span(kind, **kwargs)
        except Exception:  # pragma: no cover - graceful degrade
            # Something about neatlogs.span itself failed (e.g. an
            # incompatible version) - fall back to a true no-op so the
            # pipeline this is observing is never affected.
            def _noop_decorator(func: F) -> F:
                return func
            return _noop_decorator

except Exception:  # pragma: no cover - neatlogs not installed
    def span(kind: str, **kwargs: Any) -> Callable[[F], F]:
        def _noop_decorator(func: F) -> F:
            return func
        return _noop_decorator
