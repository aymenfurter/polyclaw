"""RBAC preflight checks."""

from __future__ import annotations

from typing import Any

from ..cloud.azure import AzureCLI
from .security_preflight import (
    IdentityInfo,
    PreflightCheck,
    PreflightResult,
    _ELEVATED_ROLES,
    add_check as _add,
)


def check_rbac_list(
    az: AzureCLI, result: PreflightResult, info: IdentityInfo,
) -> list[dict[str, Any]] | None:
    assignee = info.get("assignee", "")
    if not assignee:
        return None

    cmd = f"az role assignment list --assignee {assignee} --all"
    assignments = az.json(
        "role", "assignment", "list", "--assignee", assignee, "--all",
    )
    if not isinstance(assignments, list):
        _add(
            result, id="rbac_assignments_list", category="rbac",
            name="RBAC Assignments Retrieved",
            status="fail",
            detail="Could not list RBAC assignments",
            evidence=az.last_stderr or "No response",
            command=cmd,
        )
        return None

    summary = ", ".join(
        f"{a.get('roleDefinitionName', '?')} @ "
        f"{a.get('scope', '?').rsplit('/', 1)[-1]}"
        for a in assignments
    )
    _add(
        result, id="rbac_assignments_list", category="rbac",
        name="RBAC Assignments Retrieved",
        status="pass",
        detail=f"{len(assignments)} assignment(s): {summary}",
        evidence="\n".join(
            f"- {a.get('roleDefinitionName', '?')} on {a.get('scope', '?')}"
            for a in assignments
        ),
        command=cmd,
    )
    return assignments


def check_rbac_has_role(
    result: PreflightResult,
    assignments: list[dict[str, Any]],
    role_name: str,
    check_id: str,
    check_name: str,
    bot_rg: str,
    *,
    missing_severity: str = "fail",
    missing_detail: str = "",
) -> None:
    matching = [
        a for a in assignments
        if a.get("roleDefinitionName") == role_name
    ]
    if matching:
        scopes = [a.get("scope", "") for a in matching]
        _add(
            result, id=check_id, category="rbac",
            name=check_name,
            status="pass",
            detail=f"{role_name} assigned ({len(matching)} assignment(s))",
            evidence="\n".join(f"scope={s}" for s in scopes),
            command="Filtered from role assignment list",
        )
    else:
        detail = missing_detail or f"{role_name} NOT found in assignments"
        _add(
            result, id=check_id, category="rbac",
            name=check_name,
            status=missing_severity,
            detail=detail,
            evidence=(
                f"Expected '{role_name}' but not present "
                f"in {len(assignments)} assignment(s)"
            ),
            command="Filtered from role assignment list",
        )


def check_rbac_kv_access(
    result: PreflightResult,
    assignments: list[dict[str, Any]],
    info: IdentityInfo,
) -> None:
    kv_roles = [
        a for a in assignments
        if "key vault" in (a.get("roleDefinitionName") or "").lower()
    ]

    if not kv_roles:
        _add(
            result, id="rbac_kv_access", category="rbac",
            name="Key Vault Access Role",
            status="warn",
            detail="No Key Vault role assignment found",
            evidence=f"Checked {len(assignments)} assignments for 'Key Vault' roles",
            command="Filtered from role assignment list",
        )
        return

    role_names = [a.get("roleDefinitionName", "?") for a in kv_roles]
    has_officer = "Key Vault Secrets Officer" in role_names
    has_user = "Key Vault Secrets User" in role_names

    if info["strategy"] == "managed_identity":
        if has_user and not has_officer:
            status = "pass"
            detail = "Key Vault Secrets User (read-only) -- correct for MI"
        elif has_officer:
            status = "warn"
            detail = (
                "Key Vault Secrets Officer (read+write) -- "
                "consider restricting to Secrets User for runtime"
            )
        else:
            status = "pass"
            detail = f"Key Vault role: {', '.join(role_names)}"
    else:
        status = "pass"
        detail = f"Key Vault role: {', '.join(role_names)}"

    _add(
        result, id="rbac_kv_access", category="rbac",
        name="Key Vault Access Role",
        status=status,
        detail=detail,
        evidence="\n".join(
            f"- {a.get('roleDefinitionName', '?')} on {a.get('scope', '?')}"
            for a in kv_roles
        ),
        command="Filtered from role assignment list",
    )


def check_rbac_session_pool(
    result: PreflightResult, assignments: list[dict[str, Any]],
) -> None:
    from ...state.sandbox_config import SandboxConfigStore

    try:
        sandbox_store = SandboxConfigStore()
        sandbox_enabled = sandbox_store.enabled
        sandbox_configured = sandbox_store.is_provisioned
    except Exception:
        sandbox_enabled = False
        sandbox_configured = False

    matching = [
        a for a in assignments
        if "session" in (a.get("roleDefinitionName") or "").lower()
    ]
    if matching:
        names = [a.get("roleDefinitionName", "?") for a in matching]
        _add(
            result, id="rbac_session_pool", category="rbac",
            name="Session Pool Executor",
            status="pass",
            detail=f"Session role: {', '.join(names)}",
            evidence="\n".join(
                f"scope={a.get('scope', '?')}" for a in matching
            ),
            command="Filtered from role assignment list",
        )
    elif sandbox_enabled or sandbox_configured:
        _add(
            result, id="rbac_session_pool", category="rbac",
            name="Session Pool Executor",
            status="fail",
            detail=(
                "Azure ContainerApps Session Executor NOT found -- "
                "required for sandbox (HTTP 403 on file upload/execute)"
            ),
            evidence=f"Not present in {len(assignments)} assignment(s)",
            command="Filtered from role assignment list",
        )
    else:
        _add(
            result, id="rbac_session_pool", category="rbac",
            name="Session Pool Executor",
            status="warn",
            detail="ContainerApps Session Executor NOT found (needed if sandbox is enabled)",
            evidence=f"Not present in {len(assignments)} assignment(s)",
            command="Filtered from role assignment list",
        )


def check_rbac_no_elevated(
    result: PreflightResult, assignments: list[dict[str, Any]],
) -> None:
    elevated = [
        a for a in assignments
        if a.get("roleDefinitionName") in _ELEVATED_ROLES
    ]
    if not elevated:
        _add(
            result, id="rbac_no_elevated", category="rbac",
            name="No Elevated Roles",
            status="pass",
            detail="No Owner, Contributor, or User Access Administrator roles",
            evidence=(
                f"Checked {len(assignments)} assignment(s) against: "
                f"{', '.join(sorted(_ELEVATED_ROLES))}"
            ),
            command="Filtered from role assignment list",
        )
    else:
        _add(
            result, id="rbac_no_elevated", category="rbac",
            name="No Elevated Roles",
            status="fail",
            detail=(
                f"ELEVATED roles found: "
                f"{', '.join(a.get('roleDefinitionName', '?') for a in elevated)}"
            ),
            evidence="\n".join(
                f"- {a.get('roleDefinitionName', '?')} on {a.get('scope', '?')}"
                for a in elevated
            ),
            command="Filtered from role assignment list",
        )


def check_rbac_scope_contained(
    result: PreflightResult, assignments: list[dict[str, Any]],
) -> None:
    out_of_scope = [
        a for a in assignments
        if "/resourcegroups/" not in (a.get("scope") or "").lower()
    ]
    if not out_of_scope:
        _add(
            result, id="rbac_scope_contained", category="rbac",
            name="Scope Limited to Resource Group",
            status="pass",
            detail=(
                f"All {len(assignments)} assignment(s) scoped to "
                f"resource group level or below"
            ),
            evidence="\n".join(
                f"- {a.get('scope', '?')}" for a in assignments
            ) if assignments else "No assignments",
            command="Scope analysis from role assignment list",
        )
    else:
        _add(
            result, id="rbac_scope_contained", category="rbac",
            name="Scope Limited to Resource Group",
            status="fail",
            detail=(
                f"{len(out_of_scope)} assignment(s) at subscription or management "
                f"group level"
            ),
            evidence="\n".join(
                f"- {a.get('roleDefinitionName', '?')} at {a.get('scope', '?')}"
                for a in out_of_scope
            ),
            command="Scope analysis from role assignment list",
        )
