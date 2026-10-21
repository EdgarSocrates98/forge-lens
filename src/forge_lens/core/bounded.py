"""Bounded collection for warnings/quarantine notes.

A single malformed span must never cost more than a few bytes of
evidence — bounded-resource inegociável #13 applies to *generated*
data too, not just accepted payloads.
"""

from __future__ import annotations

from collections.abc import Iterable

WARNINGS_CAP = 200


class BoundedList(list[str]):
    """``list[str]`` with a hard cap: appends past the cap are counted,
    not stored; ``summary()`` materializes the overflow note once.
    """

    def __init__(self, cap: int = WARNINGS_CAP) -> None:
        super().__init__()
        self.cap = cap
        self.overflow = 0

    def append(self, item: str) -> None:
        if len(self) < self.cap:
            super().append(item)
        else:
            self.overflow += 1

    def extend(self, items: Iterable[str]) -> None:
        for item in items:
            self.append(item)

    def summary(self) -> list[str]:
        """Plain list of stored items + one trailing overflow note."""
        if not self.overflow:
            return list(self)
        return [
            *self,
            f"... and {self.overflow} more item(s) (cap {self.cap})",
        ]
