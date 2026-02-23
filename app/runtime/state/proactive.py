"""Proactive follow-up messaging state.

Manages a single pending follow-up message that the agent schedules
during memory formation. Only one pending message at a time -- scheduling
a new one replaces the previous.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config.settings import cfg

logger = logging.getLogger(__name__)


@dataclass
class PendingMessage:
    id: str
    message: str
    deliver_at: str
    context: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class SentMessage:
    id: str
    message: str
    delivered_at: str
    context: str = ""
    created_at: str = ""
    reaction: str = ""
    reaction_detail: str = ""


@dataclass
class ProactivePreferences:
    min_gap_hours: int = 4
    max_daily: int = 3
    avoided_topics: list[str] = field(default_factory=list)
    preferred_times: str = ""


class ProactiveStore:
    """JSON-file-backed store for proactive follow-up state."""

    _MAX_HISTORY = 100

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (cfg.data_dir / "proactive.json")
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load proactive state: %s", exc, exc_info=True)
                self._data = {}
        env_default = cfg.proactive_enabled if not self._path.exists() else False
        self._data.setdefault("enabled", env_default)
        self._data.setdefault("pending", None)
        self._data.setdefault("history", [])
        self._data.setdefault("preferences", asdict(ProactivePreferences()))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2) + "\n")

    def reload(self) -> None:
        self._load()

    # -- enabled -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self._data.get("enabled", False))

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._data["enabled"] = value
        self._save()

    # -- pending message ---------------------------------------------------

    @property
    def pending(self) -> PendingMessage | None:
        raw = self._data.get("pending")
        if not raw:
            return None
        try:
            return PendingMessage(**raw)
        except (TypeError, KeyError):
            return None

    def schedule_followup(self, message: str, deliver_at: str, context: str = "") -> PendingMessage:
        msg = PendingMessage(
            id=uuid.uuid4().hex[:8],
            message=message,
            deliver_at=deliver_at,
            context=context,
        )
        self._data["pending"] = asdict(msg)
        self._save()
        logger.info("Proactive follow-up scheduled: id=%s deliver_at=%s", msg.id, deliver_at)
        return msg

    def clear_pending(self) -> PendingMessage | None:
        raw = self._data.get("pending")
        self._data["pending"] = None
        self._save()
        if raw:
            try:
                return PendingMessage(**raw)
            except (TypeError, KeyError):
                return None
        return None

    # -- history -----------------------------------------------------------

    @property
    def history(self) -> list[SentMessage]:
        return [
            SentMessage(**entry)
            for entry in self._data.get("history", [])
            if isinstance(entry, dict)
        ]

    def record_sent(
        self,
        message: str,
        context: str = "",
        created_at: str = "",
        msg_id: str = "",
    ) -> SentMessage:
        sent = SentMessage(
            id=msg_id or uuid.uuid4().hex[:8],
            message=message,
            delivered_at=datetime.now(UTC).isoformat(),
            context=context,
            created_at=created_at,
        )
        entries = self._data.setdefault("history", [])
        entries.append(asdict(sent))
        if len(entries) > self._MAX_HISTORY:
            self._data["history"] = entries[-self._MAX_HISTORY:]
        self._save()
        return sent

    def update_reaction(self, msg_id: str, reaction: str, detail: str = "") -> bool:
        for entry in reversed(self._data.get("history", [])):
            if entry.get("id") == msg_id:
                entry["reaction"] = reaction
                entry["reaction_detail"] = detail
                self._save()
                return True
        return False

    def mark_latest_reaction(self, reaction: str, detail: str = "") -> bool:
        history = self._data.get("history", [])
        if not history:
            return False
        history[-1]["reaction"] = reaction
        history[-1]["reaction_detail"] = detail
        self._save()
        return True

    # -- preferences -------------------------------------------------------

    @property
    def preferences(self) -> ProactivePreferences:
        raw = self._data.get("preferences", {})
        try:
            return ProactivePreferences(**raw)
        except (TypeError, KeyError):
            return ProactivePreferences()

    def update_preferences(self, **kwargs: Any) -> None:
        prefs = self._data.setdefault("preferences", {})
        for key, val in kwargs.items():
            if hasattr(ProactivePreferences, key):
                prefs[key] = val
        self._save()

    # -- query helpers -----------------------------------------------------

    def messages_sent_today(self) -> int:
        now = datetime.now(UTC)
        count = 0
        for entry in self._data.get("history", []):
            try:
                delivered = datetime.fromisoformat(entry["delivered_at"])
                if delivered.tzinfo is None:
                    delivered = delivered.replace(tzinfo=UTC)
                if (now - delivered).total_seconds() < 86400:
                    count += 1
            except (KeyError, ValueError):
                continue
        return count

    def hours_since_last_sent(self) -> float | None:
        history = self._data.get("history", [])
        if not history:
            return None
        try:
            last = datetime.fromisoformat(history[-1]["delivered_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            return (datetime.now(UTC) - last).total_seconds() / 3600
        except (KeyError, ValueError):
            return None

    def is_due(self) -> bool:
        pending = self.pending
        if not pending:
            return False
        try:
            deliver_dt = datetime.fromisoformat(pending.deliver_at)
            if deliver_dt.tzinfo is None:
                deliver_dt = deliver_dt.replace(tzinfo=UTC)
            return datetime.now(UTC) >= deliver_dt
        except ValueError:
            return False

    def get_full_state(self) -> dict[str, Any]:
        self.reload()
        return {
            "enabled": self.enabled,
            "pending": self._data.get("pending"),
            "history": self._data.get("history", []),
            "preferences": self._data.get("preferences", {}),
            "messages_sent_today": self.messages_sent_today(),
            "hours_since_last_sent": self.hours_since_last_sent(),
        }


# -- singleton -------------------------------------------------------------

_store: ProactiveStore | None = None


def get_proactive_store() -> ProactiveStore:
    global _store
    if _store is None:
        _store = ProactiveStore()
    return _store


def _reset_proactive_store() -> None:
    global _store
    _store = None


from ..util.singletons import register_singleton
register_singleton(_reset_proactive_store)
