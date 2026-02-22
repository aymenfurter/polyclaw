"""Tool activity store -- aggregates tool calls across sessions for audit."""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config.settings import cfg
from ..util.singletons import register_singleton

logger = logging.getLogger(__name__)


@dataclass
class ToolActivityEntry:
    """A single recorded tool invocation."""

    id: str = ""
    session_id: str = ""
    tool: str = ""
    call_id: str = ""
    category: str = ""  # sdk | custom | mcp | skill
    arguments: str = ""
    result: str = ""
    status: str = ""  # started | completed | denied | error
    timestamp: float = 0.0
    duration_ms: float | None = None
    flagged: bool = False
    flag_reason: str = ""
    risk_score: int = 0  # 0-100 computed risk score
    risk_factors: list[str] = field(default_factory=list)
    model: str = ""  # which LLM model initiated this tool call
    interaction_type: str = ""  # "" | hitl | aitl | pitl | filter | deny
    shield_result: str = ""  # "" | clean | attack | error | not_configured
    shield_detail: str = ""  # human-readable detail from Content Safety API
    shield_elapsed_ms: float | None = None  # round-trip time for the shield call


_SUSPICIOUS_PATTERNS: list[tuple[str, int, str]] = [
    # (pattern, severity 1-100, description)
    ("rm -rf", 90, "Recursive forced deletion"),
    ("rm -r /", 100, "Root filesystem deletion"),
    ("DROP TABLE", 85, "SQL table drop"),
    ("DELETE FROM", 60, "SQL mass deletion"),
    ("curl.*|.*sh", 80, "Remote code execution via curl"),
    ("wget.*|.*sh", 80, "Remote code execution via wget"),
    ("eval(", 75, "Dynamic code evaluation"),
    ("exec(", 75, "Dynamic code execution"),
    ("os.system", 70, "Shell command execution"),
    ("subprocess", 50, "Subprocess invocation"),
    ("chmod 777", 65, "World-writable permissions"),
    ("passwd", 55, "Password file access"),
    ("/etc/shadow", 90, "Shadow password file access"),
    ("env | grep", 45, "Environment variable enumeration"),
    ("printenv", 45, "Environment variable dump"),
    ("base64 -d", 60, "Base64 decode (potential obfuscation)"),
    (".ssh/", 70, "SSH directory access"),
    ("id_rsa", 85, "SSH private key access"),
    ("PRIVATE KEY", 95, "Private key exposure"),
    ("API_KEY", 50, "API key in arguments"),
    ("SECRET", 55, "Secret value in arguments"),
    ("TOKEN", 45, "Token value in arguments"),
    ("password", 50, "Password in arguments"),
    ("credentials", 55, "Credentials reference"),
    ("sudo ", 60, "Privilege escalation"),
    ("nc -l", 70, "Netcat listener (reverse shell)"),
    (">&/dev/tcp", 90, "Bash reverse shell"),
    ("/dev/tcp", 85, "Network device access"),
    ("mkfifo", 65, "Named pipe creation"),
    ("nmap", 55, "Network scanning"),
    ("sqlmap", 80, "SQL injection tool"),
    (".env", 40, "Environment file access"),
    ("aws configure", 50, "Cloud credential configuration"),
    ("gcloud auth", 50, "Cloud credential configuration"),
    ("az login", 40, "Azure CLI login"),
    ("docker run", 45, "Container execution"),
    ("kubectl exec", 55, "Kubernetes pod execution"),
]


def _check_suspicious(arguments: str, result: str) -> tuple[bool, str, int, list[str]]:
    """Check if a tool call looks suspicious based on arguments/result.

    Returns (flagged, primary_reason, risk_score, risk_factors).
    """
    text = f"{arguments} {result}".lower()
    factors: list[str] = []
    max_severity = 0
    primary_reason = ""
    for pattern, severity, description in _SUSPICIOUS_PATTERNS:
        if pattern.lower() in text:
            factors.append(description)
            if severity > max_severity:
                max_severity = severity
                primary_reason = f"Suspicious pattern: {pattern}"
    flagged = max_severity >= 40
    return flagged, primary_reason, max_severity, factors


class ToolActivityStore:
    """Append-only log of tool invocations for audit and review.

    Stores entries in a single JSON-lines file at ``data_dir/tool_activity.jsonl``.
    Also maintains an in-memory index for fast querying.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or cfg.data_dir / "tool_activity.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._entries: list[ToolActivityEntry] = []
        self._pending_starts: dict[str, ToolActivityEntry] = {}
        self._counter = 0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._lock:
            try:
                by_id: dict[str, ToolActivityEntry] = {}
                for line in self._path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    entry = ToolActivityEntry(**{
                        k: v for k, v in data.items()
                        if k in ToolActivityEntry.__dataclass_fields__
                    })
                    by_id[entry.id] = entry
                    self._counter = max(self._counter, int(entry.id.split("-")[-1] or "0"))
                self._entries = list(by_id.values())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("[tool_activity] failed to load: %s", exc)

    def _next_id(self) -> str:
        self._counter += 1
        return f"ta-{self._counter}"

    def _append(self, entry: ToolActivityEntry) -> None:
        with self._lock:
            self._entries.append(entry)
            with open(self._path, "a") as f:
                f.write(json.dumps(asdict(entry), default=str) + "\n")

    def record_start(
        self,
        session_id: str,
        tool: str,
        call_id: str,
        arguments: str = "",
        category: str = "",
        model: str = "",
        interaction_type: str = "",
    ) -> ToolActivityEntry:
        """Record the start of a tool invocation."""
        entry = ToolActivityEntry(
            id=self._next_id(),
            session_id=session_id,
            tool=tool,
            call_id=call_id,
            category=category or self._infer_category(tool),
            arguments=arguments,
            status="started",
            timestamp=time.time(),
            model=model,
            interaction_type=interaction_type,
        )
        flagged, reason, risk, factors = _check_suspicious(arguments, "")
        entry.flagged = flagged
        entry.flag_reason = reason
        entry.risk_score = risk
        entry.risk_factors = factors
        self._pending_starts[call_id] = entry
        self._append(entry)
        return entry

    def update_shield_result(
        self,
        call_id: str,
        shield_result: str,
        shield_detail: str = "",
        shield_elapsed_ms: float | None = None,
    ) -> None:
        """Attach Content Safety shield results to a pending tool entry."""
        pending = self._pending_starts.get(call_id)
        if pending:
            pending.shield_result = shield_result
            pending.shield_detail = shield_detail
            pending.shield_elapsed_ms = shield_elapsed_ms

    def record_complete(
        self,
        call_id: str,
        result: str = "",
        status: str = "completed",
    ) -> ToolActivityEntry | None:
        """Record the completion of a tool invocation."""
        pending = self._pending_starts.pop(call_id, None)
        if pending:
            pending.result = result[:2000] if result else ""
            pending.status = status
            pending.duration_ms = (time.time() - pending.timestamp) * 1000
            flagged, reason, risk, factors = _check_suspicious(pending.arguments, result)
            if flagged and not pending.flagged:
                pending.flagged = True
                pending.flag_reason = reason
            if risk > pending.risk_score:
                pending.risk_score = risk
            pending.risk_factors = list(set(pending.risk_factors + factors))
            # Update the in-memory entry (already appended)
            # Append a completion record so the file has the full story
            completion = ToolActivityEntry(
                id=pending.id,
                session_id=pending.session_id,
                tool=pending.tool,
                call_id=call_id,
                category=pending.category,
                arguments=pending.arguments,
                result=pending.result,
                status=status,
                timestamp=pending.timestamp,
                duration_ms=pending.duration_ms,
                flagged=pending.flagged,
                flag_reason=pending.flag_reason,
                model=pending.model,
                interaction_type=pending.interaction_type,
                shield_result=pending.shield_result,
                shield_detail=pending.shield_detail,
                shield_elapsed_ms=pending.shield_elapsed_ms,
            )
            # Replace the in-memory start entry with completed version
            with self._lock:
                for i, e in enumerate(self._entries):
                    if e.id == pending.id:
                        self._entries[i] = completion
                        break
                with open(self._path, "a") as f:
                    f.write(json.dumps(asdict(completion), default=str) + "\n")
            return completion
        return None

    def query(
        self,
        *,
        session_id: str = "",
        tool: str = "",
        category: str = "",
        status: str = "",
        flagged_only: bool = False,
        since: float = 0,
        model: str = "",
        interaction_type: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Query tool activity with filters."""
        with self._lock:
            # Deduplicate: keep the latest version of each entry id
            by_id: dict[str, ToolActivityEntry] = {}
            for e in self._entries:
                by_id[e.id] = e
            entries = sorted(by_id.values(), key=lambda e: e.timestamp, reverse=True)

        # Apply filters
        if session_id:
            entries = [e for e in entries if e.session_id == session_id]
        if tool:
            entries = [e for e in entries if tool.lower() in e.tool.lower()]
        if category:
            entries = [e for e in entries if e.category == category]
        if status:
            entries = [e for e in entries if e.status == status]
        if flagged_only:
            entries = [e for e in entries if e.flagged]
        if since > 0:
            entries = [e for e in entries if e.timestamp >= since]
        if model:
            entries = [e for e in entries if model.lower() in e.model.lower()]
        if interaction_type:
            entries = [e for e in entries if e.interaction_type == interaction_type]

        total = len(entries)
        page = entries[offset : offset + limit]

        return {
            "entries": [asdict(e) for e in page],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def get_summary(self) -> dict[str, Any]:
        """Get aggregate statistics about tool activity."""
        with self._lock:
            by_id: dict[str, ToolActivityEntry] = {}
            for e in self._entries:
                by_id[e.id] = e
            entries = list(by_id.values())

        total = len(entries)
        flagged = sum(1 for e in entries if e.flagged)
        by_tool: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_session: dict[str, int] = {}
        by_model: dict[str, int] = {}
        by_interaction_type: dict[str, int] = {}
        durations: list[float] = []
        risk_scores: list[int] = []
        for e in entries:
            by_tool[e.tool] = by_tool.get(e.tool, 0) + 1
            by_category[e.category] = by_category.get(e.category, 0) + 1
            by_status[e.status] = by_status.get(e.status, 0) + 1
            by_session[e.session_id] = by_session.get(e.session_id, 0) + 1
            if e.model:
                by_model[e.model] = by_model.get(e.model, 0) + 1
            if e.interaction_type:
                by_interaction_type[e.interaction_type] = (
                    by_interaction_type.get(e.interaction_type, 0) + 1
                )
            if e.duration_ms is not None:
                durations.append(e.duration_ms)
            if e.risk_score > 0:
                risk_scores.append(e.risk_score)

        # Top tools sorted by count
        top_tools = sorted(by_tool.items(), key=lambda x: x[1], reverse=True)[:20]

        # Duration stats
        avg_duration = sum(durations) / len(durations) if durations else 0
        max_duration = max(durations) if durations else 0
        p95_duration = sorted(durations)[int(len(durations) * 0.95)] if durations else 0

        # Risk distribution
        high_risk = sum(1 for s in risk_scores if s >= 70)
        medium_risk = sum(1 for s in risk_scores if 40 <= s < 70)
        low_risk = sum(1 for s in risk_scores if 0 < s < 40)

        return {
            "total": total,
            "flagged": flagged,
            "by_tool": dict(top_tools),
            "by_category": by_category,
            "by_status": by_status,
            "by_model": by_model,
            "by_interaction_type": by_interaction_type,
            "sessions_with_activity": len(by_session),
            "avg_duration_ms": round(avg_duration, 1),
            "max_duration_ms": round(max_duration, 1),
            "p95_duration_ms": round(p95_duration, 1),
            "risk_high": high_risk,
            "risk_medium": medium_risk,
            "risk_low": low_risk,
        }

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        """Get a single entry by ID."""
        with self._lock:
            for e in reversed(self._entries):
                if e.id == entry_id:
                    return asdict(e)
        return None

    def flag_entry(self, entry_id: str, reason: str = "") -> bool:
        """Manually flag an entry as suspicious."""
        with self._lock:
            for e in self._entries:
                if e.id == entry_id:
                    e.flagged = True
                    e.flag_reason = reason or "Manually flagged"
                    e.risk_score = max(e.risk_score, 50)
                    if "Manual review" not in e.risk_factors:
                        e.risk_factors.append("Manual review")
                    with open(self._path, "a") as f:
                        f.write(json.dumps(asdict(e), default=str) + "\n")
                    return True
        return False

    def unflag_entry(self, entry_id: str) -> bool:
        """Remove flag from an entry."""
        with self._lock:
            for e in self._entries:
                if e.id == entry_id:
                    e.flagged = False
                    e.flag_reason = ""
                    with open(self._path, "a") as f:
                        f.write(json.dumps(asdict(e), default=str) + "\n")
                    return True
        return False

    def get_timeline(
        self,
        *,
        bucket_minutes: int = 60,
        since: float = 0,
        until: float = 0,
    ) -> list[dict[str, Any]]:
        """Return tool call counts bucketed by time interval."""
        with self._lock:
            by_id: dict[str, ToolActivityEntry] = {}
            for e in self._entries:
                by_id[e.id] = e
            entries = list(by_id.values())

        if not entries:
            return []

        if not since:
            since = min(e.timestamp for e in entries)
        if not until:
            until = time.time()

        bucket_secs = bucket_minutes * 60
        buckets: dict[int, dict[str, int]] = {}

        for e in entries:
            if e.timestamp < since or e.timestamp > until:
                continue
            bucket_ts = int(e.timestamp // bucket_secs) * bucket_secs
            if bucket_ts not in buckets:
                buckets[bucket_ts] = {"total": 0, "flagged": 0, "sdk": 0, "mcp": 0, "custom": 0, "skill": 0}
            buckets[bucket_ts]["total"] += 1
            if e.flagged:
                buckets[bucket_ts]["flagged"] += 1
            if e.category in buckets[bucket_ts]:
                buckets[bucket_ts][e.category] += 1

        return [
            {"timestamp": ts, **counts}
            for ts, counts in sorted(buckets.items())
        ]

    def get_session_breakdown(self) -> list[dict[str, Any]]:
        """Return per-session aggregation for the session-level audit view."""
        with self._lock:
            by_id: dict[str, ToolActivityEntry] = {}
            for e in self._entries:
                by_id[e.id] = e
            entries = list(by_id.values())

        sessions: dict[str, dict[str, Any]] = {}
        for e in entries:
            sid = e.session_id
            if sid not in sessions:
                sessions[sid] = {
                    "session_id": sid,
                    "tool_count": 0,
                    "flagged_count": 0,
                    "max_risk": 0,
                    "categories": set(),
                    "tools_used": set(),
                    "models_used": set(),
                    "first_ts": e.timestamp,
                    "last_ts": e.timestamp,
                    "total_duration_ms": 0.0,
                }
            s = sessions[sid]
            s["tool_count"] += 1
            if e.flagged:
                s["flagged_count"] += 1
            s["max_risk"] = max(s["max_risk"], e.risk_score)
            s["categories"].add(e.category)
            s["tools_used"].add(e.tool)
            if e.model:
                s["models_used"].add(e.model)
            s["first_ts"] = min(s["first_ts"], e.timestamp)
            s["last_ts"] = max(s["last_ts"], e.timestamp)
            if e.duration_ms:
                s["total_duration_ms"] += e.duration_ms

        result = []
        for s in sessions.values():
            result.append({
                "session_id": s["session_id"],
                "tool_count": s["tool_count"],
                "flagged_count": s["flagged_count"],
                "max_risk": s["max_risk"],
                "categories": sorted(s["categories"]),
                "unique_tools": len(s["tools_used"]),
                "models": sorted(s["models_used"]),
                "first_activity": s["first_ts"],
                "last_activity": s["last_ts"],
                "total_duration_ms": round(s["total_duration_ms"], 1),
            })
        result.sort(key=lambda x: x["last_activity"], reverse=True)
        return result

    def export_csv(self, **filters: Any) -> str:
        """Export filtered entries as CSV string."""
        data = self.query(**filters, limit=10000)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "timestamp", "session_id", "tool", "category",
            "model", "status", "interaction_type", "duration_ms", "risk_score", "flagged",
            "flag_reason", "shield_result", "shield_detail", "shield_elapsed_ms",
            "arguments", "result",
        ])
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

    @staticmethod
    def _infer_category(tool: str) -> str:
        """Infer tool category from the tool name."""
        sdk_tools = {"create", "edit", "view", "grep", "glob", "run", "bash"}
        if tool.lower() in sdk_tools:
            return "sdk"
        if "__" in tool or "." in tool or tool.startswith("mcp_"):
            return "mcp"
        return "custom"

    def import_from_sessions(self, session_store: object) -> int:
        """Backfill tool activity from existing session data."""
        from .session_store import SessionStore

        if not isinstance(session_store, SessionStore):
            return 0

        existing_ids: set[str] = set()
        with self._lock:
            existing_ids = {f"{e.session_id}:{e.call_id}" for e in self._entries}

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
                        id=self._next_id(),
                        session_id=sid,
                        tool=tc.get("name", "unknown"),
                        call_id="",
                        category=self._infer_category(tc.get("name", "")),
                        arguments=tc.get("arguments", ""),
                        result=tc.get("result", "")[:2000],
                        status="completed",
                        timestamp=msg.get("timestamp", 0),
                    )
                    flagged, reason, risk, factors = _check_suspicious(entry.arguments, entry.result)
                    entry.flagged = flagged
                    entry.flag_reason = reason
                    entry.risk_score = risk
                    entry.risk_factors = factors
                    self._append(entry)
                    existing_ids.add(key)
                    count += 1

        logger.info("[tool_activity] imported %d entries from sessions", count)
        return count


# -- Singleton access ------------------------------------------------------

_instance: ToolActivityStore | None = None


def get_tool_activity_store() -> ToolActivityStore:
    """Return the global ToolActivityStore singleton."""
    global _instance
    if _instance is None:
        _instance = ToolActivityStore()
    return _instance


def _reset_tool_activity_store() -> None:
    global _instance
    _instance = None


register_singleton(_reset_tool_activity_store)
