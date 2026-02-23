"""Runtime identity provisioner -- creates a scoped service principal for the agent runtime.

The admin container calls this to provision a service principal that the
runtime container uses for ``az login``.  The SP receives the following RBAC
roles scoped to the single resource group:

* ``Bot Service Contributor`` -- create, update, and delete the Bot Service
  registration and update its messaging endpoint on every restart.
* ``Reader`` -- enumerate resources in the resource group.
* ``Key Vault Secrets Officer`` -- read and write Key Vault secrets that
  store bot credentials (MicrosoftAppId / MicrosoftAppPassword).  Scoped
  to the vault resource itself (which may live in a separate prerequisites
  resource group).

The SP credentials are written to the shared ``.env`` (``RUNTIME_SP_*``
keys) and the runtime container picks them up at boot via
``az login --service-principal``.
"""

from __future__ import annotations

import logging
from typing import Any

from ...state.sandbox_config import SandboxConfigStore
from ._azure_rbac import (
    BOT_CONTRIBUTOR_ROLE as _BOT_CONTRIBUTOR_ROLE,
    KV_SECRETS_ROLE as _KV_SECRETS_ROLE,
    MI_NAME as _MI_NAME,
    RG_READER_ROLE as _RG_READER_ROLE,
    SESSION_EXECUTOR_ROLE as _SESSION_EXECUTOR_ROLE,
    session_pool_scope as _session_pool_scope_fn,
)
from .azure import AzureCLI

logger = logging.getLogger(__name__)

_SP_DISPLAY_NAME = "polyclaw-runtime"


class RuntimeIdentityProvisioner:
    """Provision and revoke the scoped identity for the agent runtime.

    Two strategies are supported:

    * **Service principal** -- used in Docker Compose deployments.  The SP
      credentials are written to ``/data/.env`` and the runtime container
      picks them up via ``az login --service-principal``.
    * **User-assigned managed identity** -- used in ACA deployments.  The
      MI is attached to the runtime container app and
      ``DefaultAzureCredential`` discovers it automatically.  No secrets
      to rotate.
    """

    def __init__(self, az: AzureCLI) -> None:
        self._az = az

    def provision(self, resource_group: str) -> dict[str, Any]:
        """Create (or rotate) the runtime SP and assign RBAC.

        Returns a dict with ``ok``, ``app_id``, ``password``, ``tenant``,
        and a human-readable ``steps`` list.
        """
        steps: list[dict[str, str]] = []

        # 1. Get the subscription id for RBAC scope
        account = self._az.account_info()
        if not account:
            return {"ok": False, "error": "Not logged in to Azure", "steps": steps}
        sub_id = account.get("id", "")
        tenant = account.get("tenantId", "")

        # 2. Ensure the resource group exists (admin should have created it)
        rg_info = self._az.json("group", "show", "--name", resource_group)
        if not rg_info:
            steps.append({"step": "check_rg", "status": "failed",
                          "detail": f"Resource group '{resource_group}' not found"})
            return {"ok": False, "error": f"Resource group '{resource_group}' does not exist",
                    "steps": steps}
        steps.append({"step": "check_rg", "status": "ok", "detail": resource_group})

        # 3. Check for existing SP
        existing = self._az.json(
            "ad", "sp", "list",
            "--display-name", _SP_DISPLAY_NAME,
            "--query", "[0]",
        )
        app_id = ""
        if isinstance(existing, dict) and existing.get("appId"):
            app_id = existing["appId"]
            logger.info("Found existing runtime SP: %s", app_id)
            steps.append({"step": "find_sp", "status": "ok",
                          "detail": f"Reusing {app_id}"})
        else:
            # 4. Create a new app registration + SP
            app = self._az.json(
                "ad", "app", "create",
                "--display-name", _SP_DISPLAY_NAME,
                "--sign-in-audience", "AzureADMyOrg",
            )
            if not isinstance(app, dict):
                steps.append({"step": "create_app", "status": "failed",
                              "detail": self._az.last_stderr})
                return {"ok": False, "error": "App registration failed", "steps": steps}
            app_id = app.get("appId", "")
            sp = self._az.json("ad", "sp", "create", "--id", app_id)
            if not sp and "already in use" not in (self._az.last_stderr or ""):
                steps.append({"step": "create_sp", "status": "failed",
                              "detail": self._az.last_stderr})
                return {"ok": False, "error": "Service principal creation failed",
                        "steps": steps}
            logger.info("Created runtime SP: %s", app_id)
            steps.append({"step": "create_sp", "status": "ok", "detail": app_id})

        # 5. Rotate credentials
        cred = self._az.json("ad", "app", "credential", "reset", "--id", app_id, "--years", "2")
        if not isinstance(cred, dict) or not cred.get("password"):
            steps.append({"step": "rotate_creds", "status": "failed",
                          "detail": self._az.last_stderr})
            return {"ok": False, "error": "Credential rotation failed", "steps": steps}
        password = cred["password"]
        tenant = cred.get("tenant", tenant)
        steps.append({"step": "rotate_creds", "status": "ok"})

        # 6. Assign RBAC roles on the resource group
        rg_scope = f"/subscriptions/{sub_id}/resourceGroups/{resource_group}"
        self._assign_role(app_id, _BOT_CONTRIBUTOR_ROLE, rg_scope, steps)
        self._assign_role(app_id, _RG_READER_ROLE, rg_scope, steps)

        # Key Vault may live in a different RG (e.g. polyclaw-prereq-rg).
        # Scope the secrets role to the vault resource itself so the SP
        # can resolve @kv: references regardless of which RG the vault is in.
        kv_scope = self._keyvault_scope(sub_id)
        self._assign_role(app_id, _KV_SECRETS_ROLE, kv_scope or rg_scope, steps)

        # Session pool executor (needed for sandbox / code interpreter)
        session_scope = self._session_pool_scope(sub_id)
        if session_scope:
            self._assign_role(app_id, _SESSION_EXECUTOR_ROLE, session_scope, steps)

        # 7. Write the SP credentials to the shared .env
        from ...config.settings import cfg

        cfg.write_env(
            RUNTIME_SP_APP_ID=app_id,
            RUNTIME_SP_PASSWORD=password,
            RUNTIME_SP_TENANT=tenant,
        )
        steps.append({"step": "write_env", "status": "ok"})

        logger.info(
            "[runtime_identity] provisioned: app_id=%s, rg=%s, roles=%s",
            app_id, resource_group,
            [_BOT_CONTRIBUTOR_ROLE, _RG_READER_ROLE, _KV_SECRETS_ROLE],
        )
        return {
            "ok": True,
            "app_id": app_id,
            "password": "***",
            "tenant": tenant,
            "resource_group": resource_group,
            "steps": steps,
        }

    def revoke(self) -> dict[str, Any]:
        """Delete the runtime SP and clear env vars."""
        from ...config.settings import cfg

        steps: list[dict[str, str]] = []

        app_id = cfg.env.read("RUNTIME_SP_APP_ID")
        if not app_id:
            return {"ok": True, "steps": [{"step": "revoke", "status": "skip",
                                           "detail": "No runtime SP configured"}]}

        result = self._az.ok("ad", "app", "delete", "--id", app_id)
        steps.append({
            "step": "delete_app",
            "status": "ok" if result else "failed",
            "detail": app_id,
        })

        cfg.write_env(RUNTIME_SP_APP_ID="", RUNTIME_SP_PASSWORD="", RUNTIME_SP_TENANT="")
        steps.append({"step": "clear_env", "status": "ok"})

        return {"ok": bool(result), "steps": steps}

    # ------------------------------------------------------------------
    # Managed Identity (ACA deployments)
    # ------------------------------------------------------------------

    def provision_managed_identity(
        self, resource_group: str, location: str = "eastus",
    ) -> dict[str, Any]:
        """Create a user-assigned managed identity for the ACA runtime container.

        Returns ``{ok, mi_resource_id, client_id, steps}``.
        """
        steps: list[dict[str, str]] = []

        account = self._az.account_info()
        if not account:
            return {"ok": False, "error": "Not logged in to Azure", "steps": steps}
        sub_id = account.get("id", "")

        # Check for existing MI
        existing = self._az.json(
            "identity", "show",
            "--name", _MI_NAME,
            "--resource-group", resource_group,
        )
        if isinstance(existing, dict) and existing.get("id"):
            mi_id = existing["id"]
            client_id = existing.get("clientId", "")
            principal_id = existing.get("principalId", "")
            logger.info("Found existing runtime MI: %s", mi_id)
            steps.append({"step": "find_mi", "status": "ok",
                          "detail": f"Reusing {_MI_NAME}"})
        else:
            result = self._az.json(
                "identity", "create",
                "--name", _MI_NAME,
                "--resource-group", resource_group,
                "--location", location,
            )
            if not isinstance(result, dict):
                steps.append({"step": "create_mi", "status": "failed",
                              "detail": self._az.last_stderr})
                return {"ok": False, "error": "Managed identity creation failed",
                        "steps": steps}
            mi_id = result.get("id", "")
            client_id = result.get("clientId", "")
            principal_id = result.get("principalId", "")
            logger.info("Created runtime MI: %s", mi_id)
            steps.append({"step": "create_mi", "status": "ok", "detail": _MI_NAME})

        # Assign RBAC
        rg_scope = f"/subscriptions/{sub_id}/resourceGroups/{resource_group}"
        self._assign_role(principal_id, _BOT_CONTRIBUTOR_ROLE, rg_scope, steps)
        self._assign_role(principal_id, _RG_READER_ROLE, rg_scope, steps)

        # Key Vault may live in a different RG -- scope to the vault resource.
        kv_scope = self._keyvault_scope(sub_id)
        self._assign_role(principal_id, _KV_SECRETS_ROLE, kv_scope or rg_scope, steps)

        # Session pool executor (needed for sandbox / code interpreter)
        session_scope = self._session_pool_scope(sub_id)
        if session_scope:
            self._assign_role(principal_id, _SESSION_EXECUTOR_ROLE, session_scope, steps)

        # Write MI config to .env so the ACA deployer can reference it
        from ...config.settings import cfg

        cfg.write_env(
            ACA_MI_RESOURCE_ID=mi_id,
            ACA_MI_CLIENT_ID=client_id,
        )
        steps.append({"step": "write_env", "status": "ok"})

        logger.info(
            "[runtime_identity.mi] provisioned: mi=%s, client=%s, rg=%s, roles=%s",
            mi_id, client_id, resource_group,
            [_BOT_CONTRIBUTOR_ROLE, _RG_READER_ROLE, _KV_SECRETS_ROLE],
        )
        return {
            "ok": True,
            "mi_resource_id": mi_id,
            "client_id": client_id,
            "resource_group": resource_group,
            "steps": steps,
        }

    def revoke_managed_identity(self, resource_group: str) -> dict[str, Any]:
        """Delete the managed identity."""
        from ...config.settings import cfg

        steps: list[dict[str, str]] = []
        mi_id = cfg.env.read("ACA_MI_RESOURCE_ID")
        if not mi_id:
            return {"ok": True, "steps": [{"step": "revoke_mi", "status": "skip",
                                           "detail": "No MI configured"}]}
        ok, _msg = self._az.ok("identity", "delete", "--ids", mi_id)
        steps.append({"step": "delete_mi", "status": "ok" if ok else "failed",
                      "detail": mi_id})
        cfg.write_env(ACA_MI_RESOURCE_ID="", ACA_MI_CLIENT_ID="")
        steps.append({"step": "clear_env", "status": "ok"})
        return {"ok": bool(ok), "steps": steps}

    def status(self) -> dict[str, Any]:
        """Return current runtime identity state."""
        from ...config.settings import cfg

        app_id = cfg.env.read("RUNTIME_SP_APP_ID")
        mi_client_id = cfg.env.read("ACA_MI_CLIENT_ID")
        return {
            "configured": bool(app_id or mi_client_id),
            "strategy": "managed_identity" if mi_client_id else ("sp" if app_id else None),
            "app_id": app_id or None,
            "tenant": cfg.env.read("RUNTIME_SP_TENANT") or None,
            "mi_client_id": mi_client_id or None,
            "mi_resource_id": cfg.env.read("ACA_MI_RESOURCE_ID") or None,
        }

    def _session_pool_scope(self, subscription_id: str) -> str | None:
        """Return the ARM resource scope for the ACA session pool, or ``None``."""
        return _session_pool_scope_fn(subscription_id)

    def _keyvault_scope(self, subscription_id: str) -> str | None:
        """Return the ARM resource scope for the Key Vault, or ``None``.

        The vault may live in a different resource group from the main
        deployment (e.g. ``polyclaw-prereq-rg``).  Using the vault-level
        scope ensures the KV Secrets Officer role grants access regardless
        of which RG the vault is in.
        """
        from ...config.settings import cfg

        kv_name = cfg.env.read("KEY_VAULT_NAME") or ""
        kv_rg = cfg.env.read("KEY_VAULT_RG") or ""
        if kv_name and kv_rg:
            return (
                f"/subscriptions/{subscription_id}/resourceGroups/{kv_rg}"
                f"/providers/Microsoft.KeyVault/vaults/{kv_name}"
            )
        return None

    def _assign_role(
        self, app_id: str, role: str, scope: str, steps: list[dict[str, str]],
    ) -> None:
        result = self._az.ok(
            "role", "assignment", "create",
            "--assignee", app_id,
            "--role", role,
            "--scope", scope,
        )
        if result or "already exists" in (self._az.last_stderr or "").lower():
            steps.append({"step": f"rbac_{role.lower().replace(' ', '_')}",
                          "status": "ok", "detail": f"{role} on {scope}"})
        else:
            steps.append({"step": f"rbac_{role.lower().replace(' ', '_')}",
                          "status": "failed", "detail": self._az.last_stderr})
