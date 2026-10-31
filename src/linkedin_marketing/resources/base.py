"""Shared plumbing for resource classes."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import islice

from linkedin_marketing.errors import WriteRefusedError
from linkedin_marketing.safety import Guard
from linkedin_marketing.transport import Transport


class Resource:
    """A group of related endpoints sharing the transport and the guard."""

    def __init__(self, transport: Transport, guard: Guard):
        self._transport = transport
        self._guard = guard

    @property
    def _settings(self):
        return self._transport.settings


def take[T](items: Iterable[T], limit: int | None) -> list[T]:
    """The first ``limit`` items (all when ``limit`` is None)."""
    return list(items if limit is None else islice(items, max(limit, 0)))


def upper_all(values: Iterable[str]) -> list[str]:
    return [str(value).strip().upper() for value in values]


def require_choice(value: str, allowed: Iterable[str], action: str, name: str) -> str:
    """``value`` upper-cased, refused (before any request) when not allowed."""
    allowed = tuple(allowed)
    upper = str(value).strip().upper()
    if upper not in allowed:
        raise WriteRefusedError(
            "invalid_input",
            action,
            f"{name} must be one of {', '.join(allowed)}, not {value!r}",
            field=name,
        )
    return upper
