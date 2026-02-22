"""Network info API routes -- /api/network/*."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from ...config.settings import cfg
from ...services.azure import AzureCLI
from ...state.foundry_iq_config import FoundryIQConfigStore
from ...state.sandbox_config import SandboxConfigStore

logger = logging.getLogger(__name__)

# Prefixes that the tunnel restriction middleware allows through
_TUNNEL_ALLOWED_PREFIXES = (
    "/health",
    "/api/messages",
    "/acs",
    "/realtime-acs",
    "/api/voice/acs-callback",
    "/api/voice/media-streaming",
)


def _detect_deploy_mode() -> str:
    """Return 'docker', 'aca', or 'local' based on runtime environment."""
    if os.getenv("POLYCLAW_USE_MI"):
        return "aca"
    if os.getenv("POLYCLAW_CONTAINER") == "1":
        return "docker"
    return "local"


def _classify_endpoint(method: str, path: str) -> str:
    """Classify an endpoint into a category for display grouping."""
    if path.startswith("/api/messages") or path.startswith("/acs") or path.startswith("/realtime-acs"):
        return "bot"
    if path.startswith("/api/voice/") or path.startswith("/api/setup/voice/"):
        return "voice"
    if path.startswith("/api/chat/") or path.startswith("/api/models"):
        return "chat"
    if path.startswith("/api/setup/"):
        return "setup"
    if path.startswith("/api/foundry-iq/"):
        return "foundry-iq"
    if path.startswith("/api/sandbox/"):
        return "sandbox"
    if path.startswith("/api/network/"):
        return "network"
    if path.startswith("/api/"):
        return "admin"
    if path == "/health":
        return "health"
    return "frontend"


def _is_tunnel_exposed(path: str) -> bool:
    """Return True if the path would be allowed through in restricted tunnel mode."""
    return any(path.startswith(pfx) for pfx in _TUNNEL_ALLOWED_PREFIXES)


# Prefixes that live on the runtime (agent) container in split deployments.
_RUNTIME_PREFIXES = (
    "/api/chat/",
    "/api/models",
    "/api/messages",
    "/api/sessions",
    "/api/schedules",
    "/api/proactive",
    "/api/skills",
    "/api/plugins",
    "/api/mcp/",
    "/api/guardrails/",
    "/api/tool-activity",
    "/api/profile",
    "/api/voice/",
    "/api/internal/",
    "/acs",
    "/realtime-acs",
)

# Prefixes that live on the admin (control-plane) container.
_ADMIN_PREFIXES = (
    "/api/setup/",
    "/api/workspace/",
    "/api/environments",
    "/api/sandbox/",
    "/api/foundry-iq/",
    "/api/network/",
    "/api/monitoring/",
    "/api/guardrails/preflight",
    "/api/content-safety/",
)


def _endpoint_container(path: str) -> str:
    """Return which container an endpoint belongs to: 'runtime', 'admin', or 'shared'."""
    # Check admin prefixes first -- more specific paths (e.g.
    # /api/guardrails/preflight) must win over broader runtime prefixes.
    if any(path.startswith(pfx) for pfx in _ADMIN_PREFIXES):
        return "admin"
    if any(path.startswith(pfx) or path == pfx.rstrip("/") for pfx in _RUNTIME_PREFIXES):
        return "runtime"
    if path == "/health" or path == "/api/auth/check" or path.startswith("/api/media/"):
        return "shared"
    return "shared"


class NetworkRoutes:
    """Provides runtime network info: endpoints, components, tunnel mode."""

    def __init__(
        self,
        tunnel: object | None,
        az: AzureCLI | None = None,
        sandbox_store: SandboxConfigStore | None = None,
        foundry_iq_store: FoundryIQConfigStore | None = None,
    ) -> None:
        self._tunnel = tunnel
        self._az = az
        self._sandbox_store = sandbox_store
        self._foundry_iq_store = foundry_iq_store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/network/info", self._info)
        router.add_get("/api/network/endpoints", self._endpoints)
        router.add_get("/api/network/probe", self._probe)
        router.add_get("/api/network/resource-audit", self._resource_audit)

    async def _info(self, req: web.Request) -> web.Response:
        """Return full network topology info."""
        from ..tunnel_status import resolve_tunnel_info

        cfg.reload()
        deploy_mode = _detect_deploy_mode()
        admin_port = cfg.admin_port
        server_mode = cfg.server_mode.value

        # Collect registered endpoints from the live app router
        endpoints = self._collect_endpoints(req.app)

        # Tag local endpoints with source
        for ep in endpoints:
            ep["source"] = "admin"

        # In admin-only mode, also fetch runtime endpoints via RUNTIME_URL
        runtime_endpoints: list[dict[str, Any]] = []
        runtime_url = os.getenv("RUNTIME_URL", "")
        if server_mode == "admin" and runtime_url:
            runtime_endpoints = await self._fetch_runtime_endpoints(runtime_url)
            # Merge: add runtime endpoints that don't already exist locally
            local_keys = {(e["method"], e["path"]) for e in endpoints}
            for rep in runtime_endpoints:
                if (rep["method"], rep["path"]) not in local_keys:
                    rep["source"] = "runtime"
                    endpoints.append(rep)
            endpoints.sort(key=lambda e: (e["category"], e["path"], e["method"]))

        tunnel_info = await resolve_tunnel_info(self._tunnel, self._az)

        # Build component info (what network connections are configured)
        components = self._build_components(deploy_mode, tunnel_info)

        # Build container topology for dual-container deployments
        containers = self._build_containers(deploy_mode, server_mode, admin_port)

        return web.json_response({
            "deploy_mode": deploy_mode,
            "admin_port": admin_port,
            "server_mode": server_mode,
            "tunnel": tunnel_info,
            "lockdown_mode": cfg.lockdown_mode,
            "components": components,
            "endpoints": endpoints,
            "containers": containers,
        })

    async def _endpoints(self, req: web.Request) -> web.Response:
        """Return just the list of registered endpoints."""
        endpoints = self._collect_endpoints(req.app)
        return web.json_response(endpoints)

    async def _fetch_runtime_endpoints(self, runtime_url: str) -> list[dict[str, Any]]:
        """Fetch the endpoint list from the runtime container via its API."""
        url = f"{runtime_url.rstrip('/')}/api/network/endpoints"
        timeout = ClientTimeout(total=3)
        try:
            headers: dict[str, str] = {}
            if cfg.admin_secret:
                headers["Authorization"] = f"Bearer {cfg.admin_secret}"
            async with ClientSession() as session:
                async with session.get(url, timeout=timeout, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            return data
                    logger.warning(
                        "[network.runtime_endpoints] status=%s from %s",
                        resp.status, url,
                    )
        except Exception:
            logger.warning(
                "[network.runtime_endpoints] failed to reach %s", url, exc_info=True,
            )
        return []

    # ------------------------------------------------------------------
    # Endpoint probing – actual HTTP calls to verify auth / tunnel
    # ------------------------------------------------------------------

    # Endpoints whose POST handler enforces Bot Framework JWT auth via
    # the BotFrameworkAdapter – they return 401 when the JWT is missing
    # or invalid.
    _BOT_FRAMEWORK_PATHS = ("/api/messages",)

    # Endpoints whose POST handler validates an ACS callback token
    # (query-param ``?token=``) and optionally an ACS-signed JWT.
    # These return 401 when the token is wrong / missing.
    _ACS_AUTH_PATHS = ("/acs", "/acs/incoming", "/realtime-acs",
                       "/api/voice/acs-callback", "/api/voice/media-streaming")

    async def _probe(self, req: web.Request) -> web.Response:
        """Probe every registered endpoint with real HTTP calls.

        In admin-only mode the probe runs in two phases:

        1. **Local probe** -- tests endpoints registered on this (admin)
           container against ``127.0.0.1:{admin_port}``.
        2. **Cross-container probe** -- fetches the runtime container's
           endpoint list via ``RUNTIME_URL`` and probes those endpoints
           against the runtime's address.

        Each endpoint receives three checks:

        * **Auth probe** -- unauthenticated GET. 401 = auth required.
        * **Tunnel probe** -- GET with Cloudflare headers.  403 = tunnel
          restriction middleware blocked the request.  Only meaningful on
          the runtime container where tunnel middleware runs.
        * **Framework auth probe** -- for bot/ACS endpoints: unauthenticated
          POST to check framework-level auth (Bot JWT / ACS token).
        """
        cfg.reload()
        local_endpoints = self._collect_endpoints(req.app)
        admin_port = cfg.admin_port
        admin_base = f"http://127.0.0.1:{admin_port}"

        runtime_url = os.getenv("RUNTIME_URL", "")
        server_mode = cfg.server_mode.value

        sem = asyncio.Semaphore(20)
        timeout = ClientTimeout(total=3)

        cf_headers = {
            "cf-connecting-ip": "198.51.100.1",
            "cf-ray": "probe",
            "cf-ipcountry": "US",
        }

        _bot_probe_body = {
            "type": "message",
            "text": "",
            "channelId": "probe",
            "from": {"id": "probe"},
            "serviceUrl": "https://probe.invalid",
            "conversation": {"id": "probe"},
        }

        async def _test(
            session: ClientSession,
            ep: dict[str, Any],
            base: str,
            *,
            tunnel_probe_meaningful: bool = False,
        ) -> dict[str, Any]:
            path: str = ep["path"]
            url = f"{base}{path}"
            out: dict[str, Any] = {
                **ep,
                "requires_auth": None,
                "tunnel_blocked": None,
                "auth_type": None,
                "framework_auth_ok": None,
                "probe_error": None,
            }
            async with sem:
                # 1. Auth probe -- unauthenticated GET
                try:
                    async with session.get(
                        url, timeout=timeout, allow_redirects=False,
                    ) as r:
                        out["requires_auth"] = r.status == 401
                except Exception as exc:
                    out["probe_error"] = str(exc)

                # 2. Tunnel probe -- only meaningful on runtime container
                if tunnel_probe_meaningful:
                    try:
                        async with session.get(
                            url, headers=cf_headers,
                            timeout=timeout, allow_redirects=False,
                        ) as r:
                            out["tunnel_blocked"] = r.status == 403
                    except Exception:
                        pass

                # 3. Framework auth probe -- POST for bot/acs endpoints
                is_bot = any(
                    path == p or path.startswith(p + "/")
                    for p in self._BOT_FRAMEWORK_PATHS
                )
                is_acs = any(
                    path == p or path.startswith(p + "/")
                    for p in self._ACS_AUTH_PATHS
                )

                if is_bot:
                    try:
                        async with session.post(
                            url, json=_bot_probe_body,
                            timeout=timeout, allow_redirects=False,
                        ) as r:
                            out["framework_auth_ok"] = r.status == 401
                            out["auth_type"] = "bot_jwt" if r.status == 401 else "open"
                    except Exception:
                        out["auth_type"] = "bot_jwt"
                elif is_acs:
                    # ACS endpoints use event-grid / ACS-token auth, not
                    # standard HTTP 401.  Mark the type but leave
                    # framework_auth_ok as None (not applicable).
                    out["auth_type"] = "acs_token"
                elif out.get("requires_auth"):
                    out["auth_type"] = "admin_key"
                elif path == "/health":
                    out["auth_type"] = "health"
                elif path.startswith("/api/auth/"):
                    out["auth_type"] = "health"
                else:
                    out["auth_type"] = "open"

            return out

        async with ClientSession() as session:
            # Phase 1: probe local (admin) endpoints
            local_tasks = [
                _test(session, ep, admin_base, tunnel_probe_meaningful=False)
                for ep in local_endpoints
            ]
            local_raw = await asyncio.gather(*local_tasks, return_exceptions=True)
            local_probed = [r for r in local_raw if isinstance(r, dict)]

            # Phase 2: cross-probe runtime endpoints (if in admin-only mode)
            runtime_probed: list[dict[str, Any]] = []
            runtime_reachable = False
            if server_mode == "admin" and runtime_url:
                runtime_endpoints = await self._fetch_runtime_endpoints(runtime_url)
                if runtime_endpoints:
                    runtime_reachable = True
                    runtime_base = runtime_url.rstrip("/")
                    # Tunnel probe is meaningful on runtime (that's where
                    # the restriction middleware runs)
                    runtime_tasks = [
                        _test(
                            session, ep, runtime_base,
                            tunnel_probe_meaningful=True,
                        )
                        for ep in runtime_endpoints
                    ]
                    runtime_raw = await asyncio.gather(
                        *runtime_tasks, return_exceptions=True,
                    )
                    runtime_probed = [r for r in runtime_raw if isinstance(r, dict)]
                    for ep in runtime_probed:
                        ep["source"] = "runtime"

        # Mark local endpoints with source
        for ep in local_probed:
            ep.setdefault("source", "admin")

        all_probed = local_probed + runtime_probed

        # --- Compute summary counts per source ---
        def _counts(eps: list[dict[str, Any]]) -> dict[str, Any]:
            total = len(eps)
            auth_required = sum(1 for e in eps if e.get("requires_auth") is True)
            public_no_auth = sum(1 for e in eps if e.get("requires_auth") is False)
            tunnel_blocked = sum(1 for e in eps if e.get("tunnel_blocked") is True)
            tunnel_accessible = sum(1 for e in eps if e.get("tunnel_blocked") is False)
            auth_types: dict[str, int] = {}
            for e in eps:
                at = e.get("auth_type") or "unknown"
                auth_types[at] = auth_types.get(at, 0) + 1
            fw = [e for e in eps if e.get("framework_auth_ok") is not None]
            return {
                "total": total,
                "auth_required": auth_required,
                "public_no_auth": public_no_auth,
                "tunnel_blocked": tunnel_blocked,
                "tunnel_accessible": tunnel_accessible,
                "auth_types": auth_types,
                "framework_auth_ok": sum(1 for e in fw if e["framework_auth_ok"]),
                "framework_auth_fail": sum(1 for e in fw if not e["framework_auth_ok"]),
            }

        return web.json_response({
            "endpoints": all_probed,
            "admin": _counts(local_probed),
            "runtime": _counts(runtime_probed),
            "counts": _counts(all_probed),
            "runtime_reachable": runtime_reachable,
            "tunnel_restricted_during_probe": cfg.tunnel_restricted,
        })

    def _collect_endpoints(self, app: web.Application) -> list[dict[str, Any]]:
        """Walk the live aiohttp router to gather all registered routes."""
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for resource in app.router.resources():
            info = resource.get_info()
            # aiohttp resources can be plain, dynamic, or static
            path = info.get("path") or info.get("formatter") or str(resource)
            if not path or path.startswith("/{tail"):
                continue
            # Skip static asset routes
            if "/assets/" in path and info.get("directory"):
                continue

            for route in resource:
                method = route.method.upper()
                if method == "*":
                    continue
                key = (method, path)
                if key in seen:
                    continue
                seen.add(key)

                category = _classify_endpoint(method, path)
                tunnel_exposed = _is_tunnel_exposed(path)
                container = _endpoint_container(path)

                results.append({
                    "method": method,
                    "path": path,
                    "category": category,
                    "tunnel_exposed": tunnel_exposed,
                    "container": container,
                })

        # Sort by category then path
        results.sort(key=lambda e: (e["category"], e["path"], e["method"]))
        return results

    def _build_containers(
        self,
        deploy_mode: str,
        server_mode: str,
        admin_port: int,
    ) -> list[dict[str, Any]]:
        """Build container topology for the network diagram.

        Only states facts that can be read from the current environment
        or configuration.  Identity and volume claims are intentionally
        omitted -- those are verified by the probe endpoint.
        """
        if deploy_mode == "docker":
            runtime_port = int(os.getenv("RUNTIME_PORT", "8080"))
            runtime_url = os.getenv("RUNTIME_URL", "http://runtime:8080")
            # Parse actual port from RUNTIME_URL if set
            if ":" in runtime_url.rsplit("/", 1)[-1]:
                try:
                    runtime_port = int(runtime_url.rsplit(":", 1)[-1].rstrip("/"))
                except ValueError:
                    pass
            return [
                {
                    "role": "admin",
                    "label": "Admin Container",
                    "port": admin_port,
                    "host": "127.0.0.1",
                    "exposure": "localhost-only",
                },
                {
                    "role": "runtime",
                    "label": "Agent Container",
                    "port": runtime_port,
                    "host": "runtime",
                    "exposure": "tunnel (Cloudflare)",
                },
            ]
        if deploy_mode == "aca":
            aca_name = os.getenv("ACA_ENV_NAME", "polyclaw")
            runtime_port = int(os.getenv("RUNTIME_PORT", "8080"))
            return [
                {
                    "role": "admin",
                    "label": "Admin Container",
                    "port": admin_port,
                    "host": "internal",
                    "exposure": "internal-only",
                },
                {
                    "role": "runtime",
                    "label": "Agent Container",
                    "port": runtime_port,
                    "host": aca_name,
                    "exposure": "ACA ingress",
                },
            ]
        # local / combined -- single process
        return [
            {
                "role": "combined",
                "label": "Polyclaw Server",
                "port": admin_port,
                "host": "localhost",
                "exposure": "localhost",
            },
        ]

    def _build_components(
        self, deploy_mode: str, tunnel_info: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the list of network-connected components."""
        components: list[dict[str, Any]] = []

        # Azure OpenAI / Foundry
        aoai_endpoint = cfg.azure_openai_endpoint
        if aoai_endpoint:
            components.append({
                "name": "Azure OpenAI",
                "type": "ai",
                "endpoint": aoai_endpoint,
                "deployment": cfg.azure_openai_realtime_deployment,
                "status": "configured",
            })

        # GitHub Copilot (model backend)
        if cfg.github_token:
            components.append({
                "name": "GitHub Copilot",
                "type": "ai",
                "endpoint": "https://api.githubcopilot.com",
                "model": cfg.copilot_model,
                "status": "configured",
            })

        # ACS (Communication Services)
        if cfg.acs_connection_string:
            components.append({
                "name": "Azure Communication Services",
                "type": "communication",
                "status": "configured",
                "source_number": cfg.acs_source_number or None,
            })

        # Cloudflare Tunnel -- use pre-resolved tunnel_info when available
        if tunnel_info is not None:
            components.append({
                "name": "Cloudflare Tunnel",
                "type": "tunnel",
                "status": "active" if tunnel_info["active"] else "inactive",
                "url": tunnel_info["url"],
                "restricted": tunnel_info["restricted"],
            })
        else:
            components.append({
                "name": "Cloudflare Tunnel",
                "type": "tunnel",
                "status": "active" if getattr(self._tunnel, "is_active", False) else "inactive",
                "url": getattr(self._tunnel, "url", None),
                "restricted": cfg.tunnel_restricted,
            })

        # Azure Bot Service
        if cfg.bot_app_id:
            components.append({
                "name": "Azure Bot Service",
                "type": "bot",
                "status": "configured",
                "app_id": cfg.bot_app_id[:12] + "..." if cfg.bot_app_id else None,
            })

        # Foundry IQ / AI Search (check env for search endpoint)
        search_endpoint = cfg.env.read("SEARCH_ENDPOINT") or ""
        if search_endpoint:
            components.append({
                "name": "Azure AI Search",
                "type": "search",
                "endpoint": search_endpoint,
                "status": "configured",
            })

        # Storage / Data directory
        components.append({
            "name": "Local Data Store",
            "type": "storage",
            "path": str(cfg.data_dir),
            "status": "active",
            "deploy_mode": deploy_mode,
        })

        return components

    # ------------------------------------------------------------------
    # Resource network audit
    # ------------------------------------------------------------------

    async def _resource_audit(self, req: web.Request) -> web.Response:
        """Audit network configuration of Azure resources.

        Returns per-resource info: public access, firewall rules, allowed IPs,
        private endpoints, TLS settings, etc.
        """
        if not self._az:
            return web.json_response({"resources": [], "error": "Azure CLI not available"})

        resource_groups = self._collect_resource_groups()
        if not resource_groups:
            return web.json_response({"resources": []})

        resources: list[dict[str, Any]] = []
        for rg in resource_groups:
            raw = self._az.json("resource", "list", "--resource-group", rg)
            if not isinstance(raw, list):
                continue
            for r in raw:
                rtype = (r.get("type") or "").lower()
                rname = r.get("name", "")
                audit = self._audit_resource(rg, rname, rtype)
                if audit:
                    resources.append(audit)

        return web.json_response({"resources": resources})

    def _collect_resource_groups(self) -> list[str]:
        """Gather all known resource groups from config stores."""
        rgs: set[str] = set()

        # Main bot / infra resource group
        bot_rg = cfg.env.read("RESOURCE_GROUP") or ""
        if bot_rg:
            rgs.add(bot_rg)

        # Sandbox
        if self._sandbox_store:
            sb = self._sandbox_store.config
            if sb.resource_group:
                rgs.add(sb.resource_group)

        # Foundry IQ
        if self._foundry_iq_store:
            fiq = self._foundry_iq_store.config
            if fiq.resource_group:
                rgs.add(fiq.resource_group)

        # Deploy state resource group
        deploy_rg = cfg.env.read("DEPLOY_RESOURCE_GROUP") or ""
        if deploy_rg:
            rgs.add(deploy_rg)

        # Voice resource group
        voice_rg = cfg.env.read("VOICE_RESOURCE_GROUP") or ""
        if voice_rg:
            rgs.add(voice_rg)

        return list(rgs)

    def _audit_resource(self, rg: str, name: str, rtype: str) -> dict[str, Any] | None:
        """Return a network audit dict for a single Azure resource."""
        if "microsoft.storage/storageaccounts" in rtype:
            return self._audit_storage(rg, name)
        if "microsoft.keyvault/vaults" in rtype:
            return self._audit_keyvault(rg, name)
        if "microsoft.cognitiveservices/accounts" in rtype:
            return self._audit_cognitive(rg, name)
        if "microsoft.search/searchservices" in rtype:
            return self._audit_search(rg, name)
        if "microsoft.containerregistry/registries" in rtype:
            return self._audit_acr(rg, name)
        if "microsoft.app/sessionpools" in rtype:
            return self._audit_session_pool(rg, name)
        if "microsoft.communication/communicationservices" in rtype:
            return self._audit_acs(rg, name)
        return None

    def _audit_storage(self, rg: str, name: str) -> dict[str, Any] | None:
        info = self._az.json("storage", "account", "show", "--name", name, "--resource-group", rg)
        if not isinstance(info, dict):
            return None
        props = info.get("properties") or info
        net_rules = props.get("networkRuleSet") or props.get("networkAcls") or {}
        default_action = (net_rules.get("defaultAction") or "Allow")
        ip_rules = net_rules.get("ipRules") or []
        vnet_rules = net_rules.get("virtualNetworkRules") or []
        allowed_ips = [r.get("value", r.get("ipAddressOrRange", "")) for r in ip_rules]
        allowed_vnets = [r.get("id", "") for r in vnet_rules]
        public_blob = props.get("allowBlobPublicAccess", True)
        https_only = info.get("enableHttpsTrafficOnly", props.get("supportsHttpsTrafficOnly", True))
        min_tls = props.get("minimumTlsVersion", "TLS1_0")
        private_eps = self._get_private_endpoints(props)

        return {
            "name": name,
            "resource_group": rg,
            "type": "Storage Account",
            "icon": "storage",
            "public_access": default_action == "Allow",
            "default_action": default_action,
            "allowed_ips": allowed_ips,
            "allowed_vnets": allowed_vnets,
            "private_endpoints": private_eps,
            "https_only": https_only,
            "min_tls_version": min_tls,
            "extra": {
                "public_blob_access": public_blob,
            },
        }

    def _audit_keyvault(self, rg: str, name: str) -> dict[str, Any] | None:
        info = self._az.json("keyvault", "show", "--name", name, "--resource-group", rg)
        if not isinstance(info, dict):
            return None
        props = info.get("properties") or info
        net_acls = props.get("networkAcls") or {}
        default_action = (net_acls.get("defaultAction") or "Allow")
        ip_rules = net_acls.get("ipRules") or []
        vnet_rules = net_acls.get("virtualNetworkRules") or []
        allowed_ips = [r.get("value", "") for r in ip_rules]
        allowed_vnets = [r.get("id", "") for r in vnet_rules]
        public_access = props.get("publicNetworkAccess", "Enabled")
        private_eps = self._get_private_endpoints(props)
        rbac = props.get("enableRbacAuthorization", False)
        soft_delete = props.get("enableSoftDelete", False)
        purge_protect = props.get("enablePurgeProtection", False)

        return {
            "name": name,
            "resource_group": rg,
            "type": "Key Vault",
            "icon": "keyvault",
            "public_access": public_access != "Disabled" and default_action == "Allow",
            "default_action": default_action,
            "allowed_ips": allowed_ips,
            "allowed_vnets": allowed_vnets,
            "private_endpoints": private_eps,
            "extra": {
                "public_network_access": public_access,
                "rbac_authorization": rbac,
                "soft_delete": soft_delete,
                "purge_protection": purge_protect,
            },
        }

    def _audit_cognitive(self, rg: str, name: str) -> dict[str, Any] | None:
        """Audit Azure OpenAI / Cognitive Services accounts."""
        info = self._az.json(
            "cognitiveservices", "account", "show",
            "--name", name, "--resource-group", rg,
        )
        if not isinstance(info, dict):
            return None
        props = info.get("properties") or info
        net_acls = props.get("networkAcls") or {}
        default_action = (net_acls.get("defaultAction") or "Allow")
        ip_rules = net_acls.get("ipRules") or []
        vnet_rules = net_acls.get("virtualNetworkRules") or []
        allowed_ips = [r.get("value", "") for r in ip_rules]
        allowed_vnets = [r.get("id", "") for r in vnet_rules]
        public_access = props.get("publicNetworkAccess", "Enabled")
        private_eps = self._get_private_endpoints(props)
        kind = info.get("kind", "CognitiveServices")
        endpoint = props.get("endpoint") or (props.get("endpoints") or {}).get("OpenAI Language Model Instance API", "")

        label = "Azure OpenAI" if kind.lower() == "openai" else f"Cognitive Services ({kind})"

        return {
            "name": name,
            "resource_group": rg,
            "type": label,
            "icon": "ai",
            "public_access": public_access != "Disabled" and default_action == "Allow",
            "default_action": default_action,
            "allowed_ips": allowed_ips,
            "allowed_vnets": allowed_vnets,
            "private_endpoints": private_eps,
            "extra": {
                "public_network_access": public_access,
                "kind": kind,
                "endpoint": endpoint,
            },
        }

    def _audit_search(self, rg: str, name: str) -> dict[str, Any] | None:
        """Audit Azure AI Search service."""
        info = self._az.json(
            "search", "service", "show",
            "--name", name, "--resource-group", rg,
        )
        if not isinstance(info, dict):
            return None
        props = info.get("properties") or info
        public_access = props.get("publicNetworkAccess", "enabled")
        ip_rules = (props.get("networkRuleSet") or {}).get("ipRules") or []
        allowed_ips = [r.get("value", "") for r in ip_rules]
        private_eps = self._get_private_endpoints(props)

        return {
            "name": name,
            "resource_group": rg,
            "type": "Azure AI Search",
            "icon": "search",
            "public_access": public_access.lower() != "disabled",
            "default_action": "Allow" if public_access.lower() != "disabled" else "Deny",
            "allowed_ips": allowed_ips,
            "allowed_vnets": [],
            "private_endpoints": private_eps,
            "extra": {
                "public_network_access": public_access,
                "sku": info.get("sku", {}).get("name", ""),
            },
        }

    def _audit_acr(self, rg: str, name: str) -> dict[str, Any] | None:
        info = self._az.json("acr", "show", "--name", name, "--resource-group", rg)
        if not isinstance(info, dict):
            return None
        public_access = info.get("publicNetworkAccess", "Enabled")
        net_rules = info.get("networkRuleSet") or {}
        default_action = (net_rules.get("defaultAction") or "Allow")
        ip_rules = net_rules.get("ipRules") or []
        allowed_ips = [r.get("value", "") for r in ip_rules]
        admin_enabled = info.get("adminUserEnabled", False)

        return {
            "name": name,
            "resource_group": rg,
            "type": "Container Registry",
            "icon": "acr",
            "public_access": public_access == "Enabled",
            "default_action": default_action,
            "allowed_ips": allowed_ips,
            "allowed_vnets": [],
            "private_endpoints": [],
            "extra": {
                "admin_user_enabled": admin_enabled,
                "sku": info.get("sku", {}).get("name", ""),
            },
        }

    def _audit_session_pool(self, rg: str, name: str) -> dict[str, Any] | None:
        """Audit Azure Container Apps session pool."""
        return {
            "name": name,
            "resource_group": rg,
            "type": "Session Pool",
            "icon": "sandbox",
            "public_access": True,
            "default_action": "Allow",
            "allowed_ips": [],
            "allowed_vnets": [],
            "private_endpoints": [],
            "extra": {},
        }

    def _audit_acs(self, rg: str, name: str) -> dict[str, Any] | None:
        """Audit Azure Communication Services."""
        return {
            "name": name,
            "resource_group": rg,
            "type": "Communication Services",
            "icon": "communication",
            "public_access": True,
            "default_action": "Allow",
            "allowed_ips": [],
            "allowed_vnets": [],
            "private_endpoints": [],
            "extra": {},
        }

    @staticmethod
    def _get_private_endpoints(props: dict[str, Any]) -> list[str]:
        """Extract private endpoint names from a resource's properties."""
        pe_conns = props.get("privateEndpointConnections", [])
        results: list[str] = []
        for pec in pe_conns:
            pe = pec.get("privateEndpoint", {})
            pe_id = pe.get("id", "")
            if pe_id:
                # Extract just the endpoint name from the full resource ID
                results.append(pe_id.rsplit("/", 1)[-1])
        return results
