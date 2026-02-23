"""GitHub CLI authentication service."""

from __future__ import annotations

import logging
import os
import re
import selectors
import subprocess
from time import time as _time
from typing import Any

logger = logging.getLogger(__name__)


class GitHubAuth:
    """Manages authentication via ``gh auth``."""

    def __init__(self) -> None:
        self._login_proc: subprocess.Popen | None = None

    def status(self) -> dict[str, Any]:
        from ...config.settings import cfg

        if cfg.github_token:
            return {"authenticated": True, "details": "Using GITHUB_TOKEN from environment"}
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            output = (result.stdout + "\n" + result.stderr).strip()
            return {"authenticated": result.returncode == 0, "details": output}
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {"authenticated": False, "details": "gh CLI not available"}

    def start_login(self) -> tuple[str, dict[str, Any]]:
        try:
            proc = subprocess.Popen(
                ["gh", "auth", "login", "--hostname", "github.com",
                 "--web", "--git-protocol", "https"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                env={**os.environ, "GH_PROMPT_DISABLED": "1"},
            )
            self._login_proc = proc
            code, url, output = self._read_device_code(proc)
            return "login_started", {
                "message": output or "GitHub login initiated.",
                "code": code,
                "url": url or "https://github.com/login/device",
                "pid": proc.pid,
            }
        except FileNotFoundError:
            return "error", {"message": "gh CLI not found."}

    def extract_token(self) -> str | None:
        """Return the current ``gh`` OAuth token, or *None*."""
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=10,
            )
            token = result.stdout.strip()
            return token if result.returncode == 0 and token else None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    @staticmethod
    def _read_device_code(
        proc: subprocess.Popen,
    ) -> tuple[str | None, str | None, str]:
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)  # type: ignore[arg-type]
        lines: list[str] = []
        code: str | None = None
        url: str | None = None
        start = _time()

        while _time() - start < 15:
            for key, _ in sel.select(timeout=0.5):
                raw = key.fileobj.readline().decode("utf-8", errors="replace")  # type: ignore[union-attr]
                if not raw:
                    continue
                stripped = raw.strip()
                lines.append(stripped)
                m_code = re.search(r"one-time code:\s*(\S+)", stripped, re.IGNORECASE)
                if m_code:
                    code = m_code.group(1)
                m_url = re.search(r"(https://github\.com/login/device\S*)", stripped)
                if m_url:
                    url = m_url.group(1)
            if proc.poll() is not None:
                for raw_line in proc.stdout:  # type: ignore[union-attr]
                    lines.append(raw_line.decode("utf-8", errors="replace").strip())
                break
            if code and url:
                break

        sel.close()
        return code, url, "\n".join(lines)
