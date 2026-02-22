---
title: "Security & Auth"
weight: 2
---

# Security & Authentication

Polyclaw implements multiple layers of security across its endpoints.

## Admin API Authentication

All `/api/*` routes require a Bearer token matching `ADMIN_SECRET`:

```
Authorization: Bearer <ADMIN_SECRET>
```

The frontend stores this token in `localStorage` after login and includes it in all API requests.

### Public Endpoints

These paths are exempt from Bearer auth but are secured through other mechanisms:

| Path | Auth Mechanism |
|---|---|
| `/health` | None (health check for load balancers) |
| `/api/messages` | Bot Framework SDK validates app ID, password, and Microsoft channel JWT |
| `/api/voice/acs-callback`, `/acs` | Query-param callback token + ACS RS256 JWT verified against Microsoft JWKS |
| `/api/voice/media-streaming`, `/realtime-acs` | Query-param callback token + ACS RS256 JWT verified against Microsoft JWKS |
| `/api/auth/check` | Intentionally open -- accepts a token attempt and returns `{"authenticated": true/false}` without exposing secrets |

### Protected Voice Endpoints

`/api/voice/call` and `/api/voice/status` are **not** public. They require the standard Bearer token like all other admin API routes.

## Lockdown Mode (Experimental)

Setting `LOCKDOWN_MODE=true` rejects **all** API requests with HTTP 403.

The intended workflow is:

1. **Activate** from the web UI -- the agent stops accepting API requests immediately, locking out the admin dashboard.
2. **Deactivate** via a bot service channel -- send `/lockdown off` through Teams or Telegram to restore access. Bot messaging endpoints remain open during lockdown specifically for this purpose.

This feature is not yet fully implemented. Currently it can be toggled via slash commands in a messaging channel:

```bash
/lockdown on   # Enable
/lockdown off  # Disable
```

A proper web UI toggle for activation is planned but not yet available.

## Tunnel Restriction

Setting `TUNNEL_RESTRICTED=true` restricts Cloudflare tunnel access to only bot and voice endpoints. This prevents public access to the admin dashboard while keeping Azure Bot Service callbacks functional.

Requests are identified as tunnel traffic by checking the request headers for Cloudflare-specific markers.

## Telegram Whitelist

For Telegram channels, `TELEGRAM_WHITELIST` restricts which user IDs can interact with the bot:

```bash
TELEGRAM_WHITELIST=123456789,987654321
```

Messages from non-whitelisted users are silently dropped.

## Bot Framework Validation

Bot Framework requests are validated by the `botbuilder-core` SDK using:

- App ID and password verification
- Channel authentication (Microsoft token validation)
- Activity schema validation

## ACS JWT Validation

Azure Communication Services callback requests include a JWT token that is validated against the ACS endpoint to ensure authenticity.

## Frontend Auth Flow

1. User sees the **Disclaimer** screen on first visit
2. After accepting, the **Login** screen appears
3. User enters `ADMIN_SECRET`
4. Frontend calls `POST /api/auth/check` with the token
5. On success, token is stored in `localStorage`
6. All subsequent API calls include the Bearer header
7. If identity is not configured, the user is redirected to the Setup Wizard
