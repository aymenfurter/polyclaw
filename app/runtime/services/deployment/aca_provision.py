"""ACA resource provisioning helpers for the deployer."""

from __future__ import annotations

import logging
import secrets
import subprocess
import time
from typing import Any

from ...config.settings import cfg
from ...state.deploy_state import DeploymentRecord
from ..cloud.azure import AzureCLI
from ..cloud._azure_rbac import (
    BOT_CONTRIBUTOR_ROLE as _BOT_CONTRIBUTOR_ROLE,
    IMAGE_NAME as _IMAGE_NAME,
    MI_NAME as _MI_NAME,
    RG_READER_ROLE as _RG_READER_ROLE,
    SESSION_EXECUTOR_ROLE as _SESSION_EXECUTOR_ROLE,
    session_pool_scope as _session_pool_scope,
)

logger = logging.getLogger(__name__)

_ENV_NAME_PREFIX = "polyclaw-env"


def ensure_acr(
    az: AzureCLI,
    resource_group: str,
    location: str,
    steps: list[dict],
    rec: DeploymentRecord,
) -> str:
    """Create a container registry.  Returns the ACR name, or ``""`` on failure."""
    logger.info("[aca] Step 3/10: Creating container registry ...")
    acr_name = "polyclaw" + secrets.token_hex(4)
    acr_name = acr_name[:50].replace("-", "")

    result = az.json(
        "acr", "create",
        "--resource-group", resource_group,
        "--name", acr_name,
        "--sku", "Basic",
        "--admin-enabled", "true",
        "--location", location,
    )
    if not result:
        steps.append({
            "step": "acr_create", "status": "failed",
            "detail": az.last_stderr,
        })
        return ""
    steps.append({"step": "acr_create", "status": "ok", "detail": acr_name})
    rec.add_resource("acr", resource_group, acr_name, "Container registry")
    return acr_name


def get_acr_credentials(az: AzureCLI, acr_name: str) -> tuple[str, str]:
    """Return ``(username, password)`` for the ACR admin account."""
    creds = az.json("acr", "credential", "show", "--name", acr_name)
    if not isinstance(creds, dict):
        return "", ""
    username = creds.get("username", "")
    passwords = creds.get("passwords", [])
    password = passwords[0].get("value", "") if passwords else ""
    return username, password


def push_image(
    az: AzureCLI,
    acr_name: str,
    tag: str,
    steps: list[dict],
) -> bool:
    """Build, tag, and push the local Docker image to ACR."""
    logger.info("[aca] Step 4/10: Pushing pre-built image to ACR ...")
    local_image = f"{_IMAGE_NAME}:{tag}"
    remote_image = f"{acr_name}.azurecr.io/{_IMAGE_NAME}:{tag}"

    check = subprocess.run(
        ["docker", "image", "inspect", local_image],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        detail = (
            f"Local image '{local_image}' not found. "
            "Build it first with: docker build --platform linux/amd64 "
            f"-t {local_image} ."
        )
        logger.error("[aca] %s", detail)
        steps.append({"step": "image_push", "status": "failed", "detail": detail})
        return False

    logger.info("[aca] Logging in to ACR %s ...", acr_name)
    ok, msg = az.ok("acr", "login", "--name", acr_name)
    if not ok:
        detail = f"ACR login failed: {msg or az.last_stderr}"
        logger.error("[aca] %s", detail)
        steps.append({"step": "image_push", "status": "failed", "detail": detail})
        return False

    logger.info("[aca] Tagging %s -> %s", local_image, remote_image)
    tag_result = subprocess.run(
        ["docker", "tag", local_image, remote_image],
        capture_output=True, text=True,
    )
    if tag_result.returncode != 0:
        detail = f"docker tag failed: {tag_result.stderr.strip()}"
        logger.error("[aca] %s", detail)
        steps.append({"step": "image_push", "status": "failed", "detail": detail})
        return False

    logger.info("[aca] Pushing %s (this may take 1-2 minutes) ...", remote_image)
    push_result = subprocess.run(
        ["docker", "push", remote_image],
        capture_output=True, text=True, timeout=600,
    )
    if push_result.returncode != 0:
        detail = f"docker push failed: {push_result.stderr.strip()[:500]}"
        logger.error("[aca] %s", detail)
        steps.append({"step": "image_push", "status": "failed", "detail": detail})
        return False

    logger.info("[aca] Image pushed: %s", remote_image)
    steps.append({"step": "image_push", "status": "ok", "detail": remote_image})
    return True


def ensure_managed_identity(
    az: AzureCLI,
    resource_group: str,
    location: str,
    steps: list[dict],
    rec: DeploymentRecord,
) -> tuple[str, str]:
    """Create a user-assigned managed identity.  Returns ``(id, client_id)``."""
    logger.info("[aca] Step 5/10: Creating managed identity ...")
    result = az.json(
        "identity", "create",
        "--name", _MI_NAME,
        "--resource-group", resource_group,
        "--location", location,
    )
    if not isinstance(result, dict):
        steps.append({"step": "managed_identity", "status": "failed",
                      "detail": az.last_stderr})
        return "", ""

    mi_id = result.get("id", "")
    client_id = result.get("clientId", "")
    steps.append({"step": "managed_identity", "status": "ok", "detail": _MI_NAME})
    rec.add_resource("managed_identity", resource_group, _MI_NAME,
                     "Runtime scoped identity")
    return mi_id, client_id


def assign_rbac(
    az: AzureCLI,
    mi_principal_id: str,
    resource_group: str,
    steps: list[dict],
) -> None:
    """Assign RBAC roles to the managed identity."""
    logger.info("[aca] Step 6/10: Assigning RBAC ...")
    account = az.account_info()
    sub_id = account.get("id", "") if account else ""
    rg_scope = f"/subscriptions/{sub_id}/resourceGroups/{resource_group}"

    for role in (_BOT_CONTRIBUTOR_ROLE, _RG_READER_ROLE):
        label = role.lower().replace(" ", "_")
        assigned = False
        for attempt in range(4):
            if attempt:
                delay = 10 * attempt
                logger.info(
                    "[aca] RBAC retry %d/3 for %s in %ds ...",
                    attempt, label, delay,
                )
                time.sleep(delay)
            ok, _msg = az.ok(
                "role", "assignment", "create",
                "--assignee", mi_principal_id,
                "--role", role,
                "--scope", rg_scope,
            )
            if ok or "already exists" in (az.last_stderr or "").lower():
                assigned = True
                break
        if assigned:
            steps.append({"step": f"rbac_{label}", "status": "ok",
                          "detail": f"{role} on {resource_group}"})
        else:
            steps.append({"step": f"rbac_{label}", "status": "failed",
                          "detail": az.last_stderr})

    session_scope = _session_pool_scope(sub_id)
    if session_scope:
        label = _SESSION_EXECUTOR_ROLE.lower().replace(" ", "_")
        assigned = False
        for attempt in range(4):
            if attempt:
                delay = 10 * attempt
                logger.info(
                    "[aca] RBAC retry %d/3 for %s in %ds ...",
                    attempt, label, delay,
                )
                time.sleep(delay)
            ok, _msg = az.ok(
                "role", "assignment", "create",
                "--assignee", mi_principal_id,
                "--role", _SESSION_EXECUTOR_ROLE,
                "--scope", session_scope,
            )
            if ok or "already exists" in (az.last_stderr or "").lower():
                assigned = True
                break
        if assigned:
            steps.append({"step": f"rbac_{label}", "status": "ok",
                          "detail": f"{_SESSION_EXECUTOR_ROLE} on session pool"})
        else:
            steps.append({"step": f"rbac_{label}", "status": "failed",
                          "detail": az.last_stderr})


def ensure_aca_environment(
    az: AzureCLI,
    resource_group: str,
    location: str,
    steps: list[dict],
    rec: DeploymentRecord,
) -> tuple[str, str]:
    """Create an ACA environment.  Returns ``(env_name, env_id)``."""
    logger.info("[aca] Step 7/10: Creating ACA environment ...")
    env_name = f"{_ENV_NAME_PREFIX}-{secrets.token_hex(4)}"

    result = az.json(
        "containerapp", "env", "create",
        "--name", env_name,
        "--resource-group", resource_group,
        "--location", location,
    )
    if not isinstance(result, dict):
        steps.append({
            "step": "aca_environment", "status": "failed",
            "detail": az.last_stderr,
        })
        return "", ""

    env_id = result.get("id", "")
    steps.append({"step": "aca_environment", "status": "ok", "detail": env_name})
    rec.add_resource("aca_environment", resource_group, env_name,
                     "Container Apps environment")
    return env_name, env_id


def ensure_runtime_app(
    az: AzureCLI,
    resource_group: str,
    env_id: str,
    acr_name: str,
    mi_id: str,
    mi_client_id: str,
    acr_user: str,
    acr_pass: str,
    env_vars: dict[str, str],
    image_tag: str,
    runtime_port: int,
    steps: list[dict],
    rec: DeploymentRecord,
) -> str:
    """Create the runtime container app.  Returns the FQDN, or ``""`` on failure."""
    app_name = "polyclaw-runtime"
    admin_secret = cfg.admin_secret or secrets.token_urlsafe(24)
    image = f"{acr_name}.azurecr.io/{_IMAGE_NAME}:{image_tag}"

    logger.info("[aca] Step 8/10: Creating runtime container app ...")

    _SECRET_ENV_KEYS = frozenset({
        "RUNTIME_SP_PASSWORD", "ACS_CALLBACK_TOKEN",
        "GITHUB_TOKEN", "BOT_APP_PASSWORD",
        "ACS_CONNECTION_STRING", "AZURE_OPENAI_API_KEY",
    })
    _SKIP = frozenset({
        "POLYCLAW_MODE", "POLYCLAW_DATA_DIR", "ADMIN_PORT",
        "ADMIN_SECRET", "POLYCLAW_CONTAINER", "POLYCLAW_USE_MI",
        "AZURE_CLIENT_ID",
    }) | _SECRET_ENV_KEYS
    aca_secrets: dict[str, str] = {
        "admin-secret": admin_secret,
    }
    for env_key in _SECRET_ENV_KEYS:
        secret_name = env_key.lower().replace("_", "-")
        value = env_vars.get(env_key, "")
        if value:
            aca_secrets[secret_name] = value

    env_pairs = [
        "POLYCLAW_MODE=runtime",
        f"ADMIN_PORT={runtime_port}",
        "ADMIN_SECRET=secretref:admin-secret",
        "POLYCLAW_CONTAINER=1",
        "POLYCLAW_USE_MI=1",
        f"AZURE_CLIENT_ID={mi_client_id}",
    ]
    for env_key in sorted(_SECRET_ENV_KEYS):
        secret_name = env_key.lower().replace("_", "-")
        if secret_name in aca_secrets:
            env_pairs.append(f"{env_key}=secretref:{secret_name}")

    for key, value in sorted(env_vars.items()):
        if key not in _SKIP and value:
            env_pairs.append(f"{key}={value}")

    logger.info("[aca] Container env vars: %d total (%d via ACA secrets)",
                len(env_pairs), len(aca_secrets))

    secret_pairs = [f"{name}={value}" for name, value in sorted(aca_secrets.items())]

    create_args: list[str] = [
        "containerapp", "create",
        "--name", app_name,
        "--resource-group", resource_group,
        "--environment", env_id,
        "--image", image,
        "--cpu", "2", "--memory", "4Gi",
        "--min-replicas", "1", "--max-replicas", "1",
        "--ingress", "external",
        "--target-port", str(runtime_port),
        "--registry-server", f"{acr_name}.azurecr.io",
        "--registry-username", acr_user,
        "--registry-password", acr_pass,
        "--secrets", *secret_pairs,
        "--env-vars", *env_pairs,
    ]

    result = az.json(*create_args)
    if not isinstance(result, dict):
        detail = az.last_stderr
        logger.error("[aca] containerapp create failed: %s", detail[:1000])
        steps.append({
            "step": "runtime_container_app", "status": "failed",
            "detail": detail[:500],
        })
        return ""

    logger.info("[aca] Assigning managed identity to container app ...")
    id_ok, id_msg = az.ok(
        "containerapp", "identity", "assign",
        "--name", app_name,
        "--resource-group", resource_group,
        "--user-assigned", mi_id,
    )
    if not id_ok:
        logger.warning("[aca] MI assignment failed (non-fatal): %s", id_msg)

    fqdn = result.get("properties", {}).get("configuration", {}).get(
        "ingress", {}
    ).get("fqdn", "")

    if fqdn:
        bot_endpoint = f"https://{fqdn}/api/messages"
        az.ok(
            "containerapp", "update",
            "--name", app_name,
            "--resource-group", resource_group,
            "--set-env-vars", f"BOT_ENDPOINT={bot_endpoint}",
        )

    steps.append({"step": "runtime_container_app", "status": "ok", "detail": fqdn})
    rec.add_resource("container_app", resource_group, app_name,
                     "Runtime data plane (MI-scoped)")
    return fqdn


def configure_ip_whitelist(
    az: AzureCLI,
    resource_group: str,
) -> list[dict[str, Any]]:
    """Restrict the runtime container's ingress to the deployer's IP."""
    ip_steps: list[dict[str, Any]] = []

    public_ip = detect_public_ip()
    if not public_ip:
        ip_steps.append({
            "step": "ip_whitelist",
            "status": "skipped",
            "detail": "Could not detect public IP -- runtime ingress unrestricted",
        })
        return ip_steps

    ok, msg = az.ok(
        "containerapp", "ingress", "access-restriction", "set",
        "--name", "polyclaw-runtime",
        "--resource-group", resource_group,
        "--rule-name", "allow-deployer",
        "--ip-address", f"{public_ip}/32",
        "--action", "Allow",
        "--description", "Allow deployer IP",
    )
    if ok:
        ip_steps.append({
            "step": "ip_whitelist",
            "status": "ok",
            "detail": f"Runtime restricted to {public_ip}/32",
        })
    else:
        ip_steps.append({
            "step": "ip_whitelist",
            "status": "warning",
            "detail": f"Could not set IP restriction: {msg}",
        })

    return ip_steps


def detect_public_ip() -> str:
    """Return the deployer's public IP address, or ``""`` if unavailable."""
    import urllib.request

    for url in (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://checkip.amazonaws.com",
    ):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                ip = resp.read().decode().strip()
                if ip and "." in ip:
                    return ip
        except Exception:
            continue
    return ""
