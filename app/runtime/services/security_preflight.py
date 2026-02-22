"""Security preflight checker -- verifiable runtime identity and secret isolation checks.

Every check runs a real command or environment inspection and reports evidence.
No static claims -- every assertion is verified at runtime.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config.settings import cfg
from .azure import AzureCLI

logger = logging.getLogger(__name__)

# Elevated RBAC roles that the runtime identity should never hold.
_ELEVATED_ROLES = frozenset({
    "Owner",
    "Contributor",
    "User Access Administrator",
    "Role Based Access Control Administrator",
})


@dataclass
class PreflightCheck:
    """Result of a single security preflight check."""

    id: str
    category: str
    name: str
    status: str = "pending"  # pending | pass | fail | warn | skip
    detail: str = ""
    evidence: str = ""
    command: str = ""


@dataclass
class PreflightResult:
    """Aggregated result of all preflight checks."""

    checks: list[PreflightCheck] = field(default_factory=list)
    run_at: str = ""
    passed: int = 0
    failed: int = 0
    warnings: int = 0
    skipped: int = 0


class SecurityPreflightChecker:
    """Run verifiable security checks against the runtime identity and secrets."""

    def __init__(self, az: AzureCLI) -> None:
        self._az = az

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_all(self) -> PreflightResult:
        """Execute all security preflight checks and return evidence."""
        result = PreflightResult(run_at=datetime.now(timezone.utc).isoformat())

        # Gate: is Azure CLI logged in?
        if not self._check_azure_logged_in(result):
            self._skip_azure_checks(result)
            self._run_secret_checks(result)
            self._tally(result)
            return result

        # Identity verification
        identity = self._check_identity_configured(result)
        if identity:
            self._check_identity_valid(result, identity)
            self._check_credential_expiry(result, identity)

            # RBAC verification
            assignments = self._check_rbac_list(result, identity)
            if assignments is not None:
                bot_rg = cfg.env.read("BOT_RESOURCE_GROUP") or ""
                self._check_rbac_has_role(
                    result, assignments, "Azure Bot Service Contributor Role",
                    "rbac_bot_contributor", "Azure Bot Service Contributor Role", bot_rg,
                )
                self._check_rbac_has_role(
                    result, assignments, "Reader",
                    "rbac_reader", "Reader Role", bot_rg,
                )
                self._check_rbac_kv_access(result, assignments, identity)
                if identity.get("strategy") == "managed_identity":
                    self._check_rbac_has_role(
                        result, assignments, "Cognitive Services OpenAI User",
                        "rbac_aoai_user", "Azure OpenAI Access",
                        "",
                        missing_severity="warn",
                        missing_detail="Needed for identity-auth voice",
                    )
                self._check_rbac_session_pool(result, assignments)
                self._check_rbac_no_elevated(result, assignments)
                self._check_rbac_scope_contained(result, assignments)

        # Secret isolation
        self._run_secret_checks(result)

        self._tally(result)
        return result

    @staticmethod
    def to_dict(result: PreflightResult) -> dict[str, Any]:
        """Serialize a *PreflightResult* to a JSON-safe dict."""
        return {
            "checks": [asdict(c) for c in result.checks],
            "run_at": result.run_at,
            "passed": result.passed,
            "failed": result.failed,
            "warnings": result.warnings,
            "skipped": result.skipped,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tally(result: PreflightResult) -> None:
        for c in result.checks:
            if c.status == "pass":
                result.passed += 1
            elif c.status == "fail":
                result.failed += 1
            elif c.status == "warn":
                result.warnings += 1
            elif c.status == "skip":
                result.skipped += 1

    def _add(self, result: PreflightResult, **kwargs: Any) -> PreflightCheck:
        check = PreflightCheck(**kwargs)
        result.checks.append(check)
        return check

    # ------------------------------------------------------------------
    # Azure login gate
    # ------------------------------------------------------------------

    def _check_azure_logged_in(self, result: PreflightResult) -> bool:
        cmd = "az account show"
        account = self._az.json("account", "show", quiet=True)
        if isinstance(account, dict) and account.get("id"):
            sub = account.get("name", account.get("id", "?"))
            self._add(
                result, id="azure_logged_in", category="identity",
                name="Azure CLI Authenticated",
                status="pass",
                detail=f"Logged in to subscription: {sub}",
                evidence=f"subscription={sub}\ntenantId={account.get('tenantId', '?')}",
                command=cmd,
            )
            return True
        self._add(
            result, id="azure_logged_in", category="identity",
            name="Azure CLI Authenticated",
            status="fail",
            detail="Not logged in -- RBAC and identity checks require Azure CLI auth",
            evidence=self._az.last_stderr or "No response",
            command=cmd,
        )
        return False

    def _skip_azure_checks(self, result: PreflightResult) -> None:
        for check_id, name, cat in [
            ("identity_configured", "Runtime Identity Configured", "identity"),
            ("identity_valid", "Identity Exists in Azure AD", "identity"),
            ("identity_credential_expiry", "Credential Expiry", "identity"),
            ("rbac_assignments_list", "RBAC Assignments", "rbac"),
            ("rbac_bot_contributor", "Azure Bot Service Contributor Role", "rbac"),
            ("rbac_reader", "Reader Role", "rbac"),
            ("rbac_kv_access", "Key Vault Access Role", "rbac"),
            ("rbac_session_pool", "Session Pool Executor", "rbac"),
            ("rbac_no_elevated", "No Elevated Roles", "rbac"),
            ("rbac_scope_contained", "Scope Limited to Resource Group", "rbac"),
        ]:
            self._add(
                result, id=check_id, category=cat, name=name,
                status="skip",
                detail="Skipped -- Azure CLI not authenticated",
                command="",
            )

    # ------------------------------------------------------------------
    # Identity checks
    # ------------------------------------------------------------------

    def _check_identity_configured(
        self, result: PreflightResult,
    ) -> dict[str, Any] | None:
        sp_app_id = cfg.env.read("RUNTIME_SP_APP_ID")
        mi_client_id = cfg.env.read("ACA_MI_CLIENT_ID")
        mi_resource_id = cfg.env.read("ACA_MI_RESOURCE_ID")

        if mi_client_id:
            self._add(
                result, id="identity_configured", category="identity",
                name="Runtime Identity Configured",
                status="pass",
                detail=f"User-assigned managed identity: client_id={mi_client_id}",
                evidence=(
                    f"ACA_MI_CLIENT_ID={mi_client_id}\n"
                    f"ACA_MI_RESOURCE_ID={mi_resource_id}"
                ),
                command="env: ACA_MI_CLIENT_ID, ACA_MI_RESOURCE_ID",
            )
            return {
                "strategy": "managed_identity",
                "client_id": mi_client_id,
                "resource_id": mi_resource_id,
                "assignee": mi_client_id,
            }

        if sp_app_id:
            sp_tenant = cfg.env.read("RUNTIME_SP_TENANT")
            has_pw = bool(cfg.env.read("RUNTIME_SP_PASSWORD"))
            self._add(
                result, id="identity_configured", category="identity",
                name="Runtime Identity Configured",
                status="pass",
                detail=f"Scoped service principal: app_id={sp_app_id}",
                evidence=(
                    f"RUNTIME_SP_APP_ID={sp_app_id}\n"
                    f"RUNTIME_SP_TENANT={sp_tenant}\n"
                    f"RUNTIME_SP_PASSWORD={'***' if has_pw else 'MISSING'}"
                ),
                command="env: RUNTIME_SP_APP_ID, RUNTIME_SP_TENANT, RUNTIME_SP_PASSWORD",
            )
            return {
                "strategy": "sp",
                "app_id": sp_app_id,
                "tenant": sp_tenant,
                "assignee": sp_app_id,
            }

        self._add(
            result, id="identity_configured", category="identity",
            name="Runtime Identity Configured",
            status="skip",
            detail="No runtime identity configured (RUNTIME_SP_* and ACA_MI_* absent)",
            evidence="RUNTIME_SP_APP_ID=(empty)\nACA_MI_CLIENT_ID=(empty)",
            command="env: RUNTIME_SP_APP_ID, ACA_MI_CLIENT_ID",
        )
        return None

    def _check_identity_valid(
        self, result: PreflightResult, info: dict[str, Any],
    ) -> None:
        if info["strategy"] == "sp":
            app_id = info["app_id"]
            cmd = f"az ad sp show --id {app_id}"
            sp = self._az.json("ad", "sp", "show", "--id", app_id)
            if isinstance(sp, dict) and sp.get("appId"):
                display = sp.get("displayName", "?")
                self._add(
                    result, id="identity_valid", category="identity",
                    name="Service Principal Exists in Azure AD",
                    status="pass",
                    detail=f"{display} ({app_id})",
                    evidence=(
                        f"displayName={display}\n"
                        f"appId={app_id}\n"
                        f"objectId={sp.get('id', '?')}"
                    ),
                    command=cmd,
                )
            else:
                self._add(
                    result, id="identity_valid", category="identity",
                    name="Service Principal Exists in Azure AD",
                    status="fail",
                    detail=f"SP not found: {app_id}",
                    evidence=self._az.last_stderr or "No response",
                    command=cmd,
                )
        else:
            resource_id = info.get("resource_id", "")
            if not resource_id:
                self._add(
                    result, id="identity_valid", category="identity",
                    name="Managed Identity Exists",
                    status="skip", detail="No MI resource ID configured",
                    command="",
                )
                return
            cmd = f"az identity show --ids {resource_id}"
            mi = self._az.json("identity", "show", "--ids", resource_id)
            if isinstance(mi, dict) and mi.get("clientId"):
                self._add(
                    result, id="identity_valid", category="identity",
                    name="Managed Identity Exists",
                    status="pass",
                    detail=f"{mi.get('name', '?')} (client={mi.get('clientId', '?')})",
                    evidence=(
                        f"name={mi.get('name', '?')}\n"
                        f"clientId={mi.get('clientId', '?')}\n"
                        f"principalId={mi.get('principalId', '?')}"
                    ),
                    command=cmd,
                )
            else:
                self._add(
                    result, id="identity_valid", category="identity",
                    name="Managed Identity Exists",
                    status="fail",
                    detail=f"MI not found: {resource_id}",
                    evidence=self._az.last_stderr or "No response",
                    command=cmd,
                )

    def _check_credential_expiry(
        self, result: PreflightResult, info: dict[str, Any],
    ) -> None:
        if info["strategy"] != "sp":
            self._add(
                result, id="identity_credential_expiry", category="identity",
                name="Credential Expiry",
                status="pass",
                detail="Managed identities do not have expiring credentials",
                command="(not applicable for MI)",
            )
            return

        app_id = info["app_id"]
        cmd = f"az ad app credential list --id {app_id}"
        creds = self._az.json("ad", "app", "credential", "list", "--id", app_id)
        if not isinstance(creds, list) or not creds:
            self._add(
                result, id="identity_credential_expiry", category="identity",
                name="Credential Expiry",
                status="warn",
                detail="Could not retrieve credential list",
                evidence=self._az.last_stderr or "Empty response",
                command=cmd,
            )
            return

        latest = max(creds, key=lambda c: c.get("endDateTime", ""))
        end = latest.get("endDateTime", "")
        now = datetime.now(timezone.utc).isoformat()

        if end and end > now:
            self._add(
                result, id="identity_credential_expiry", category="identity",
                name="Credential Expiry",
                status="pass",
                detail=f"Valid until {end}",
                evidence=f"endDateTime={end}\nnow={now}\ncredentials_count={len(creds)}",
                command=cmd,
            )
        else:
            self._add(
                result, id="identity_credential_expiry", category="identity",
                name="Credential Expiry",
                status="fail",
                detail=f"Credential EXPIRED: {end}",
                evidence=f"endDateTime={end}\nnow={now}",
                command=cmd,
            )

    # ------------------------------------------------------------------
    # RBAC checks
    # ------------------------------------------------------------------

    def _check_rbac_list(
        self, result: PreflightResult, info: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        assignee = info.get("assignee", "")
        if not assignee:
            return None

        cmd = f"az role assignment list --assignee {assignee} --all"
        assignments = self._az.json(
            "role", "assignment", "list", "--assignee", assignee, "--all",
        )
        if not isinstance(assignments, list):
            self._add(
                result, id="rbac_assignments_list", category="rbac",
                name="RBAC Assignments Retrieved",
                status="fail",
                detail="Could not list RBAC assignments",
                evidence=self._az.last_stderr or "No response",
                command=cmd,
            )
            return None

        summary = ", ".join(
            f"{a.get('roleDefinitionName', '?')} @ "
            f"{a.get('scope', '?').rsplit('/', 1)[-1]}"
            for a in assignments
        )
        self._add(
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

    def _check_rbac_has_role(
        self,
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
            self._add(
                result, id=check_id, category="rbac",
                name=check_name,
                status="pass",
                detail=f"{role_name} assigned ({len(matching)} assignment(s))",
                evidence="\n".join(f"scope={s}" for s in scopes),
                command=f"Filtered from role assignment list",
            )
        else:
            detail = missing_detail or f"{role_name} NOT found in assignments"
            self._add(
                result, id=check_id, category="rbac",
                name=check_name,
                status=missing_severity,
                detail=detail,
                evidence=(
                    f"Expected '{role_name}' but not present "
                    f"in {len(assignments)} assignment(s)"
                ),
                command=f"Filtered from role assignment list",
            )

    def _check_rbac_kv_access(
        self,
        result: PreflightResult,
        assignments: list[dict[str, Any]],
        info: dict[str, Any],
    ) -> None:
        kv_roles = [
            a for a in assignments
            if "key vault" in (a.get("roleDefinitionName") or "").lower()
        ]

        if not kv_roles:
            self._add(
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
            # SP may legitimately have Officer (used during provisioning)
            status = "pass"
            detail = f"Key Vault role: {', '.join(role_names)}"

        self._add(
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

    def _check_rbac_session_pool(
        self, result: PreflightResult, assignments: list[dict[str, Any]],
    ) -> None:
        from ..state.sandbox_config import SandboxConfigStore

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
            self._add(
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
            self._add(
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
            self._add(
                result, id="rbac_session_pool", category="rbac",
                name="Session Pool Executor",
                status="warn",
                detail="ContainerApps Session Executor NOT found (needed if sandbox is enabled)",
                evidence=f"Not present in {len(assignments)} assignment(s)",
                command="Filtered from role assignment list",
            )

    def _check_rbac_no_elevated(
        self, result: PreflightResult, assignments: list[dict[str, Any]],
    ) -> None:
        elevated = [
            a for a in assignments
            if a.get("roleDefinitionName") in _ELEVATED_ROLES
        ]
        if not elevated:
            self._add(
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
            self._add(
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

    def _check_rbac_scope_contained(
        self, result: PreflightResult, assignments: list[dict[str, Any]],
    ) -> None:
        out_of_scope = [
            a for a in assignments
            if "/resourcegroups/" not in (a.get("scope") or "").lower()
        ]
        if not out_of_scope:
            self._add(
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
            self._add(
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

    # ------------------------------------------------------------------
    # Secret isolation checks
    # ------------------------------------------------------------------

    def _run_secret_checks(self, result: PreflightResult) -> None:
        self._check_admin_cli_isolated(result)
        self._check_no_github_in_runtime(result)
        self._check_bot_credentials(result)
        self._check_admin_secret(result)
        self._check_kv_reachable(result)
        self._check_acs_credential(result)
        self._check_aoai_credential(result)
        self._check_sp_creds_written(result)

    def _check_admin_cli_isolated(self, result: PreflightResult) -> None:
        admin_home = os.environ.get("POLYCLAW_ADMIN_HOME", "/admin-home")
        azure_dir = Path(admin_home) / ".azure"
        mode = cfg.server_mode.value

        if mode == "admin":
            exists = azure_dir.exists()
            self._add(
                result, id="secret_admin_cli_isolated", category="secrets",
                name="Admin CLI Session Isolated",
                status="pass" if exists else "warn",
                detail=(
                    f"Azure CLI config at {azure_dir}: "
                    f"{'present' if exists else 'not found'}"
                ),
                evidence=(
                    f"HOME={os.environ.get('HOME', '?')}\n"
                    f"AZURE_CONFIG_DIR={os.environ.get('AZURE_CONFIG_DIR', '?')}\n"
                    f"exists={exists}"
                ),
                command=f"os.path.exists({azure_dir})",
            )
        elif mode == "runtime":
            exists = azure_dir.exists()
            self._add(
                result, id="secret_admin_cli_isolated", category="secrets",
                name="Admin CLI Session Isolated",
                status="pass" if not exists else "fail",
                detail=(
                    "Admin CLI config not accessible from runtime"
                    if not exists
                    else f"RISK: Admin CLI config accessible at {azure_dir}"
                ),
                evidence=(
                    f"HOME={os.environ.get('HOME', '?')}\n"
                    f"{azure_dir} exists={exists}"
                ),
                command=f"os.path.exists({azure_dir})",
            )
        else:
            self._add(
                result, id="secret_admin_cli_isolated", category="secrets",
                name="Admin CLI Session Isolated",
                status="warn",
                detail=(
                    "Combined mode -- admin and runtime share the same "
                    "container (no credential isolation)"
                ),
                evidence=f"POLYCLAW_SERVER_MODE={mode}",
                command="cfg.server_mode",
            )

    def _check_no_github_in_runtime(self, result: PreflightResult) -> None:
        env_data = cfg.env.read_all()
        gh_token = env_data.get("GITHUB_TOKEN", "")
        gh2 = env_data.get("GH_TOKEN", "")
        mode = cfg.server_mode.value

        if mode == "runtime":
            has = bool(gh_token or gh2)
            self._add(
                result, id="secret_no_github_runtime", category="secrets",
                name="No GitHub Token in Runtime",
                status="fail" if has else "pass",
                detail=(
                    "GitHub token NOT present in runtime environment"
                    if not has
                    else "RISK: GitHub token accessible in runtime env"
                ),
                evidence=(
                    f"GITHUB_TOKEN={'set (' + str(len(gh_token)) + ' chars)' if gh_token else 'empty'}\n"
                    f"GH_TOKEN={'set' if gh2 else 'empty'}"
                ),
                command="env: GITHUB_TOKEN, GH_TOKEN",
            )
        elif mode == "admin":
            has = bool(gh_token or gh2)
            self._add(
                result, id="secret_no_github_runtime", category="secrets",
                name="GitHub Token (Admin Only)",
                status="pass",
                detail=f"GitHub token on admin: {'present' if has else 'not configured'}",
                evidence=(
                    f"GITHUB_TOKEN={'set' if gh_token else 'empty'}\n"
                    f"GH_TOKEN={'set' if gh2 else 'empty'}"
                ),
                command="env: GITHUB_TOKEN, GH_TOKEN",
            )
        else:
            self._add(
                result, id="secret_no_github_runtime", category="secrets",
                name="GitHub Token Isolation",
                status="warn",
                detail="Combined mode -- GitHub token shared with agent runtime",
                evidence=f"POLYCLAW_SERVER_MODE={mode}",
                command="cfg.server_mode + env",
            )

    def _check_bot_credentials(self, result: PreflightResult) -> None:
        env_data = cfg.env.read_all()
        app_id = env_data.get("BOT_APP_ID", "")
        app_pw = env_data.get("BOT_APP_PASSWORD", "")
        both = bool(app_id and app_pw)

        self._add(
            result, id="secret_bot_creds", category="secrets",
            name="Bot Credentials Present",
            status="pass" if both else ("warn" if app_id else "skip"),
            detail=(
                f"BOT_APP_ID={'set' if app_id else 'missing'}, "
                f"BOT_APP_PASSWORD={'set' if app_pw else 'missing'}"
            ),
            evidence=(
                f"BOT_APP_ID={app_id[:12] + '...' if app_id else '(empty)'}\n"
                f"BOT_APP_PASSWORD={'***' if app_pw else '(empty)'}"
            ),
            command="env: BOT_APP_ID, BOT_APP_PASSWORD",
        )

    def _check_admin_secret(self, result: PreflightResult) -> None:
        secret = cfg.admin_secret
        self._add(
            result, id="secret_admin_secret", category="secrets",
            name="Admin Secret Configured",
            status="pass" if secret else "fail",
            detail=(
                f"ADMIN_SECRET set ({len(secret)} chars)"
                if secret
                else "ADMIN_SECRET MISSING"
            ),
            evidence=f"ADMIN_SECRET={'***' if secret else '(empty)'}\nlength={len(secret) if secret else 0}",
            command="env: ADMIN_SECRET",
        )

    def _check_kv_reachable(self, result: PreflightResult) -> None:
        from ..services.keyvault import kv as _kv

        if not _kv.enabled:
            self._add(
                result, id="secret_kv_reachable", category="secrets",
                name="Key Vault Reachable",
                status="skip",
                detail="Key Vault not configured",
                evidence=f"KEY_VAULT_URL={cfg.env.read('KEY_VAULT_URL') or '(empty)'}",
                command="keyvault.enabled",
            )
            return

        try:
            secrets_list = _kv.list_secrets()
            self._add(
                result, id="secret_kv_reachable", category="secrets",
                name="Key Vault Reachable",
                status="pass",
                detail=f"Key Vault accessible, {len(secrets_list)} secret(s) readable",
                evidence=f"url={_kv.url}\nsecrets_count={len(secrets_list)}",
                command="keyvault.list_secrets()",
            )
        except Exception as exc:
            self._add(
                result, id="secret_kv_reachable", category="secrets",
                name="Key Vault Reachable",
                status="fail",
                detail=f"Key Vault NOT reachable: {exc}",
                evidence=f"url={_kv.url}\nerror={exc}",
                command="keyvault.list_secrets()",
            )

    def _check_acs_credential(self, result: PreflightResult) -> None:
        conn = cfg.acs_connection_string
        if conn:
            parts = {
                k.strip().lower(): v.strip()
                for k, _, v in (seg.partition("=") for seg in conn.split(";") if "=" in seg)
            }
            has_ep = bool(parts.get("endpoint"))
            self._add(
                result, id="secret_acs_present", category="secrets",
                name="ACS Connection String",
                status="pass" if has_ep else "warn",
                detail=(
                    f"ACS connection string "
                    f"{'well-formed' if has_ep else 'malformed (missing endpoint)'}"
                ),
                evidence=f"ACS_CONNECTION_STRING=***({len(conn)} chars)\nhas_endpoint={has_ep}",
                command="env: ACS_CONNECTION_STRING",
            )
        else:
            self._add(
                result, id="secret_acs_present", category="secrets",
                name="ACS Connection String",
                status="skip",
                detail="ACS not configured",
                evidence="ACS_CONNECTION_STRING=(empty)",
                command="env: ACS_CONNECTION_STRING",
            )

    def _check_aoai_credential(self, result: PreflightResult) -> None:
        endpoint = cfg.azure_openai_endpoint
        key = cfg.azure_openai_api_key

        if endpoint:
            self._add(
                result, id="secret_aoai_present", category="secrets",
                name="Azure OpenAI Configuration",
                status="pass",
                detail=f"Endpoint configured, {'API key' if key else 'identity auth'} mode",
                evidence=(
                    f"AZURE_OPENAI_ENDPOINT={endpoint}\n"
                    f"AZURE_OPENAI_API_KEY={'***' if key else '(identity-auth)'}"
                ),
                command="env: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY",
            )
        else:
            self._add(
                result, id="secret_aoai_present", category="secrets",
                name="Azure OpenAI Configuration",
                status="skip",
                detail="Azure OpenAI not configured",
                evidence="AZURE_OPENAI_ENDPOINT=(empty)",
                command="env: AZURE_OPENAI_ENDPOINT",
            )

    def _check_sp_creds_written(self, result: PreflightResult) -> None:
        env_data = cfg.env.read_all()
        sp_id = env_data.get("RUNTIME_SP_APP_ID", "")
        sp_pw = env_data.get("RUNTIME_SP_PASSWORD", "")
        sp_tenant = env_data.get("RUNTIME_SP_TENANT", "")

        if not sp_id:
            mi_id = env_data.get("ACA_MI_CLIENT_ID", "")
            if mi_id:
                self._add(
                    result, id="secret_identity_creds", category="secrets",
                    name="Runtime Identity Credentials in .env",
                    status="pass",
                    detail="Managed identity credentials written to .env",
                    evidence=(
                        f"ACA_MI_CLIENT_ID={mi_id}\n"
                        f"ACA_MI_RESOURCE_ID={env_data.get('ACA_MI_RESOURCE_ID', '?')}"
                    ),
                    command="env: ACA_MI_CLIENT_ID, ACA_MI_RESOURCE_ID",
                )
            else:
                self._add(
                    result, id="secret_identity_creds", category="secrets",
                    name="Runtime Identity Credentials in .env",
                    status="skip",
                    detail="No runtime identity credentials in .env",
                    evidence="RUNTIME_SP_APP_ID=(empty)\nACA_MI_CLIENT_ID=(empty)",
                    command="env: RUNTIME_SP_APP_ID, ACA_MI_CLIENT_ID",
                )
            return

        all_set = bool(sp_id and sp_pw and sp_tenant)
        self._add(
            result, id="secret_identity_creds", category="secrets",
            name="SP Credentials in .env",
            status="pass" if all_set else "fail",
            detail=(
                f"app_id={'set' if sp_id else 'MISSING'}, "
                f"password={'set' if sp_pw else 'MISSING'}, "
                f"tenant={'set' if sp_tenant else 'MISSING'}"
            ),
            evidence=(
                f"RUNTIME_SP_APP_ID={sp_id[:12] + '...' if sp_id else '(empty)'}\n"
                f"RUNTIME_SP_PASSWORD={'***' if sp_pw else '(empty)'}\n"
                f"RUNTIME_SP_TENANT={sp_tenant or '(empty)'}"
            ),
            command="env: RUNTIME_SP_APP_ID, RUNTIME_SP_PASSWORD, RUNTIME_SP_TENANT",
        )
