"""Shared deployment data types and step-tracking helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class DeployStep:
    """Single step in a deployment pipeline."""

    step: str
    status: str  # "ok" | "failed" | "warn" | "skipped"
    detail: str = ""
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"step": self.step, "status": self.status}
        if self.detail:
            d["detail"] = self.detail
        if self.name:
            d["name"] = self.name
        return d


class StepTracker:
    """Accumulates deployment steps with optional live callback.

    Usage::

        tracker = StepTracker()
        tracker.ok("resource_group", name="polyclaw-rg")
        tracker.fail("bot_resource", detail="CLI error")
        steps_list = tracker.to_list()
    """

    def __init__(self, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._steps: list[dict[str, Any]] = []
        self._cb = callback

    def _append(self, d: dict[str, Any]) -> None:
        self._steps.append(d)
        if self._cb:
            try:
                self._cb(d)
            except Exception:  # noqa: BLE001
                pass

    def _build(self, step: str, status: str, **kw: Any) -> None:
        d: dict[str, Any] = {"step": step, "status": status}
        d.update(kw)
        self._append(d)

    def ok(self, step: str, **kw: Any) -> None:
        self._build(step, "ok", **kw)

    def fail(self, step: str, **kw: Any) -> None:
        self._build(step, "failed", **kw)

    def warn(self, step: str, **kw: Any) -> None:
        self._build(step, "warn", **kw)

    def skip(self, step: str, **kw: Any) -> None:
        self._build(step, "skip", **kw)

    def warning(self, step: str, **kw: Any) -> None:
        self._build(step, "warning", **kw)

    def record(self, step: str, *, ok: bool, **kw: Any) -> None:
        self._build(step, "ok" if ok else "failed", **kw)

    def append(self, item: dict[str, Any]) -> None:
        """Raw append for backward compatibility."""
        self._append(item)

    def extend(self, items: list[dict[str, Any]]) -> None:
        """Append multiple raw step dicts."""
        for item in items:
            self._append(item)

    @property
    def has_failures(self) -> bool:
        return any(s.get("status") == "failed" for s in self._steps)

    def to_list(self) -> list[dict[str, Any]]:
        return list(self._steps)

    def __iter__(self):  # noqa: ANN204
        return iter(self._steps)

    def __len__(self) -> int:
        return len(self._steps)
