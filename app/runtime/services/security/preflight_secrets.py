"""Secret-isolation preflight checks."""

from __future__ import annotations

import os
from pathlib import Path

from ...config.settings import cfg
from .security_preflight import PreflightCheck, PreflightResult, add_check as _add


def run_secret_checks(result: PreflightResult) -> None:
    """Execute all secret-isolation checks."""
    check_admin_cli_isolated(result)
    check_no_github_in_runtime(result)
    check_bot_credentials(result)
    check_admin_secret(result)
    check_kv_reachable(result)
    check_acs_credential(result)
    check_aoai_credential(result)
    check_sp_creds_written(result)


def check_admin_cli_isolated(result: PreflightResult) -> None:
    admin_home = os.environ.get("POLYCLAW_ADMIN_HOME", "/admin-home")
    azure_dir = Path(admin_home) / ".azure"
    mode = cfg.server_mode.value

    if mode == "admin":
        exists = azure_dir.exists()
        _add(
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
        _add(
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
        _add(
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


def check_no_github_in_runtime(result: PreflightResult) -> None:
    env_data = cfg.env.read_all()
    gh_token = env_data.get("GITHUB_TOKEN", "")
    gh2 = env_data.get("GH_TOKEN", "")
    mode = cfg.server_mode.value

    if mode == "runtime":
        has = bool(gh_token or gh2)
        _add(
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
        _add(
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
        _add(
            result, id="secret_no_github_runtime", category="secrets",
            name="GitHub Token Isolation",
            status="warn",
            detail="Combined mode -- GitHub token shared with agent runtime",
            evidence=f"POLYCLAW_SERVER_MODE={mode}",
            command="cfg.server_mode + env",
        )


def check_bot_credentials(result: PreflightResult) -> None:
    env_data = cfg.env.read_all()
    app_id = env_data.get("BOT_APP_ID", "")
    app_pw = env_data.get("BOT_APP_PASSWORD", "")
    both = bool(app_id and app_pw)

    _add(
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


def check_admin_secret(result: PreflightResult) -> None:
    secret = cfg.admin_secret
    _add(
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


def check_kv_reachable(result: PreflightResult) -> None:
    from ..keyvault import kv as _kv

    if not _kv.enabled:
        _add(
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
        _add(
            result, id="secret_kv_reachable", category="secrets",
            name="Key Vault Reachable",
            status="pass",
            detail=f"Key Vault accessible, {len(secrets_list)} secret(s) readable",
            evidence=f"url={_kv.url}\nsecrets_count={len(secrets_list)}",
            command="keyvault.list_secrets()",
        )
    except Exception as exc:
        _add(
            result, id="secret_kv_reachable", category="secrets",
            name="Key Vault Reachable",
            status="fail",
            detail=f"Key Vault NOT reachable: {exc}",
            evidence=f"url={_kv.url}\nerror={exc}",
            command="keyvault.list_secrets()",
        )


def check_acs_credential(result: PreflightResult) -> None:
    conn = cfg.acs_connection_string
    if conn:
        parts = {
            k.strip().lower(): v.strip()
            for k, _, v in (seg.partition("=") for seg in conn.split(";") if "=" in seg)
        }
        has_ep = bool(parts.get("endpoint"))
        _add(
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
        _add(
            result, id="secret_acs_present", category="secrets",
            name="ACS Connection String",
            status="skip",
            detail="ACS not configured",
            evidence="ACS_CONNECTION_STRING=(empty)",
            command="env: ACS_CONNECTION_STRING",
        )


def check_aoai_credential(result: PreflightResult) -> None:
    endpoint = cfg.azure_openai_endpoint
    key = cfg.azure_openai_api_key

    if endpoint:
        _add(
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
        _add(
            result, id="secret_aoai_present", category="secrets",
            name="Azure OpenAI Configuration",
            status="skip",
            detail="Azure OpenAI not configured",
            evidence="AZURE_OPENAI_ENDPOINT=(empty)",
            command="env: AZURE_OPENAI_ENDPOINT",
        )


def check_sp_creds_written(result: PreflightResult) -> None:
    env_data = cfg.env.read_all()
    sp_id = env_data.get("RUNTIME_SP_APP_ID", "")
    sp_pw = env_data.get("RUNTIME_SP_PASSWORD", "")
    sp_tenant = env_data.get("RUNTIME_SP_TENANT", "")

    if not sp_id:
        mi_id = env_data.get("ACA_MI_CLIENT_ID", "")
        if mi_id:
            _add(
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
            _add(
                result, id="secret_identity_creds", category="secrets",
                name="Runtime Identity Credentials in .env",
                status="skip",
                detail="No runtime identity credentials in .env",
                evidence="RUNTIME_SP_APP_ID=(empty)\nACA_MI_CLIENT_ID=(empty)",
                command="env: RUNTIME_SP_APP_ID, ACA_MI_CLIENT_ID",
            )
        return

    all_set = bool(sp_id and sp_pw and sp_tenant)
    _add(
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
