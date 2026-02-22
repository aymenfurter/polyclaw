#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Container-only entrypoint
# ---------------------------------------------------------------------------

DATA_DIR="${POLYCLAW_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

MODE="${POLYCLAW_MODE:-auto}"

# ---------------------------------------------------------------------------
# HOME and Azure CLI credential isolation
#
# Admin container:
#   HOME = /admin-home (dedicated volume, never shared with runtime).
#   AZURE_CONFIG_DIR points here so `az login` creds are isolated.
#
# Runtime container:
#   HOME = /runtime-home (ephemeral, inside the container -- NOT on /data).
#   AZURE_CONFIG_DIR = /runtime-home/.azure -- populated at boot via
#   `az login --service-principal` using the scoped SP credentials that
#   the admin wrote to /data/.env.  This SP can ONLY manage Bot Service
#   in a single pre-existing resource group.
#
# Combined / legacy:
#   HOME = /data (backwards-compatible).
# ---------------------------------------------------------------------------

if [[ "$MODE" == "admin" ]]; then
    ADMIN_HOME="${POLYCLAW_ADMIN_HOME:-/admin-home}"
    mkdir -p "$ADMIN_HOME"
    export HOME="$ADMIN_HOME"
    export AZURE_CONFIG_DIR="$ADMIN_HOME/.azure"
elif [[ "$MODE" == "runtime" ]]; then
    RUNTIME_HOME="/runtime-home"
    mkdir -p "$RUNTIME_HOME"
    export HOME="$RUNTIME_HOME"
    export AZURE_CONFIG_DIR="$RUNTIME_HOME/.azure"
else
    # Combined / legacy -- everything in /data
    export HOME="$DATA_DIR"
    export AZURE_CONFIG_DIR="$DATA_DIR/.azure"
fi

# Clean stale copilot CLI runtime cache (forces re-download of matching version)
COPILOT_INSTALLED="$(copilot --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo '')"
if [[ -n "$COPILOT_INSTALLED" && -d "$HOME/.copilot/pkg" ]]; then
    # Remove mismatched versions in both arch-specific and universal directories
    find "$HOME/.copilot/pkg" -mindepth 2 -maxdepth 2 -type d \
        ! -name "$COPILOT_INSTALLED" -exec rm -rf {} + 2>/dev/null || true
    echo "Copilot CLI v${COPILOT_INSTALLED} -- runtime cache cleaned."
fi

# Load .env from the persistent data volume
if [[ -f "$DATA_DIR/.env" ]]; then
    set -a
    source "$DATA_DIR/.env"
    set +a
fi

# ---------------------------------------------------------------------------
# Stale RUNTIME_URL cleanup
#
# After an ACA deploy, RUNTIME_URL in .env points to an ACA FQDN.
# When the user switches back to local Docker mode, the admin container
# must proxy to the local runtime container (http://runtime:8080), not
# the old ACA host.  Detect this by checking: admin + Docker (no MI) +
# RUNTIME_URL contains azurecontainerapps.io  -->  reset to default.
# ---------------------------------------------------------------------------
if [[ "$MODE" == "admin" && -z "${POLYCLAW_USE_MI:-}" && "${RUNTIME_URL:-}" == *azurecontainerapps.io* ]]; then
    echo "Admin (Docker): clearing stale ACA RUNTIME_URL -- using http://runtime:8080"
    export RUNTIME_URL="http://runtime:8080"
    # Also remove the stale value from the persisted .env so it does not
    # leak back on subsequent restarts.
    if [[ -f "$DATA_DIR/.env" ]]; then
        sed -i '/^RUNTIME_URL=/d' "$DATA_DIR/.env"
    fi
fi

# ---------------------------------------------------------------------------
# Key Vault resolution and Azure identity
#
# Admin / combined -- eagerly resolve all @kv:* env vars via the Python
# keyvault_resolve helper (runs once, rewrites env vars, exits).
#
# Runtime -- the service principal (or ACA managed identity) has
# "Key Vault Secrets Officer" on the vault, so DefaultAzureCredential
# can resolve @kv:* secrets at runtime.  The flow is:
#   1. `az login --service-principal` (or --identity for ACA).
#   2. If login succeeds AND KEY_VAULT_URL is set, keep the @kv:* refs
#      untouched -- the Python KV client resolves them on demand.
#   3. If login fails (or no SP provisioned yet), strip @kv:* refs to
#      empty strings so the app degrades gracefully.
# ---------------------------------------------------------------------------

if [[ "$MODE" == "runtime" ]]; then

    # --- Step 1: authenticate the runtime identity -----------------------
    _RUNTIME_AUTH_OK=false

    if [[ -n "${POLYCLAW_USE_MI:-}" ]]; then
        echo "Runtime (ACA): managed identity detected (AZURE_CLIENT_ID=${AZURE_CLIENT_ID:-<not set>})"
        if timeout 30 az login --identity --output none 2>/dev/null; then
            echo "Runtime (ACA): az CLI authenticated via managed identity."
            _RUNTIME_AUTH_OK=true
        else
            echo "Runtime (ACA): WARNING -- managed identity login failed (or timed out)."
            # Fall back to service principal if credentials are available
            if [[ -n "${RUNTIME_SP_APP_ID:-}" && -n "${RUNTIME_SP_PASSWORD:-}" && -n "${RUNTIME_SP_TENANT:-}" ]]; then
                echo "Runtime (ACA): Falling back to service principal..."
                if az login --service-principal \
                    -u "$RUNTIME_SP_APP_ID" \
                    -p "$RUNTIME_SP_PASSWORD" \
                    --tenant "$RUNTIME_SP_TENANT" \
                    --output none 2>/dev/null; then
                    echo "Runtime (ACA): Azure CLI authenticated via SP fallback."
                    _RUNTIME_AUTH_OK=true
                else
                    echo "Runtime (ACA): SP fallback also failed."
                fi
            fi
        fi
    elif [[ -n "${RUNTIME_SP_APP_ID:-}" && -n "${RUNTIME_SP_PASSWORD:-}" && -n "${RUNTIME_SP_TENANT:-}" ]]; then
        echo "Runtime (Docker): logging in with scoped service principal..."
        if az login --service-principal \
            -u "$RUNTIME_SP_APP_ID" \
            -p "$RUNTIME_SP_PASSWORD" \
            --tenant "$RUNTIME_SP_TENANT" \
            --output none 2>/dev/null; then
            echo "Runtime (Docker): Azure CLI authenticated (scoped SP)."
            _RUNTIME_AUTH_OK=true
        else
            echo "Runtime (Docker): WARNING -- service principal login failed. Bot endpoint sync will be unavailable."
        fi
    else
        echo "Runtime: no identity credentials found. Running without Azure CLI access."
        echo "         Bot endpoint updates will not work until admin provisions a runtime identity."
    fi

    # --- Step 2: resolve @kv:* secrets eagerly ---------------------------
    if [[ "$_RUNTIME_AUTH_OK" == "true" && -n "${KEY_VAULT_URL:-}" ]]; then
        echo "Runtime: resolving @kv: secrets from Key Vault..."
        _KV_OUTPUT=$(timeout 60 python -m polyclaw.keyvault_resolve 2>&1) || {
            _KV_RC=$?
            echo "  ERROR: keyvault_resolve failed (exit code $_KV_RC)"
            if [[ $_KV_RC -eq 124 ]]; then
                echo "  TIMEOUT: KV resolution hung for 60s"
            fi
            echo "$_KV_OUTPUT" | sed 's/^/    /'
        }
        if [[ -n "${_KV_OUTPUT:-}" ]]; then
            while IFS= read -r line; do
                if [[ "$line" == export\ * ]]; then
                    eval "$line"
                else
                    echo "  $line"
                fi
            done <<< "$_KV_OUTPUT"
        fi
        echo "Runtime: Key Vault resolution complete."
    else
        # No identity or no KV URL -- strip @kv:* references to empty
        # strings so the app can start without noisy tracebacks.
        while IFS='=' read -r key value; do
            if [[ "$value" == @kv:* ]]; then
                export "$key="
            fi
        done < <(env)
        unset KEY_VAULT_URL 2>/dev/null || true
    fi

elif [[ -n "${KEY_VAULT_URL:-}" ]]; then
    echo "Resolving secrets from Key Vault..."
    echo "  Vault URL:  ${KEY_VAULT_URL}"

    # Collect @kv: references for debugging
    _KV_REFS=$(env | grep '=@kv:' | cut -d= -f1 | tr '\n' ', ' | sed 's/,$//')
    if [[ -n "$_KV_REFS" ]]; then
        echo "  @kv: refs:  ${_KV_REFS}"
    else
        echo "  @kv: refs:  (none found -- nothing to resolve)"
    fi

    # Gate on Azure CLI auth: if `az account show` fails, there are no
    # credentials available and DefaultAzureCredential will hang probing
    # IMDS (especially on ACA where the admin has no managed identity).
    if az account show --output none 2>/dev/null; then
        echo "  Azure CLI:  authenticated"
        _KV_OUTPUT=$(timeout 60 python -m polyclaw.keyvault_resolve 2>&1) || {
            _KV_RC=$?
            echo "  ERROR: keyvault_resolve failed (exit code $_KV_RC)"
            if [[ $_KV_RC -eq 124 ]]; then
                echo "  TIMEOUT: KV resolution hung for 60s (likely IMDS probe or network issue)"
            fi
            echo "  output:"
            echo "$_KV_OUTPUT" | sed 's/^/    /'
        }
        if [[ -n "${_KV_OUTPUT:-}" ]]; then
            # Only eval lines that start with 'export ' (safe); print the rest
            while IFS= read -r line; do
                if [[ "$line" == export\ * ]]; then
                    eval "$line"
                else
                    echo "  $line"
                fi
            done <<< "$_KV_OUTPUT"
        fi
        echo "  Key Vault resolution complete."
    else
        echo "  Azure CLI:  NOT authenticated -- skipping Key Vault resolution."
        echo "  Run 'az login' in the admin container or complete the Setup Wizard first."
        # Strip @kv:* references to empty strings so the app starts cleanly.
        while IFS='=' read -r key value; do
            if [[ "$value" == @kv:* ]]; then
                export "$key="
            fi
        done < <(env)
    fi
fi

AUTH_DONE="$DATA_DIR/.copilot-auth/.authenticated"

# --- GitHub Authentication ------------------------------------------------

if [[ "$MODE" != "runtime" ]]; then
    # Only admin / combined modes care about GitHub auth
    if [[ -n "${GITHUB_TOKEN:-}" ]] || [[ -n "${GH_TOKEN:-}" ]]; then
        echo "Using token from environment."
    elif [[ -f "$AUTH_DONE" ]]; then
        echo "Already authenticated (cached)."
    else
        echo "GitHub not authenticated -- use the web admin UI to authenticate."
    fi
fi

# --- Launch ---------------------------------------------------------------

if [[ "$MODE" == "run" ]]; then
    shift 2>/dev/null || true  # consume the mode arg if passed via docker CMD
    echo ""
    echo "Starting single-command CLI..."
    exec polyclaw-run "$@"
elif [[ "$MODE" == "cli" ]]; then
    echo ""
    echo "Starting interactive CLI..."
    exec polyclaw
elif [[ "$MODE" == "bot" ]]; then
    export ADMIN_PORT="${ADMIN_PORT:-${BOT_PORT:-8080}}"
    echo ""
    echo "Starting polyclaw (bot mode) on port ${ADMIN_PORT}..."
    exec polyclaw-admin
elif [[ "$MODE" == "admin" ]]; then
    ADMIN_PORT="${ADMIN_PORT:-9090}"
    export POLYCLAW_SERVER_MODE="admin"
    echo ""
    echo "Starting polyclaw ADMIN container on port ${ADMIN_PORT}..."
    echo "  Mode:         admin (control-plane only)"
    echo "  HOME:         $HOME (isolated from runtime)"
    if [[ -n "${ADMIN_SECRET:-}" && ! "${ADMIN_SECRET}" == @kv:* ]]; then
        echo "  Admin UI:     http://localhost:${ADMIN_PORT}/?secret=${ADMIN_SECRET}"
    else
        echo "  Admin UI:     http://localhost:${ADMIN_PORT}  (secret pending)"
    fi
    echo ""
    exec polyclaw-admin --admin-only
elif [[ "$MODE" == "runtime" ]]; then
    ADMIN_PORT="${ADMIN_PORT:-8080}"
    export POLYCLAW_SERVER_MODE="runtime"
    echo ""
    echo "Starting polyclaw RUNTIME container on port ${ADMIN_PORT}..."
    echo "  Mode:         runtime (data-plane only)"
    echo "  HOME:         $HOME (ephemeral, no admin creds)"
    if [[ -n "${POLYCLAW_USE_MI:-}" ]]; then
        echo "  Platform:     ACA (managed identity)"
        echo "  Identity:     managed identity (AZURE_CLIENT_ID=${AZURE_CLIENT_ID:-<not set>})"
    else
        echo "  Platform:     Docker"
        echo "  Identity:     ${RUNTIME_SP_APP_ID:+scoped SP $RUNTIME_SP_APP_ID}${RUNTIME_SP_APP_ID:-none}"
    fi
    echo "  Bot messages: http://localhost:${ADMIN_PORT}/api/messages"
    echo ""
    exec polyclaw-admin --runtime-only
else
    ADMIN_PORT="${ADMIN_PORT:-9090}"

    echo ""
    echo "Starting polyclaw admin on port ${ADMIN_PORT}..."
    # Use the env var (already resolved by keyvault_resolve above)
    # Never print raw @kv: references -- they are not usable as tokens.
    if [[ -n "${ADMIN_SECRET:-}" && ! "${ADMIN_SECRET}" == @kv:* ]]; then
        echo "  Admin UI:      http://localhost:${ADMIN_PORT}/?secret=${ADMIN_SECRET}"
    elif [[ "${ADMIN_SECRET:-}" == @kv:* ]]; then
        echo "  Admin UI:      http://localhost:${ADMIN_PORT}  (secret pending KV resolution)"
    else
        echo "  Admin UI:      http://localhost:${ADMIN_PORT}"
        echo "  (admin secret will be auto-generated on first start)"
    fi
    echo "  Bot messages:  http://localhost:${ADMIN_PORT}/api/messages"
    echo ""
    exec polyclaw-admin
fi
