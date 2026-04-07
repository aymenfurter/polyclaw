"""Singleton registry for test isolation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar, overload

_reset_fns: list[Callable[[], None]] = []

T = TypeVar("T")


def register_singleton(reset_fn: Callable[[], None]) -> None:
    """Register a reset function to be called during test teardown."""
    _reset_fns.append(reset_fn)


def reset_all_singletons() -> None:
    """Reset every registered singleton -- intended for test isolation."""
    for fn in _reset_fns:
        fn()


class Singleton(Generic[T]):
    """Descriptor that lazily creates a singleton and registers it for test reset.

    Usage at module level::

        get_foo, _reset_foo = Singleton.create(FooClass)

    Or with a custom factory::

        get_foo, _reset_foo = Singleton.create(FooClass, factory=lambda: FooClass(arg))
    """

    @staticmethod
    def create(
        cls: type[T],
        *,
        factory: Callable[[], T] | None = None,
    ) -> tuple[Callable[[], T], Callable[[T | None], None]]:
        """Return a ``(getter, resetter)`` pair for *cls*.

        The *resetter* can be called with no args (or ``None``) to clear the
        singleton, or with an instance to replace it (useful in tests).
        """
        instance: list[T | None] = [None]

        def get() -> T:
            if instance[0] is None:
                instance[0] = factory() if factory else cls()
            return instance[0]  # type: ignore[return-value]

        def reset(value: T | None = None) -> None:
            instance[0] = value

        register_singleton(reset)
        return get, reset
