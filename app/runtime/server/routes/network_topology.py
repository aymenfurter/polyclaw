"""Network topology builders for the network-info API."""

from __future__ import annotations

import os
from typing import Any

from ...config.settings import cfg


def build_containers(
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


def build_components(
    deploy_mode: str,
    tunnel: object | None,
    tunnel_info: dict[str, Any] | None = None,
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
            "status": "active" if getattr(tunnel, "is_active", False) else "inactive",
            "url": getattr(tunnel, "url", None),
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
