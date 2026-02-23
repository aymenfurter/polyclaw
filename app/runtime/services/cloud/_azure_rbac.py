"""Shared Azure RBAC constants and helpers used across deployment and identity modules."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Common identity / image names.
MI_NAME = "polyclaw-runtime-mi"
IMAGE_NAME = "polyclaw"

# RBAC role names used for runtime identity scoping.
BOT_CONTRIBUTOR_ROLE = "Azure Bot Service Contributor Role"
RG_READER_ROLE = "Reader"
KV_SECRETS_ROLE = "Key Vault Secrets Officer"
SESSION_EXECUTOR_ROLE = "Azure ContainerApps Session Executor"


def session_pool_scope(subscription_id: str) -> str | None:
    """Return the ARM resource scope for the ACA session pool, or ``None``.

    The session pool id is stored in ``sandbox.json`` after provisioning.
    Shared between ``runtime_identity`` and ``aca_provision``.
    """
    from ...state.sandbox_config import SandboxConfigStore

    try:
        store = SandboxConfigStore()
        pool_id = store.pool_id
        if pool_id:
            return pool_id
        rg = store.resource_group
        name = store.pool_name
        if rg and name:
            return (
                f"/subscriptions/{subscription_id}/resourceGroups/{rg}"
                f"/providers/Microsoft.App/sessionPools/{name}"
            )
    except Exception as exc:
        logger.debug("Could not resolve session pool scope: %s", exc)
    return None
