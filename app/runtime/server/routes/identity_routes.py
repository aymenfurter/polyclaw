"""Admin routes for Agent Identity inspection -- /api/identity/*."""

from __future__ import annotations

import functools
import logging
from typing import Any

from aiohttp import web

from ...config.settings import cfg
from ...services.azure import AzureCLI
from ...state.guardrails_config import GuardrailsConfigStore
from ...state.sandbox_config import SandboxConfigStore
from ...util.async_helpers import run_sync

logger = logging.getLogger(__name__)

_DEFAULT_RG = "polyclaw-rg"

# Roles that the runtime *should* have, keyed by a human-readable
# feature label.  The ``data_action`` is what Azure actually checks
# when the API call is made.
_REQUIRED_ROLES: list[dict[str, str]] = [
    {
        "feature": "Prompt Shields (Content Safety)",
        "role": "Cognitive Services User",
        "role_id": "a97b65f3-24c7-4388-baec-2e87135dc908",
        "data_action": (
            "Microsoft.CognitiveServices/accounts/"
            "ContentSafety/text:shieldprompt/action"
        ),
    },
    {
        "feature": "Bot Service Management",
        "role": "Azure Bot Service Contributor Role",
        "data_action": "",
    },
    {
        "feature": "Resource Group Visibility",
        "role": "Reader",
        "data_action": "",
    },
    {
        "feature": "Key Vault Secrets",
        "role": "Key Vault Secrets Officer",
        "data_action": "",
    },
    {
        "feature": "Sandbox / Code Interpreter",
        "role": "Azure ContainerApps Session Executor",
        "role_id": "0fb8eba5-a2bb-4abe-b1c1-49dfad359bb0",
        "data_action": "",
    },
]


class IdentityRoutes:
    """Inspect and audit the runtime agent identity and its RBAC roles."""

    def __init__(
        self,
        az: AzureCLI | None = None,
        guardrails_store: GuardrailsConfigStore | None = None,
        sandbox_store: SandboxConfigStore | None = None,
    ) -> None:
        self._az = az
        self._guardrails_store = guardrails_store
        self._sandbox_store = sandbox_store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/identity/info", self._info)
        router.add_get("/api/identity/roles", self._roles)
        router.add_post("/api/identity/fix-roles", self._fix_roles)

    # ------------------------------------------------------------------
    # GET /api/identity/info
    # ------------------------------------------------------------------

    async def _info(self, _req: web.Request) -> web.Response:
        """Return the resolved runtime identity and tenant."""
        identity = self._static_identity()
        lookup_id = identity.get("app_id") or identity.get("mi_client_id")

        if self._az and lookup_id:
            sp_info = await self._sp_show(lookup_id)
            if isinstance(sp_info, dict):
                identity["display_name"] = (
                    sp_info.get("displayName", "")
                    or sp_info.get("appDisplayName", "")
                )
                identity["principal_id"] = (
                    sp_info.get("id", "")
                    or sp_info.get("objectId", "")
                    or identity.get("principal_id", "")
                )
            else:
                logger.warning(
                    "[identity.info] az ad sp show failed, returning partial info",
                )
        return web.json_response({"status": "ok", **identity})

    # ------------------------------------------------------------------
    # GET /api/identity/roles
    # ------------------------------------------------------------------

    async def _roles(self, _req: web.Request) -> web.Response:
        """List all RBAC assignments and check required roles."""
        identity = self._static_identity()
        lookup_id = identity.get("app_id") or identity.get("mi_client_id") or ""
        if not lookup_id or not self._az:
            return web.json_response({
                "status": "ok",
                "assignments": [],
                "required": _REQUIRED_ROLES,
                "checks": [],
                "message": "No identity or Azure CLI available",
            })

        # Resolve principal object-id for accurate role listing; fall back
        # to the app / client id when Entra resolution fails.
        assignee = lookup_id
        sp_info = await self._sp_show(lookup_id)
        if isinstance(sp_info, dict):
            assignee = (
                sp_info.get("id", "")
                or sp_info.get("objectId", "")
                or lookup_id
            )

        assignments = await run_sync(
            self._az.json,
            "role", "assignment", "list",
            "--assignee", assignee, "--all",
        )
        if not isinstance(assignments, list):
            return web.json_response({
                "status": "error",
                "message": "Failed to list role assignments",
                "detail": self._az.last_stderr or "",
            }, status=500)

        # Normalise for the frontend
        clean: list[dict[str, str]] = []
        for a in assignments:
            if not isinstance(a, dict):
                continue
            clean.append({
                "role": a.get("roleDefinitionName", ""),
                "scope": a.get("scope", ""),
                "condition": a.get("condition", ""),
            })

        # Check which required roles are present
        assigned_names = {a.get("roleDefinitionName", "") for a in assignments}
        checks: list[dict[str, Any]] = []
        for req in _REQUIRED_ROLES:
            present = req["role"] in assigned_names
            checks.append({
                "feature": req["feature"],
                "role": req["role"],
                "present": present,
                "data_action": req.get("data_action", ""),
            })

        return web.json_response({
            "status": "ok",
            "assignments": clean,
            "required": _REQUIRED_ROLES,
            "checks": checks,
        })

    # ------------------------------------------------------------------
    # POST /api/identity/fix-roles
    # ------------------------------------------------------------------

    async def _fix_roles(self, req: web.Request) -> web.Response:
        """Assign any missing required roles to the runtime identity.

        Currently supports fixing the Content Safety role by resolving the
        resource from the configured endpoint and assigning Cognitive
        Services User.
        """
        if not self._az:
            return web.json_response(
                {"status": "error", "message": "Azure CLI not available"},
                status=400,
            )

        identity = self._static_identity()
        principal_id = identity.get("principal_id", "")
        principal_type = identity.get("principal_type", "ServicePrincipal")

        # If static identity has no principal_id, resolve from Entra.
        # If resolution fails, fall back to app_id with ``--assignee``
        # instead of ``--assignee-object-id``.
        use_object_id = True
        if not principal_id:
            principal_id, principal_type = await self._resolve_principal(identity)
        if not principal_id:
            # Fallback: use app_id directly
            principal_id = (
                identity.get("app_id") or identity.get("mi_client_id") or ""
            )
            use_object_id = False
        if not principal_id:
            return web.json_response({
                "status": "error",
                "message": "Cannot determine runtime principal",
            }, status=400)

        steps: list[dict[str, Any]] = []

        # Fix Content Safety role
        cs_endpoint = ""
        if self._guardrails_store:
            cs_endpoint = self._guardrails_store.config.content_safety_endpoint

        if cs_endpoint:
            resource_id = await self._resolve_cs_resource(cs_endpoint)
            if resource_id:
                await self._assign_role(
                    principal_id, principal_type,
                    "a97b65f3-24c7-4388-baec-2e87135dc908",
                    resource_id, "Cognitive Services User", steps,
                    use_object_id=use_object_id,
                )
            else:
                steps.append({
                    "step": "content_safety_rbac",
                    "status": "warning",
                    "detail": f"Cannot resolve resource for endpoint {cs_endpoint}",
                })
        else:
            steps.append({
                "step": "content_safety_rbac",
                "status": "skipped",
                "detail": "No Content Safety endpoint configured",
            })

        # Fix Session Pool Executor role
        await self._fix_session_pool_role(
            principal_id, principal_type, steps,
            use_object_id=use_object_id,
        )

        return web.json_response({"status": "ok", "steps": steps})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fix_session_pool_role(
        self,
        principal_id: str,
        principal_type: str,
        steps: list[dict[str, Any]],
        *,
        use_object_id: bool = True,
    ) -> None:
        """Assign Azure ContainerApps Session Executor on the session pool."""
        assert self._az is not None
        store = self._sandbox_store or SandboxConfigStore()
        pool_id = store.pool_id
        if not pool_id:
            # Try to construct from rg + pool_name
            rg = store.resource_group
            name = store.pool_name
            if rg and name:
                account = await run_sync(self._az.json, "account", "show", quiet=True)
                sub_id = account.get("id", "") if isinstance(account, dict) else ""
                if sub_id:
                    pool_id = (
                        f"/subscriptions/{sub_id}/resourceGroups/{rg}"
                        f"/providers/Microsoft.App/sessionPools/{name}"
                    )
        if not pool_id:
            steps.append({
                "step": "session_pool_rbac",
                "status": "skipped",
                "detail": "No session pool configured",
            })
            return

        await self._assign_role(
            principal_id, principal_type,
            "0fb8eba5-a2bb-4abe-b1c1-49dfad359bb0",
            pool_id,
            "Azure ContainerApps Session Executor",
            steps,
            use_object_id=use_object_id,
        )

    def _static_identity(self) -> dict[str, Any]:
        """Build identity dict from env config (no az calls)."""
        app_id = cfg.runtime_sp_app_id
        mi_client_id = cfg.aca_mi_client_id
        tenant = cfg.runtime_sp_tenant

        strategy: str | None = None
        if mi_client_id:
            strategy = "managed_identity"
        elif app_id:
            strategy = "service_principal"

        return {
            "configured": bool(app_id or mi_client_id),
            "strategy": strategy,
            "app_id": app_id or "",
            "mi_client_id": mi_client_id or "",
            "tenant": tenant or "",
            "display_name": "",
            "principal_id": "",
            "principal_type": "ServicePrincipal" if strategy else "",
        }

    async def _sp_show(self, lookup_id: str) -> dict[str, Any] | None:
        """Call ``az ad sp show`` and return the parsed dict, or *None*."""
        assert self._az is not None
        result = await run_sync(
            functools.partial(
                self._az.json, "ad", "sp", "show", "--id", lookup_id, quiet=True,
            ),
        )
        return result if isinstance(result, dict) else None

    async def _resolve_principal(
        self, identity: dict[str, Any],
    ) -> tuple[str, str]:
        """Resolve principal object ID from app_id or mi_client_id."""
        assert self._az is not None
        lookup_id = identity.get("app_id") or identity.get("mi_client_id") or ""
        if not lookup_id:
            return "", ""
        sp_info = await self._sp_show(lookup_id)
        if sp_info:
            pid = sp_info.get("id", "") or sp_info.get("objectId", "")
            if pid:
                return pid, "ServicePrincipal"
        return "", ""

    async def _resolve_cs_resource(self, endpoint: str) -> str:
        """Find the ARM resource ID for a Content Safety endpoint.

        First tries scoping to the configured resource group (fast).  If
        that yields nothing, falls back to a subscription-wide listing.
        """
        assert self._az is not None
        normalised = endpoint.rstrip("/").lower()

        rg = _DEFAULT_RG
        if rg:
            accounts = await run_sync(
                self._az.json,
                "cognitiveservices", "account", "list",
                "--resource-group", rg,
            )
            rid = self._match_cs_endpoint(accounts, normalised)
            if rid:
                return rid

        # Fallback: subscription-wide (slower)
        accounts = await run_sync(
            self._az.json, "cognitiveservices", "account", "list",
        )
        return self._match_cs_endpoint(accounts, normalised)

    @staticmethod
    def _match_cs_endpoint(
        accounts: list[Any] | dict[str, Any] | None,
        normalised: str,
    ) -> str:
        """Return the ARM resource ID whose endpoint matches *normalised*."""
        if not isinstance(accounts, list):
            return ""
        for acct in accounts:
            if not isinstance(acct, dict):
                continue
            acct_ep = (
                acct.get("properties", {}).get("endpoint", "")
            ).rstrip("/").lower()
            if acct_ep == normalised:
                return acct.get("id", "")
        return ""

    async def _assign_role(
        self,
        principal_id: str,
        principal_type: str,
        role_id: str,
        scope: str,
        role_name: str,
        steps: list[dict[str, Any]],
        *,
        use_object_id: bool = True,
    ) -> None:
        """Create a role assignment."""
        assert self._az is not None
        logger.info(
            "[identity.fix] assigning %s to %s on %s (object_id=%s)",
            role_name, principal_id, scope, use_object_id,
        )
        if use_object_id:
            assignee_args = [
                "--assignee-object-id", principal_id,
                "--assignee-principal-type", principal_type,
            ]
        else:
            assignee_args = ["--assignee", principal_id]
        ok, msg = await run_sync(
            self._az.ok,
            "role", "assignment", "create",
            *assignee_args,
            "--role", role_id,
            "--scope", scope,
        )
        if ok:
            steps.append({
                "step": f"assign_{role_name.lower().replace(' ', '_')}",
                "status": "ok",
                "detail": f"{role_name} assigned to {principal_id}",
            })
        elif "already exists" in (msg or "").lower() or "conflict" in (msg or "").lower():
            steps.append({
                "step": f"assign_{role_name.lower().replace(' ', '_')}",
                "status": "ok",
                "detail": "Already assigned",
            })
        else:
            steps.append({
                "step": f"assign_{role_name.lower().replace(' ', '_')}",
                "status": "failed",
                "detail": f"Assignment failed: {msg}",
            })
            logger.warning("[identity.fix] role assignment failed: %s", msg, exc_info=True)
