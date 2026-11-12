"""The framework internals the bot still needs, kept in this one module.

Everything else uses only the public API of Entari 0.19.4 / Letoderea 0.22.1
(enforced by ``tests/test_boundary_guard.py``). Each import below names the
upstream API that is missing; when it appears, only this module changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from arclet.entari.plugin import get_all_subscribers
from arclet.letoderea import Propagator, Subscriber

# Missing upstream API: "attach a propagator to every subscriber registered from
# now on". ``Scope.register`` puts this module-level list in front of every
# registration (plugin scopes and hot-reloaded ones included), but
# ``arclet.letoderea`` exports neither the list nor a function to extend it.
from arclet.letoderea.scope import global_propagators

# Missing upstream API: "which subscriber is running" outside the handler's own
# Contexts. ``SUBSCRIBER`` only lives in that context; this ContextVar is also
# inherited by the tasks a handler starts (scheduler callbacks, workers).
from arclet.letoderea.subscriber import current_subscriber

P = TypeVar("P", bound=Propagator)


def running_subscriber() -> Subscriber | None:
    return current_subscriber.get(None)


def _attach(subscriber: Subscriber, kind: type[P], factory: Callable[[Subscriber], P | None]) -> None:
    try:
        subscriber.get_propagator(kind)
        return  # already attached
    except ValueError:
        pass
    if (propagator := factory(subscriber)) is not None:
        subscriber.propagate(propagator)


class _AttachToEach(Propagator):
    """Attaches ``factory(subscriber)`` to each subscriber as it is registered.

    It only uses the registration-time ``validate`` call and adds nothing itself.
    """

    def __init__(self, kind: type[Propagator], factory: Callable[[Subscriber], Propagator | None]):
        self.kind = kind
        self.factory = factory

    def validate(self, subscriber: Subscriber) -> bool:
        _attach(subscriber, self.kind, self.factory)
        return False

    def compose(self):
        yield from ()


def attach_to_every_subscriber(kind: type[P], factory: Callable[[Subscriber], P | None]) -> Callable[[], None]:
    """Give every plugin subscriber, present and future, ``factory(subscriber)``.

    ``factory`` returns a ``kind`` instance, or ``None`` to leave the subscriber
    alone. Installing the same ``kind`` again is a no-op. Returns a function that
    stops covering future registrations (attached propagators stay).

    "Already attached" is checked with ``Subscriber.get_propagator``, which only sees
    propagators that are still referenced (letoderea keeps them in a WeakSet): return
    a shared instance, or one whose composed callables refer to it.
    """
    hook = next((item for item in global_propagators if isinstance(item, _AttachToEach) and item.kind is kind), None)
    if hook is None:
        hook = _AttachToEach(kind, factory)
        global_propagators.append(hook)
    for subscriber in get_all_subscribers():
        _attach(subscriber, kind, hook.factory)

    def remove() -> None:
        if hook in global_propagators:
            global_propagators.remove(hook)

    return remove
