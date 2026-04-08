---
title: "Key Vault Integration"
weight: 1
---

# Key Vault Integration

> **Warning:** Key Vault integration does not yet cover all secret variables reliably. This is actively being worked on. 

Polyclaw integrates with Azure Key Vault to separate sensitive credentials from the agent's working data. The `.env` file still holds non-secret configuration, but secrets are stored in Key Vault instead. The agent can still read resolved secrets at runtime, so Key Vault does not hide them from the LLM. The value is in keeping secrets out of the workspace filesystem, which reduces the risk of accidentally copying, committing, or leaking them alongside regular configuration and data.

## Configuration

Set the Key Vault reference in your `.env`:

```bash
KEY_VAULT_URL=https://polyclaw-kv.vault.azure.net

# Optional: used for firewall allowlisting only
KEY_VAULT_NAME=polyclaw-kv
KEY_VAULT_RG=my-rg
```

### Store Secrets

```bash
az keyvault secret set \
  --vault-name polyclaw-kv \
  --name admin-secret \
  --value "your-admin-secret"
```

### Reference Secrets

In your `.env` file, use `@kv:` prefixed values:

```bash
BOT_APP_PASSWORD=@kv:bot-app-password
ADMIN_SECRET=@kv:admin-secret
ACS_CONNECTION_STRING=@kv:acs-connection
AZURE_OPENAI_API_KEY=@kv:openai-api-key
```

## How Resolution Works

For each supported secret variable, `@kv:` references are resolved during settings load via Key Vault API calls.

1. When a supported variable (see below) is read, the `@kv:` prefix is detected
2. `SecretClient.get_secret(secret_name)` retrieves the value
3. The resolved value is used in-process; the `.env` file is not modified

The Docker entrypoint (`entrypoint.sh`) additionally runs a shell-level pass that resolves `@kv:` prefixes in all environment variables before the server starts.

## Authentication

Key Vault access uses `DefaultAzureCredential`, which automatically tries managed identity, environment variables, Azure CLI (`az login`), and other methods in sequence.

## Firewall Allowlisting

When `KEY_VAULT_RG` is set, Polyclaw automatically adds the current machine's public IP to the Key Vault firewall rules. This is useful for local development against a locked-down vault.

## write_env() Flow

When saving settings through the admin API:

1. If Key Vault is configured, secrets are stored there
2. The `.env` file is updated with `@kv:secret-name` references
3. On next restart, secrets are resolved from Key Vault

## Supported Variables

The following variables support in-process `@kv:` resolution:

- `BOT_APP_PASSWORD`
- `ADMIN_SECRET`
- `ACS_CONNECTION_STRING`
- `AZURE_OPENAI_API_KEY`

The Docker entrypoint resolves `@kv:` references in all environment variables before startup.
