"""Base class for JSON-file-backed stores."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonStore:
    """Thread-safe JSON file reader/writer."""

    def __init__(self, path: Path, default: Any = None) -> None:
        self._path = path
        self._default = default if default is not None else {}
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Any:
        if not self._path.exists():
            return self._default_copy()
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load %s: %s", self._path, exc, exc_info=True)
            return self._default_copy()

    def save(self, data: Any) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, indent=2, default=str) + "\n")

    def _default_copy(self) -> Any:
        if isinstance(self._default, dict):
            return dict(self._default)
        if isinstance(self._default, list):
            return list(self._default)
        return self._default
