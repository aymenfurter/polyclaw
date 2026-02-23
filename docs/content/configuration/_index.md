---
title: "Configuration"
weight: 30
---

Polyclaw is configured through environment variables loaded from a `.env` file or the system environment. The configuration singleton is defined in `app/runtime/config/settings.py`.

## Core Settings

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | -- | GitHub PAT with Copilot access. Supports `@kv:` prefix. |
| `COPILOT_MODEL` | `claude-sonnet-4.6` | Default LLM model for conversations |
| `COPILOT_AGENT` | -- | Optional Copilot agent name |
| `ADMIN_PORT` | `9090` | Admin server listen port |
| `ADMIN_SECRET` | -- | Bearer token for API authentication. Supports `@kv:` prefix. |
| `POLYCLAW_DATA_DIR` | `~/.polyclaw` | Root directory for all persistent data |
| `DOTENV_PATH` | -- | Custom path to `.env` file |
| `POLYCLAW_SERVER_MODE` | `combined` | Server mode: `combined`, `admin`, or `runtime` |

## Bot Framework

| Variable | Default | Description |
|---|---|---|
| `BOT_APP_ID` | -- | Azure Bot registration app ID |
| `BOT_APP_PASSWORD` | -- | Azure Bot app secret. Supports `@kv:` prefix. |
| `BOT_APP_TENANT_ID` | -- | Azure AD tenant ID |
| `BOT_PORT` | `3978` | Bot Framework endpoint port |

## Voice / Azure Communication Services

| Variable | Default | Description |
|---|---|---|
| `ACS_CONNECTION_STRING` | -- | Azure Communication Services connection string. Supports `@kv:` prefix. |
| `ACS_SOURCE_NUMBER` | -- | ACS phone number for outbound calls |
| `ACS_CALLBACK_TOKEN` | Auto-generated | Token securing the ACS callback webhook. Auto-generated if not set. |
| `VOICE_TARGET_NUMBER` | -- | Default target phone number |
| `AZURE_OPENAI_ENDPOINT` | -- | Azure OpenAI endpoint for realtime model |
| `AZURE_OPENAI_API_KEY` | -- | Azure OpenAI API key. Supports `@kv:` prefix. |
| `AZURE_OPENAI_REALTIME_DEPLOYMENT` | `gpt-realtime-mini` | Realtime model deployment name |

## Memory

| Variable | Default | Description |
|---|---|---|
| `MEMORY_MODEL` | `claude-sonnet-4.6` | Model used for memory consolidation |
| `MEMORY_IDLE_MINUTES` | `5` | Minutes of inactivity before memory formation triggers |

## Proactive Messaging

| Variable | Default | Description |
|---|---|---|
| `PROACTIVE_ENABLED` | `false` | Enable autonomous proactive messaging |

## Security

| Variable | Default | Description |
|---|---|---|
| `LOCKDOWN_MODE` | -- | (Experimental) Reject all admin API requests. Any non-empty value enables this mode. Web UI toggle and terminal recovery are not yet fully implemented. |
| `TUNNEL_RESTRICTED` | -- | Restrict tunnel to bot/voice endpoints only. Any non-empty value enables this mode. |
| `TELEGRAM_WHITELIST` | -- | Comma-separated allowed Telegram user IDs |

## Azure Key Vault

| Variable | Default | Description |
|---|---|---|
| `KEY_VAULT_URL` | -- | Full Key Vault URL (`https://<name>.vault.azure.net`) |
| `KEY_VAULT_NAME` | -- | Key Vault name, used for firewall allowlisting CLI commands |
| `KEY_VAULT_RG` | -- | Key Vault resource group |

## Derived Paths

All paths are computed relative to `POLYCLAW_DATA_DIR`:

| Path | Description |
|---|---|
| `media_dir` | `<data>/media/` -- `incoming/`, `outgoing/pending/`, `outgoing/sent/`, `outgoing/error/` |
| `memory_dir` | `<data>/memory/` -- daily logs, topic notes |
| `skills_dir` | `<data>/skills/` -- user and plugin skill directories |
| `sessions_dir` | `<data>/sessions/` -- archived chat sessions |
| `soul_path` | `<data>/SOUL.md` -- agent personality |
| `scheduler_db_path` | `<data>/scheduler.json` -- scheduled tasks |
| `conversation_refs_path` | `<data>/conversation_refs.json` -- stored conversation references |

## Secret Resolution

The following environment variables support `@kv:` prefix resolution from Azure Key Vault: `GITHUB_TOKEN`, `ADMIN_SECRET`, `BOT_APP_PASSWORD`, `ACS_CONNECTION_STRING`, `AZURE_OPENAI_API_KEY`. The Docker entrypoint additionally resolves all `@kv:` prefixed variables via a shell-level pass.

For example:

```bash
GITHUB_TOKEN=@kv:polyclaw-github-token
ADMIN_SECRET=@kv:polyclaw-admin-secret
```

This requires `KEY_VAULT_URL` to be set and valid Azure credentials (via `az login` or managed identity).

See [Key Vault Integration](/configuration/keyvault/) for details.
