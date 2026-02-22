"""Azure CLI wrapper."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from time import sleep
from time import time as _time
from typing import Any

from ..config.settings import cfg
from ..util.result import Result

logger = logging.getLogger(__name__)


class AzureCLI:
    """Thin wrapper around ``az`` with JSON output parsing."""

    CACHE_TTL = 30
    HEARTBEAT_INTERVAL = 15
    TIMEOUT = 1200

    def __init__(self) -> None:
        self.last_stderr: str = ""
        self._cache: dict[str, tuple[float, Any]] = {}

    def _run(self, cmd: list[str], cmd_summary: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "yes_without_prompt"}
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        t0 = _time()
        next_heartbeat = t0 + self.HEARTBEAT_INTERVAL

        while True:
            try:
                proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                now = _time()
                if now >= next_heartbeat:
                    elapsed = now - t0
                    mins_e, secs_e = divmod(int(elapsed), 60)
                    if self.TIMEOUT:
                        remaining = max(0, self.TIMEOUT - elapsed)
                        mins_r, secs_r = divmod(int(remaining), 60)
                        logger.info(
                            "[az] %dm %02ds elapsed | timeout %dm %02ds | az %s",
                            mins_e, secs_e,
                            mins_r, secs_r,
                            cmd_summary,
                        )
                    else:
                        logger.info("[az] still waiting (%.0fs): az %s", elapsed, cmd_summary)
                    next_heartbeat = now + self.HEARTBEAT_INTERVAL
                if self.TIMEOUT and (now - t0) > self.TIMEOUT:
                    proc.kill()
                    proc.wait()
                    logger.error("[az] TIMEOUT after %ds: az %s", self.TIMEOUT, cmd_summary)
                    return subprocess.CompletedProcess(
                        cmd, returncode=-1,
                        stdout=proc.stdout.read() if proc.stdout else "",
                        stderr=f"Timed out after {self.TIMEOUT}s",
                    )

        return subprocess.CompletedProcess(
            cmd, returncode=proc.returncode,
            stdout=proc.stdout.read() if proc.stdout else "",
            stderr=proc.stderr.read() if proc.stderr else "",
        )

    def json(self, *args: str, quiet: bool = False) -> dict | list | None:
        cmd_summary = " ".join(args[:5])
        _log = logger.debug if quiet else logger.info
        _log("[az] starting: az %s", cmd_summary)
        t0 = _time()
        result = self._run(["az", *args, "--output", "json"], cmd_summary)
        elapsed = _time() - t0
        self.last_stderr = result.stderr.strip()
        if result.returncode != 0:
            logger.warning(
                "[az] FAILED (%.1fs, rc=%d): az %s -- %s",
                elapsed, result.returncode, cmd_summary, self.last_stderr[:800],
            )
            return None
        _log("[az] OK (%.1fs): az %s", elapsed, cmd_summary)
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            logger.warning("[az] could not parse JSON output for: az %s", cmd_summary)
            return None

    def json_cached(self, *args: str, ttl: int | None = None) -> dict | list | None:
        ttl = ttl if ttl is not None else self.CACHE_TTL
        key = " ".join(args)
        cached = self._cache.get(key)
        if cached is not None:
            expiry, value = cached
            if _time() < expiry:
                logger.debug("[az] cache hit (ttl %ds): az %s", ttl, key)
                return value
        result = self.json(*args, quiet=True)
        self._cache[key] = (_time() + ttl, result)
        return result

    def invalidate_cache(self, *args: str) -> None:
        if args:
            self._cache.pop(" ".join(args), None)
        else:
            self._cache.clear()

    def ok(self, *args: str) -> Result:
        cmd_summary = " ".join(args[:5])
        logger.info("[az] starting: az %s", cmd_summary)
        t0 = _time()
        result = self._run(["az", *args], cmd_summary)
        elapsed = _time() - t0
        success = result.returncode == 0
        if success:
            logger.info("[az] OK (%.1fs): az %s", elapsed, cmd_summary)
        else:
            logger.warning(
                "[az] FAILED (%.1fs, rc=%d): az %s -- %s",
                elapsed, result.returncode, cmd_summary, result.stderr.strip()[:300],
            )
        return Result(success=success, message=result.stderr.strip())

    def account_info(self) -> dict[str, Any] | None:
        account = self.json_cached("account", "show")
        return account if isinstance(account, dict) else None

    def login_device_code(self) -> dict[str, Any]:
        proc = subprocess.Popen(
            ["az", "login", "--use-device-code"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        output_lines: list[str] = []
        start = _time()
        while _time() - start < 10:
            line = proc.stderr.readline().decode("utf-8", errors="replace")
            if line:
                output_lines.append(line.strip())
                if "microsoft.com/devicelogin" in line.lower() or "code" in line.lower():
                    break
            if proc.poll() is not None:
                break
            sleep(0.2)

        output = "\n".join(output_lines)
        code_match = re.search(r"code\s+([A-Z0-9]{6,12})", output, re.IGNORECASE)
        url_match = re.search(r"(https://\S+devicelogin\S*)", output, re.IGNORECASE)
        return {
            "message": output,
            "code": code_match.group(1) if code_match else None,
            "url": url_match.group(1) if url_match else "https://microsoft.com/devicelogin",
            "pid": proc.pid,
        }

    def get_bot_endpoint(self) -> str | None:
        """Read the messaging endpoint URL from the deployed Azure Bot Service."""
        rg = cfg.env.read("BOT_RESOURCE_GROUP")
        name = cfg.env.read("BOT_NAME")
        if not (rg and name):
            return None
        bot = self.json("bot", "show", "--resource-group", rg, "--name", name, quiet=True)
        if not bot:
            return None
        endpoint = (bot.get("properties") or {}).get("endpoint") or ""
        return endpoint or None

    def update_endpoint(self, endpoint: str) -> Result:
        rg = cfg.env.read("BOT_RESOURCE_GROUP")
        name = cfg.env.read("BOT_NAME")
        if not (rg and name):
            return Result.fail("Bot not configured")
        bot = self.json("bot", "show", "--resource-group", rg, "--name", name)
        if not bot:
            return Result.fail("Bot resource not found")
        # NOTE: `az bot update --endpoint` silently succeeds without
        # persisting the messaging endpoint.  Use `az resource update`
        # to patch the ARM resource directly -- this reliably updates
        # the endpoint that channels actually use.
        result = self.json(
            "resource", "update",
            "--resource-group", rg,
            "--name", name,
            "--resource-type", "Microsoft.BotService/botServices",
            "--set", f"properties.endpoint={endpoint}",
        )
        if not result:
            return Result.fail(f"Endpoint update failed: {self.last_stderr}")
        return Result.ok("Endpoint updated")

    def get_channels(self) -> dict[str, bool]:
        rg = cfg.env.read("BOT_RESOURCE_GROUP")
        name = cfg.env.read("BOT_NAME")
        if not (rg and name):
            return {}
        bot_info = self.json("bot", "show", "--resource-group", rg, "--name", name)
        if bot_info:
            props = bot_info.get("properties", {})
            configured = props.get("configuredChannels") or props.get("enabledChannels") or []
            return {"telegram": "telegram" in configured}
        channels: dict[str, bool] = {}
        for ch in ("telegram",):
            info = self.json("bot", ch, "show", "--resource-group", rg, "--name", name)
            channels[ch] = info is not None
        return channels

    _TG_RETRIES = 3
    _TG_RETRY_DELAY = 2  # seconds
    # HTTP codes that warrant a retry (transient / rate-limit).
    # Note: 404 is NOT retryable -- on /getMe it means the bot doesn't exist.
    _TG_RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})

    @staticmethod
    def validate_telegram_token(token: str, *, _retries: int = 0) -> Result:
        token = token.strip()
        if not token:
            return Result.fail("Telegram token is empty")
        if token.startswith("@kv:"):
            return Result.fail(
                "Telegram token looks like an unresolved Key Vault reference "
                "-- is Key Vault configured?"
            )
        retries = _retries or AzureCLI._TG_RETRIES
        url = f"https://api.telegram.org/bot{token}/getMe"
        logger.debug("Validating Telegram token (len=%d, prefix=%s...)", len(token), token[:8])
        last_err = ""
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                    if data.get("ok"):
                        username = data.get("result", {}).get("username", "?")
                        return Result.ok(f"@{username}")
                    return Result.fail(data.get("description", "Unknown error from Telegram"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")
                try:
                    detail = json.loads(body).get("description", body)
                except (json.JSONDecodeError, ValueError):
                    detail = body
                last_err = f"Telegram API error {exc.code}: {detail}"
                if exc.code not in AzureCLI._TG_RETRYABLE_CODES or attempt == retries:
                    return Result.fail(last_err)
                logger.warning(
                    "Telegram getMe returned %s (attempt %d/%d), retrying...",
                    exc.code, attempt, retries,
                )
            except Exception as exc:
                last_err = f"Cannot reach Telegram API: {exc}"
                if attempt == retries:
                    return Result.fail(last_err)
                logger.warning(
                    "Telegram getMe failed (%s, attempt %d/%d), retrying...",
                    exc, attempt, retries,
                )
            sleep(AzureCLI._TG_RETRY_DELAY)
        return Result.fail(last_err)  # pragma: no cover

    def configure_telegram(self, token: str, *, validated_name: str = "") -> Result:
        # Allow callers that already validated the token to skip the
        # redundant HTTP round-trip (and avoid a second transient failure).
        if validated_name:
            display = validated_name
        else:
            tok_result = self.validate_telegram_token(token)
            if not tok_result:
                return Result.fail(f"Invalid Telegram token: {tok_result.message}")
            display = tok_result.message
        logger.info("Telegram token validated: %s", display)
        rg = cfg.env.read("BOT_RESOURCE_GROUP")
        name = cfg.env.read("BOT_NAME")
        if not (rg and name):
            return Result.fail("Bot not deployed")
        self.ok("bot", "telegram", "delete", "--resource-group", rg, "--name", name)
        result = self.ok(
            "bot", "telegram", "create", "--resource-group", rg, "--name", name,
            "--access-token", token, "--is-validated",
        )
        return Result.ok(f"Telegram configured ({display})") if result else result

    def remove_channel(self, channel: str) -> Result:
        rg = cfg.env.read("BOT_RESOURCE_GROUP")
        name = cfg.env.read("BOT_NAME")
        if not (rg and name):
            return Result.fail("Bot not deployed")
        result = self.ok("bot", channel, "delete", "--resource-group", rg, "--name", name)
        return Result.ok(f"{channel} removed") if result else result
