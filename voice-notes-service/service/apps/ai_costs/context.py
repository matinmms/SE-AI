"""Request-local attribution for provider calls made inside worker tasks."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_cost_context: ContextVar[dict[str, object]] = ContextVar("avalai_cost_context", default={})


def current_cost_context() -> dict[str, object]:
    return dict(_cost_context.get())


@contextmanager
def avalai_cost_context(**context: object) -> Iterator[None]:
    token = _cost_context.set({key: value for key, value in context.items() if value})
    try:
        yield
    finally:
        _cost_context.reset(token)
