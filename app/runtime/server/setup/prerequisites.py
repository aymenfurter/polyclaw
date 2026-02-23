"""Infrastructure prerequisites routes -- /api/setup/prerequisites/*."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import web

from ...config.settings import SECRET_ENV_KEYS, cfg
from ...services.cloud.azure import AzureCLI
from ...services.keyvault import env_key_to_secret_name, is_kv_ref
from ...services.keyvault import kv as _kv
from ...state.deploy_state import DeployStateStore
from ...state.infra_config import InfraConfigStore
from ...util.async_helpers import run_sync

logger = logging.getLogger(__name__)

_DEFAULT_PREREQ_RG = "polyclaw-prereq-rg"


class PrerequisitesRoutes:
    """/api/setup/prerequisites/* endpoint handlers."""

    def __init__(
        self,
        az: AzureCLI,
        store: InfraConfigStore,
        deploy_store: DeployStateStore | None = None,
    ) -> None:
        self._az = az
        self._store = store
        self._deploy_store = deploy_store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/setup/prerequisites/status", self.status)
        router.add_post("/api/setup/prerequisites/deploy", self.deploy)

    async def status(self, _req: web.Request) -> web.Response:
        kv_url = cfg.env.read("KEY_VAULT_URL") or ""
        result: dict[str, Any] = {
            "keyvault": {
                "configured": _kv.enabled,
                "url": kv_url if _kv.enabled else None,
                "name": cfg.env.read("KEY_VAULT_NAME") or None,
                "resource_group": cfg.env.read("KEY_VAULT_RG") or None,
            },
        }
        if _kv.enabled:
            try:
                _kv.list_secrets()
                result["keyvault"]["reachable"] = True
            except Exception as exc:
                result["keyvault"]["reachable"] = False
                result["keyvault"]["error"] = str(exc)[:200]
        return web.json_response(result)

    async def deploy(self, req: web.Request) -> web.Response:
        body = await req.json() if req.can_read_body else {}
        location = body.get("location", "eastus").strip()
        prereq_rg = body.get("resource_group", "").strip() or _DEFAULT_PREREQ_RG

        steps: list[dict[str, Any]] = []

        if _kv.enabled:
            self._link_existing_keyvault()
            steps.append({
                "step": "keyvault", "status": "ok",
                "detail": f"Already configured: {_kv.url}",
            })
            try:
                migrated = await run_sync(self._migrate_existing_secrets)
                if migrated:
                    steps.append({
                        "step": "migrate_secrets", "status": "ok",
                        "detail": f"Migrated {migrated} secret(s)",
                    })
            except Exception as exc:
                logger.warning("Migration failed: %s", exc)
                steps.append({
                    "step": "migrate_secrets", "status": "warning",
                    "detail": "Migration failed -- try again shortly",
                })
            return web.json_response({
                "status": "ok", "steps": steps,
                "message": "Key Vault already configured",
            })

        if not await self._ensure_rg(prereq_rg, location, steps):
            return _fail(steps)

        vault_url = await self._create_keyvault(prereq_rg, location, steps)
        if not vault_url:
            return _fail(steps)

        await self._assign_role(prereq_rg, steps)

        try:
            migrated = await run_sync(self._migrate_existing_secrets)
            if migrated:
                steps.append({
                    "step": "migrate_secrets", "status": "ok",
                    "detail": f"Migrated {migrated} secret(s)",
                })
        except Exception as exc:
            logger.warning("Migration failed (RBAC may be propagating): %s", exc)
            steps.append({
                "step": "migrate_secrets", "status": "warning",
                "detail": "RBAC propagating -- try again in a minute",
            })

        return web.json_response({
            "status": "ok", "steps": steps,
            "message": "Prerequisites deployed",
        })

    async def ensure_keyvault_ready(
        self, location: str = "eastus"
    ) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []

        if _kv.enabled:
            self._link_existing_keyvault()
            steps.append({
                "step": "keyvault", "status": "ok",
                "detail": f"Already configured: {_kv.url}",
            })
            await self._wait_for_access(steps)
            return steps

        prereq_rg = _DEFAULT_PREREQ_RG

        if not await self._ensure_rg(prereq_rg, location, steps):
            return steps

        vault_url = await self._create_keyvault(prereq_rg, location, steps)
        if not vault_url:
            return steps

        await self._assign_role(prereq_rg, steps)
        await self._wait_for_access(steps)
        return steps

    def _link_existing_keyvault(self) -> None:
        """Ensure the existing Key Vault resource is registered on the current deployment."""
        if not self._deploy_store:
            return
        rec = self._deploy_store.current_local()
        if not rec:
            return
        kv_name = cfg.env.read("KEY_VAULT_NAME") or ""
        kv_rg = cfg.env.read("KEY_VAULT_RG") or ""
        if not kv_name:
            return
        # Skip if already tracked on this deployment
        for r in rec.resources:
            if r.resource_type == "keyvault" and r.resource_name == kv_name:
                return
        rec.add_resource(
            resource_type="keyvault", resource_group=kv_rg,
            resource_name=kv_name, purpose="Secret storage",
        )
        if kv_rg and kv_rg not in rec.resource_groups:
            rec.resource_groups.append(kv_rg)
        self._deploy_store.update(rec)

    async def _wait_for_access(
        self,
        steps: list[dict[str, Any]],
        max_retries: int = 6,
        initial_wait: float = 10.0,
    ) -> bool:
        wait = initial_wait
        for attempt in range(max_retries):
            try:
                await run_sync(_kv.list_secrets)
                steps.append({
                    "step": "rbac_verify", "status": "ok",
                    "detail": "Access verified",
                })
                return True
            except Exception:
                if attempt < max_retries - 1:
                    logger.info(
                        "RBAC propagation (%d/%d), waiting %.0fs...",
                        attempt + 1, max_retries, wait,
                    )
                    await asyncio.sleep(wait)
                    wait = min(wait * 2, 60.0)

        steps.append({
            "step": "rbac_verify", "status": "warning",
            "detail": "RBAC slow -- try again shortly",
        })
        return False

    async def _ensure_rg(
        self, rg: str, location: str, steps: list[dict]
    ) -> bool:
        existing = await run_sync(self._az.json, "group", "show", "--name", rg)
        if existing:
            steps.append({
                "step": "resource_group", "status": "ok",
                "detail": f"{rg} (existing)",
            })
            return True

        tag_args: list[str] = []
        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                tag_args = ["--tags", f"polyclaw_deploy={rec.tag}"]

        result = await run_sync(
            self._az.json,
            "group", "create", "--name", rg, "--location", location, *tag_args,
        )
        ok = bool(result)
        steps.append({
            "step": "resource_group",
            "status": "ok" if ok else "failed",
            "detail": rg if ok else (self._az.last_stderr or "Unknown error"),
        })
        if ok and self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec and rg not in rec.resource_groups:
                rec.resource_groups.append(rg)
                self._deploy_store.update(rec)
        return ok

    async def _create_keyvault(
        self, rg: str, location: str, steps: list[dict]
    ) -> str:
        import secrets as _secrets

        vault_name = f"polyclaw-kv-{_secrets.token_hex(4)}"
        result = await run_sync(
            self._az.json,
            "keyvault", "create",
            "--name", vault_name, "--resource-group", rg,
            "--location", location, "--enable-rbac-authorization", "true",
        )
        if not result:
            steps.append({
                "step": "keyvault", "status": "failed",
                "detail": self._az.last_stderr or "Unknown error",
            })
            return ""

        vault_url = ""
        if isinstance(result, dict):
            vault_url = result.get("properties", {}).get("vaultUri", "")
        if not vault_url:
            vault_url = f"https://{vault_name}.vault.azure.net"

        cfg.write_env(
            KEY_VAULT_URL=vault_url,
            KEY_VAULT_NAME=vault_name,
            KEY_VAULT_RG=rg,
        )
        _kv.reinit()

        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                rec.add_resource(
                    resource_type="keyvault", resource_group=rg,
                    resource_name=vault_name, purpose="Secret storage",
                )
                self._deploy_store.update(rec)

        steps.append({"step": "keyvault", "status": "ok", "detail": vault_url})
        return vault_url

    async def _assign_role(self, rg: str, steps: list[dict]) -> None:
        account = self._az.account_info()
        if not account:
            steps.append({
                "step": "rbac_role", "status": "failed",
                "detail": "No Azure account",
            })
            return

        user_id = account.get("user", {}).get("name", "")
        kv_name = cfg.env.read("KEY_VAULT_NAME") or ""
        sub_id = account.get("id", "")
        if not (user_id and kv_name and sub_id):
            steps.append({
                "step": "rbac_role", "status": "failed",
                "detail": "Missing user/vault/subscription info",
            })
            return

        scope = (
            f"/subscriptions/{sub_id}/resourceGroups/{rg}"
            f"/providers/Microsoft.KeyVault/vaults/{kv_name}"
        )
        ok, msg = await run_sync(
            self._az.ok,
            "role", "assignment", "create",
            "--role", "Key Vault Secrets Officer",
            "--assignee", user_id, "--scope", scope,
        )
        steps.append({
            "step": "rbac_role",
            "status": "ok" if ok else "failed",
            "detail": f"Assigned to {user_id}" if ok else (msg or "Unknown error"),
        })

    def _migrate_existing_secrets(
        self, *, max_retries: int = 4, initial_wait: float = 10.0
    ) -> int:
        if not _kv.enabled:
            return 0

        migrated = 0
        env_data = cfg.env.read_all()
        updates: dict[str, str] = {}
        secrets_to_migrate = [
            (key, env_data.get(key, ""))
            for key in SECRET_ENV_KEYS
            if env_data.get(key, "") and not is_kv_ref(env_data.get(key, ""))
        ]

        if secrets_to_migrate:
            wait = initial_wait
            for attempt in range(max_retries + 1):
                try:
                    for key, value in secrets_to_migrate:
                        if key in updates:
                            continue
                        ref = _kv.store(env_key_to_secret_name(key), value)
                        updates[key] = ref
                        migrated += 1
                    break
                except Exception:
                    if attempt == max_retries:
                        raise
                    logger.warning(
                        "Migration attempt %d/%d failed, retrying in %.0fs...",
                        attempt + 1, max_retries + 1, wait,
                    )
                    time.sleep(wait)
                    wait *= 2

        if updates:
            cfg.env.write(**updates)

        self._store._save()
        return migrated


def _fail(steps: list[dict]) -> web.Response:
    failed = [s for s in steps if s.get("status") == "failed"]
    msg = failed[0].get("detail", "Unknown") if failed else "Unknown"
    return web.json_response(
        {"status": "error", "steps": steps, "message": f"Prerequisites failed: {msg}"},
        status=500,
    )
