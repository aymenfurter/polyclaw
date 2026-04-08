"""Agent profile -- personality, preferences, and usage tracking."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..config.settings import cfg
from ..util.singletons import register_singleton

_DEFAULT_PROFILE: dict[str, Any] = {
    "name": "polyclaw",
    "emoji": "",
    "location": "",
    "emotional_state": "neutral",
    "preferences": {},
}


def _load_json(path: Path, default: Any = None) -> Any:
    """Load a JSON file, returning *default* on any error."""
    if default is None:
        default = {}
    if not path.exists():
        return default() if callable(default) else (dict(default) if isinstance(default, dict) else list(default) if isinstance(default, list) else default)
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default() if callable(default) else (dict(default) if isinstance(default, dict) else list(default) if isinstance(default, list) else default)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def profile_path() -> Path:
    """Return the path to the agent profile JSON file."""
    return cfg.data_dir / "agent_profile.json"


def _usage_path() -> Path:
    return cfg.data_dir / "skill_usage.json"


def _interactions_path() -> Path:
    return cfg.data_dir / "interactions.json"


def load_profile() -> dict[str, Any]:
    data = _load_json(profile_path(), _DEFAULT_PROFILE)
    for key, default in _DEFAULT_PROFILE.items():
        data.setdefault(key, default)
    return data


def save_profile(profile: dict[str, Any]) -> None:
    _write_json(profile_path(), profile)


def load_skill_usage() -> dict[str, int]:
    return _load_json(_usage_path(), {})


def increment_skill_usage(skill_name: str) -> None:
    usage = load_skill_usage()
    usage[skill_name] = usage.get(skill_name, 0) + 1
    _write_json(_usage_path(), usage)


def log_interaction(interaction_type: str, channel: str = "") -> None:
    path = _interactions_path()
    interactions = _load_json(path, [])
    interactions.append({
        "type": interaction_type,
        "channel": channel,
        "timestamp": time.time(),
    })
    _write_json(path, interactions[-1000:])


def load_interactions() -> list[dict[str, Any]]:
    """Load the raw interaction log."""
    return _load_json(_interactions_path(), [])


def get_contributions(days: int = 365) -> list[dict[str, Any]]:
    """Aggregate interactions into per-day contribution counts.

    Returns a list of ``{"date": "YYYY-MM-DD", "user": N, "scheduled": N}``
    covering the last *days* days.
    """
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone

    interactions = load_interactions()
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)

    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"user": 0, "scheduled": 0})
    for entry in interactions:
        d = _parse_interaction_date(entry)
        if d is None or d < start:
            continue
        key = d.isoformat()
        itype = entry.get("type", "user")
        if itype == "scheduled":
            buckets[key]["scheduled"] += 1
        else:
            buckets[key]["user"] += 1

    result: list[dict[str, Any]] = []
    cursor = start
    while cursor <= today:
        ds = cursor.isoformat()
        counts = buckets.get(ds, {"user": 0, "scheduled": 0})
        result.append({"date": ds, "user": counts["user"], "scheduled": counts["scheduled"]})
        cursor += timedelta(days=1)
    return result


def _parse_interaction_date(entry: dict[str, Any]) -> Any:
    """Parse an interaction's timestamp to a date, or return None."""
    from datetime import datetime, timezone

    ts = entry.get("timestamp")
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        return datetime.fromisoformat(str(ts)).date()
    except (ValueError, OSError):
        return None


def get_activity_stats() -> dict[str, Any]:
    """Compute summary activity statistics from interactions."""
    from datetime import datetime, timedelta, timezone

    interactions = load_interactions()
    now = datetime.now(timezone.utc)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())

    today_count = 0
    week_count = 0
    month_count = 0
    active_days: set[str] = set()

    for entry in interactions:
        d = _parse_interaction_date(entry)
        if d is None:
            continue
        active_days.add(d.isoformat())
        if d == today:
            today_count += 1
        if d >= week_start:
            week_count += 1
        if d.year == today.year and d.month == today.month:
            month_count += 1

    # Calculate current streak (consecutive days ending today or yesterday)
    streak = 0
    check = today
    if check.isoformat() not in active_days:
        check = today - timedelta(days=1)
    while check.isoformat() in active_days:
        streak += 1
        check -= timedelta(days=1)

    return {
        "total": len(interactions),
        "today": today_count,
        "this_week": week_count,
        "this_month": month_count,
        "streak": streak,
    }


def get_full_profile() -> dict[str, Any]:
    profile = load_profile()
    profile["skill_usage"] = load_skill_usage()
    profile["contributions"] = get_contributions()
    profile["activity_stats"] = get_activity_stats()
    return profile


def _reset() -> None:
    pass  # stateless -- config paths will change on Settings reset


register_singleton(_reset)
