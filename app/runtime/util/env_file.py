"""Thread-safe ``.env`` file reader/writer."""

from __future__ import annotations

import threading
from pathlib import Path


class EnvFile:
    """Reads and writes a simple ``KEY=VALUE`` file with thread safety."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def read(self, key: str) -> str:
        """Return the value for *key*, or ``""`` if absent."""
        return self.read_all().get(key, "")

    def read_all(self) -> dict[str, str]:
        """Parse the env file into a ``{key: value}`` mapping."""
        if not self.path.exists():
            return {}
        result: dict[str, str] = {}
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip('"').strip("'")
        return result

    def write(self, **kwargs: str) -> None:
        """Merge *kwargs* into the env file, preserving existing entries.

        Values are wrapped in double-quotes so that ``bash source`` handles
        special characters (``~``, ``!``, ``$``, spaces, etc.) safely.
        """
        with self._lock:
            existing = self.read_all()
            existing.update(kwargs)
            lines = [
                f'{k}="{v}"' for k, v in sorted(existing.items()) if v
            ]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(lines) + "\n")
