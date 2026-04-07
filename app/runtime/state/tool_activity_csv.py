"""CSV export / session import helpers for tool activity."""

from __future__ import annotations

import csv
import io
import time
from typing import TYPE_CHECKING, Any

from .tool_activity_models import ToolActivityEntry, check_suspicious

if TYPE_CHECKING:
    from .tool_activity_store import ToolActivityStore


_CSV_COLUMNS = [
    "id", "timestamp", "session_id", "tool", "category",
    "model", "status", "interaction_type", "duration_ms", "risk_score", "flagged",
    "flag_reason", "shield_result", "shield_detail", "shield_elapsed_ms",
    "arguments", "result",
]


def export_csv(store: ToolActivityStore, **filters: Any) -> str:
    """Export filtered entries as CSV string."""
    data = store.query(**filters, limit=10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_CSV_COLUMNS)
    for e in data["entries"]:
        writer.writerow([
            e["id"],
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e["timestamp"])),
            e["session_id"],
            e["tool"],
            e["category"],
            e.get("model", ""),
            e["status"],
            e.get("interaction_type", ""),
            e.get("duration_ms") or "",
            e.get("risk_score", 0),
            "Yes" if e.get("flagged") else "No",
            e.get("flag_reason", ""),
            e.get("shield_result", ""),
            e.get("shield_detail", ""),
            e.get("shield_elapsed_ms") or "",
            (e.get("arguments") or "")[:500],
            (e.get("result") or "")[:500],
        ])
    return output.getvalue()


def import_from_sessions(store: ToolActivityStore, session_store: object) -> int:
    """Backfill tool activity from existing session data."""
    import logging

    from .session_store import SessionStore

    logger = logging.getLogger(__name__)

    if not isinstance(session_store, SessionStore):
        return 0

    existing_ids: set[str] = set()
    with store._lock:  # noqa: SLF001
        existing_ids = {f"{e.session_id}:{e.call_id}" for e in store._entries}  # noqa: SLF001

    count = 0
    for session_summary in session_store.list_sessions():
        sid = session_summary["id"]
        session_data = session_store.get_session(sid)
        if not session_data:
            continue
        for msg in session_data.get("messages", []):
            for tc in msg.get("tool_calls", []):
                key = f"{sid}:{tc.get('name', '')}:{msg.get('timestamp', 0)}"
                if key in existing_ids:
                    continue
                entry = ToolActivityEntry(
                    id=store._next_id(),  # noqa: SLF001
                    session_id=sid,
                    tool=tc.get("name", "unknown"),
                    call_id="",
                    category=store._infer_category(tc.get("name", "")),  # noqa: SLF001
                    arguments=tc.get("arguments", ""),
                    result=tc.get("result", "")[:2000],
                    status="completed",
                    timestamp=msg.get("timestamp", 0),
                )
                flagged, reason, risk, factors = check_suspicious(
                    entry.arguments, entry.result,
                )
                entry.flagged = flagged
                entry.flag_reason = reason
                entry.risk_score = risk
                entry.risk_factors = factors
                store._append(entry)  # noqa: SLF001
                existing_ids.add(key)
                count += 1

    logger.info("[tool_activity] imported %d entries from sessions", count)
    return count
