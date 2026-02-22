"""Preflight security / readiness checks -- /api/setup/preflight."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from aiohttp import web

from ..config.settings import cfg
from ..services.azure import AzureCLI
from ..state.infra_config import InfraConfigStore

logger = logging.getLogger(__name__)


class PreflightRoutes:
    """/api/setup/preflight endpoint and sub-checks."""

    def __init__(
        self,
        tunnel: object | None,
        store: InfraConfigStore,
        az: AzureCLI | None = None,
    ) -> None:
        self._tunnel = tunnel
        self._store = store
        self._az = az

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/setup/preflight", self._preflight)

    async def _preflight(self, req: web.Request) -> web.Response:
        checks: list[dict[str, Any]] = []

        has_app_id = bool(cfg.bot_app_id)
        has_app_pw = bool(cfg.bot_app_password)
        creds_ok = has_app_id and has_app_pw
        missing = [
            name for name, present in [
                ("BOT_APP_ID", has_app_id), ("BOT_APP_PASSWORD", has_app_pw)
            ] if not present
        ]
        checks.append({
            "check": "bot_credentials", "ok": creds_ok,
            "detail": "APP_ID and APP_PASSWORD set" if creds_ok
            else f"Missing: {', '.join(missing)}",
        })

        jwt_ok = False
        jwt_detail = "skipped (no credentials)"
        if creds_ok:
            jwt_ok, jwt_detail = await self._check_jwt_validation()
        checks.append({"check": "jwt_validation", "ok": jwt_ok, "detail": jwt_detail})

        from .tunnel_status import resolve_tunnel_info

        tunnel_info = await resolve_tunnel_info(self._tunnel, self._az)
        tunnel_ok = tunnel_info["active"]
        checks.append({
            "check": "tunnel", "ok": tunnel_ok,
            "detail": tunnel_info["url"] if tunnel_ok
            else "Tunnel not running",
        })

        has_tenant = bool(cfg.bot_app_tenant_id)
        checks.append({
            "check": "tenant_id", "ok": has_tenant,
            "detail": "Configured" if has_tenant else "BOT_APP_TENANT_ID not set",
        })

        admin_ok, admin_detail, admin_results = await self._check_endpoint_auth()
        checks.append({
            "check": "endpoint_auth", "ok": admin_ok,
            "detail": admin_detail, "endpoints": admin_results,
        })

        tg_ok, tg_detail, tg_sub = await self._check_telegram_security()
        checks.append({
            "check": "telegram_security", "ok": tg_ok,
            "detail": tg_detail, "sub_checks": tg_sub,
        })

        acs_ok, acs_detail, acs_sub = await self._check_acs_config()
        checks.append({
            "check": "acs_voice", "ok": acs_ok,
            "detail": acs_detail, "sub_checks": acs_sub,
        })

        voice_active = req.app.get("voice_configured", False)
        sec_ok, sec_detail, sec_sub = await self._check_acs_callback_security(
            voice_routes_active=voice_active
        )
        checks.append({
            "check": "acs_callback_security", "ok": sec_ok,
            "detail": sec_detail, "sub_checks": sec_sub,
        })

        all_ok = all(c["ok"] for c in checks)
        return web.json_response({
            "status": "ok" if all_ok else "warnings", "checks": checks
        })

    async def _check_acs_config(
        self,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        sub: list[dict[str, Any]] = []

        conn = cfg.acs_connection_string
        if conn:
            parts = {
                k.strip().lower(): v.strip()
                for k, _, v in (seg.partition("=") for seg in conn.split(";") if "=" in seg)
            }
            has_ep = bool(parts.get("endpoint"))
            has_key = bool(parts.get("accesskey"))
            if has_ep and has_key:
                sub.append({"name": "acs_connection_string", "ok": True, "detail": "Well-formed"})
            else:
                missing = [x for x, v in [("endpoint", has_ep), ("accesskey", has_key)] if not v]
                sub.append({
                    "name": "acs_connection_string", "ok": False,
                    "detail": f"Malformed -- missing: {', '.join(missing)}",
                })
        else:
            sub.append({
                "name": "acs_connection_string", "ok": False,
                "detail": "ACS_CONNECTION_STRING not set",
            })

        src = cfg.acs_source_number
        sub.append({
            "name": "acs_source_number",
            "ok": bool(src),
            "detail": f"Source: {src}" if src else "ACS_SOURCE_NUMBER not set",
        })

        tgt = cfg.voice_target_number
        sub.append({
            "name": "voice_target_number",
            "ok": bool(tgt),
            "detail": f"Target: {tgt}" if tgt else "VOICE_TARGET_NUMBER not set",
        })

        aoai = cfg.azure_openai_endpoint
        if aoai:
            try:
                async with aiohttp.ClientSession() as session:
                    url = aoai.rstrip("/") + "/openai/models?api-version=2024-10-21"
                    headers = (
                        {"api-key": cfg.azure_openai_api_key}
                        if cfg.azure_openai_api_key else {}
                    )
                    async with session.get(
                        url, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        ok = resp.status in (200, 401, 403)
                        sub.append({
                            "name": "azure_openai_endpoint", "ok": ok,
                            "detail": f"Reachable ({resp.status})" if ok
                            else f"Returned {resp.status}",
                        })
            except Exception as exc:
                sub.append({
                    "name": "azure_openai_endpoint", "ok": False,
                    "detail": f"Unreachable: {exc}",
                })
        else:
            sub.append({
                "name": "azure_openai_endpoint", "ok": False,
                "detail": "AZURE_OPENAI_ENDPOINT not set",
            })

        dep = cfg.azure_openai_realtime_deployment
        sub.append({
            "name": "realtime_deployment",
            "ok": bool(dep),
            "detail": f"Deployment: {dep}" if dep else "Not set",
        })

        all_ok = all(s["ok"] for s in sub)
        issues = [s["name"] for s in sub if not s["ok"]]
        detail = "ACS voice OK" if all_ok else f"Issues: {', '.join(issues)}"
        return all_ok, detail, sub

    async def _check_acs_callback_security(
        self, *, voice_routes_active: bool = False,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        sub: list[dict[str, Any]] = []
        voice_configured = bool(cfg.acs_connection_string and cfg.acs_source_number)

        token = cfg.acs_callback_token
        sub.append({
            "name": "callback_token",
            "ok": bool(token and len(token) >= 16),
            "detail": f"Token active ({len(token)} chars)" if token and len(token) >= 16
            else "Missing or too short",
        })

        if voice_configured and not voice_routes_active:
            sub.append({
                "name": "routes_not_registered", "ok": False,
                "detail": "Voice configured but routes not registered; restart needed.",
            })
        elif voice_routes_active:
            base = f"http://127.0.0.1:{cfg.admin_port}"
            timeout = aiohttp.ClientTimeout(total=5)
            probes = [
                ("POST", "/acs", "no_token"),
                ("POST", "/acs?token=wrong_token_value", "wrong_token"),
                ("GET", "/realtime-acs", "no_token_ws"),
            ]
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for method, path, label in probes:
                    try:
                        async with session.request(
                            method, base + path,
                            json=[{}] if method == "POST" else None,
                        ) as resp:
                            ok = resp.status == 401
                            sub.append({
                                "name": label, "ok": ok,
                                "detail": (
                                    f"{method} {path.split('?')[0]} rejected ({resp.status})"
                                    if ok else
                                    f"RISK: {method} {path.split('?')[0]} returned {resp.status}"
                                ),
                            })
                    except Exception as exc:
                        sub.append({
                            "name": label, "ok": True,
                            "detail": f"Rejected with error: {str(exc)[:80]}",
                        })
        elif not voice_configured:
            sub.append({
                "name": "endpoint_probes", "ok": True,
                "detail": "Voice not configured (no exposure)",
            })

        try:
            import jwt as pyjwt  # noqa: F401
            sub.append({
                "name": "pyjwt_installed", "ok": True,
                "detail": f"PyJWT {pyjwt.__version__}",
            })
        except ImportError:
            sub.append({
                "name": "pyjwt_installed", "ok": False,
                "detail": "PyJWT not installed",
            })

        if voice_configured or voice_routes_active:
            from .tunnel_status import resolve_tunnel_info

            t_info = await resolve_tunnel_info(self._tunnel, self._az)
            tunnel_active = t_info["active"]
            tunnel_url = (t_info["url"] or "").rstrip("/") if t_info["url"] else ""
            if tunnel_active and tunnel_url:
                sub.append({
                    "name": "tunnel", "ok": True,
                    "detail": f"Active: {tunnel_url}",
                })
            else:
                sub.append({
                    "name": "tunnel", "ok": False,
                    "detail": "No tunnel / not running",
                })

            res_id = cfg.acs_resource_id
            sub.append({
                "name": "acs_resource_id",
                "ok": bool(res_id),
                "detail": f"Resource ID: {res_id}" if res_id else "Cannot derive",
            })

        all_ok = all(s["ok"] for s in sub)
        detail = "ACS callback security OK" if all_ok else (
            f"Issues: {', '.join(s['name'] for s in sub if not s['ok'])}"
        )
        return all_ok, detail, sub

    async def _check_jwt_validation(self) -> tuple[bool, str]:
        url = f"http://127.0.0.1:{cfg.admin_port}/api/messages"
        payload = {
            "type": "message", "text": "__preflight_jwt_check__",
            "channelId": "preflight",
            "from": {"id": "preflight-probe", "name": "preflight"},
            "conversation": {"id": "preflight"},
            "recipient": {"id": "bot"},
            "serviceUrl": "https://preflight.invalid",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (401, 403):
                        return True, f"Rejected ({resp.status})"
                    if resp.status == 503:
                        return True, "Credentials guard active (503)"
                    body = await resp.text()
                    return False, (
                        f"RISK: returned {resp.status} without auth. "
                        f"Response: {body[:200]}"
                    )
        except Exception as exc:
            return False, f"Cannot reach /api/messages: {exc}"

    async def _check_endpoint_auth(
        self,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        base = f"http://127.0.0.1:{cfg.admin_port}"
        endpoints = [
            ("GET", "/api/setup/status"),
            ("POST", "/api/setup/azure/login"),
            ("GET", "/api/setup/azure/check"),
            ("GET", "/api/setup/azure/subscriptions"),
            ("GET", "/api/setup/copilot/status"),
            ("POST", "/api/setup/tunnel/start"),
            ("GET", "/api/setup/bot/config"),
            ("GET", "/api/setup/channels/config"),
            ("GET", "/api/setup/prerequisites/status"),
            ("GET", "/api/setup/infra/status"),
            ("GET", "/api/setup/config"),
            ("GET", "/api/setup/preflight"),
            ("GET", "/api/schedules"),
            ("GET", "/api/workspace/list"),
            ("GET", "/api/workspace/read"),
            ("GET", "/api/media/test.png"),
            ("GET", "/api/chat/ws"),
        ]

        results: list[dict[str, Any]] = []
        all_ok = True
        timeout = aiohttp.ClientTimeout(total=5)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for method, path in endpoints:
                try:
                    async with session.request(method, base + path) as resp:
                        ok = resp.status == 401
                        if not ok:
                            all_ok = False
                        results.append({
                            "method": method, "path": path,
                            "status": resp.status, "ok": ok,
                        })
                except Exception as exc:
                    results.append({
                        "method": method, "path": path,
                        "status": "error", "ok": True,
                        "detail": str(exc)[:120],
                    })

        exposed = [r for r in results if not r["ok"]]
        if all_ok:
            detail = f"All {len(results)} endpoints returned 401"
        else:
            names = ", ".join(
                f"{r['method']} {r['path']} ({r['status']})" for r in exposed
            )
            detail = f"EXPOSED: {names}"
        return all_ok, detail, results

    async def _check_telegram_security(
        self,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        sub: list[dict[str, Any]] = []
        tg_token = self._store.channels.telegram.token

        if not tg_token:
            sub.append({
                "name": "configured", "ok": True,
                "detail": "Not configured (no exposure)",
            })
            return True, "Telegram not configured", sub

        sub.append({"name": "configured", "ok": True, "detail": "Configured"})

        wl_raw = self._store.channels.telegram.whitelist or ""
        wl_ids = [s.strip() for s in wl_raw.split(",") if s.strip()]
        runtime_wl = cfg.telegram_whitelist
        has_wl = bool(wl_ids) or bool(runtime_wl)
        ids = wl_ids or list(runtime_wl)
        sub.append({
            "name": "whitelist",
            "ok": has_wl,
            "detail": (
                f"Active ({len(ids)} user(s))" if has_wl
                else "No whitelist -- ANY user can interact"
            ),
        })

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.telegram.org/bot{tg_token}/getMe",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        info = data.get("result", {})
                        username = info.get("username", "?")
                        sub.append({
                            "name": "token_valid", "ok": True,
                            "detail": f"Valid -- @{username}",
                        })

                        can_join = info.get("can_join_groups", False)
                        sub.append({
                            "name": "groups",
                            "ok": not can_join,
                            "detail": "Cannot join groups" if not can_join
                            else "Can join groups (risk)",
                        })

                        can_read = info.get("can_read_all_group_messages", False)
                        sub.append({
                            "name": "group_privacy",
                            "ok": not can_read,
                            "detail": "Privacy mode on" if not can_read
                            else "Reads ALL group messages",
                        })

                        inline = info.get("supports_inline_queries", False)
                        sub.append({
                            "name": "inline",
                            "ok": not inline,
                            "detail": "Inline disabled" if not inline
                            else "Inline enabled (risk)",
                        })
                    else:
                        sub.append({
                            "name": "token_valid", "ok": False,
                            "detail": f"Rejected: {data.get('description', '?')}",
                        })
        except Exception as exc:
            sub.append({
                "name": "token_valid", "ok": False,
                "detail": f"Cannot reach API: {exc}",
            })

        all_ok = all(s["ok"] for s in sub)
        issues = [s["name"] for s in sub if not s["ok"]]
        detail = "Telegram security OK" if all_ok else f"Issues: {', '.join(issues)}"
        return all_ok, detail, sub
