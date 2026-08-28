from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web


SESSION_COOKIE = "private_companion_session"
STATIC_ROOT = Path(__file__).resolve().parent / "webui"
CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"


class CompanionWebUI:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.sessions: dict[str, float] = {}
        self.auth_lock = asyncio.Lock()
        self.started_at = time.time()
        self.is_running = False

    async def start(self) -> bool:
        token = str(self.plugin.config.webui.access_token or "").strip()
        if len(token) < 12:
            self.plugin.ctx.logger.error(
                "[private-companion] 独立面板未启动：access_token 至少需要 12 个字符"
            )
            return False
        app = self._create_app()
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        host = str(self.plugin.config.webui.host or "0.0.0.0").strip()
        port = int(self.plugin.config.webui.port)
        try:
            self.site = web.TCPSite(self.runner, host=host, port=port)
            await self.site.start()
        except Exception:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
            raise
        self.is_running = True
        self.plugin.ctx.logger.info(
            "[private-companion] 独立面板已启动：http://%s:%s", host, port
        )
        return True

    async def stop(self) -> None:
        self.is_running = False
        if self.runner is not None:
            await self.runner.cleanup()
        self.runner = None
        self.site = None
        async with self.auth_lock:
            self.sessions.clear()

    def _create_app(self) -> web.Application:
        app = web.Application(
            middlewares=[self._security_headers, self._error_handler, self._auth_guard]
        )
        app.router.add_get("/", self._index)
        app.router.add_get("/assets/{name}", self._asset)
        app.router.add_post("/api/auth/login", self._login)
        app.router.add_get("/api/auth/status", self._auth_status)
        app.router.add_post("/api/auth/logout", self._logout)
        app.router.add_get("/api/overview", self._overview)
        app.router.add_get("/api/users", self._users)
        app.router.add_get("/api/users/{user_id}", self._user_detail)
        app.router.add_put("/api/users/{user_id}", self._update_user)
        app.router.add_delete("/api/users/{user_id}", self._delete_user)
        app.router.add_post("/api/users/{user_id}/notes", self._add_note)
        app.router.add_delete("/api/users/{user_id}/notes/{index}", self._delete_note)
        app.router.add_post("/api/users/{user_id}/summary", self._generate_summary)
        app.router.add_post("/api/users/{user_id}/proactive-test", self._proactive_test)
        app.router.add_get("/api/settings", self._get_settings)
        app.router.add_put("/api/settings", self._save_settings)
        app.router.add_get("/api/diagnostics", self._diagnostics)
        return app

    @web.middleware
    async def _security_headers(self, request: web.Request, handler: Any) -> web.StreamResponse:
        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @web.middleware
    async def _error_handler(self, request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as exc:
            self.plugin.ctx.logger.exception("[private-companion] WebUI 请求失败：%s", exc)
            return self._json(False, error="请求处理失败", status=500)

    @web.middleware
    async def _auth_guard(self, request: web.Request, handler: Any) -> web.StreamResponse:
        public = {"/", "/api/auth/login", "/api/auth/status"}
        if request.path.startswith("/assets/") or request.path in public:
            return await handler(request)
        if not await self._authenticated(request):
            return self._json(False, error="未登录", status=401)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not self._same_origin(request):
            return self._json(False, error="请求来源无效", status=403)
        return await handler(request)

    @staticmethod
    def _same_origin(request: web.Request) -> bool:
        origin = request.headers.get("Origin", "").rstrip("/")
        expected = f"{request.scheme}://{request.host}".rstrip("/")
        return bool(origin) and hmac.compare_digest(origin, expected)

    async def _authenticated(self, request: web.Request) -> bool:
        raw = request.cookies.get(SESSION_COOKIE, "")
        if not raw:
            return False
        digest = hashlib.sha256(raw.encode()).hexdigest()
        now = time.time()
        async with self.auth_lock:
            expires = self.sessions.get(digest, 0)
            if expires <= now:
                self.sessions.pop(digest, None)
                return False
        return True

    @staticmethod
    def _json(success: bool, data: Any = None, error: str = "", status: int = 200) -> web.Response:
        payload: dict[str, Any] = {"success": success}
        if data is not None:
            payload["data"] = data
        if error:
            payload["error"] = error
        return web.json_response(payload, status=status, dumps=lambda value: json.dumps(value, ensure_ascii=False))

    async def _index(self, request: web.Request) -> web.Response:
        del request
        return web.FileResponse(STATIC_ROOT / "index.html")

    async def _asset(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in {"app.css", "app.js", "logo.png"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC_ROOT / name)

    async def _login(self, request: web.Request) -> web.Response:
        if not self._same_origin(request):
            return self._json(False, error="请求来源无效", status=403)
        body = await self._body(request)
        supplied = str(body.get("token") or "")
        expected = str(self.plugin.config.webui.access_token or "")
        if not expected or not hmac.compare_digest(supplied, expected):
            await asyncio.sleep(0.25)
            return self._json(False, error="访问令牌不正确", status=401)
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        ttl = int(self.plugin.config.webui.session_ttl_hours) * 3600
        async with self.auth_lock:
            self.sessions[digest] = time.time() + ttl
        response = self._json(True, {"authenticated": True})
        response.set_cookie(
            SESSION_COOKIE, raw, max_age=ttl, httponly=True, samesite="Strict", path="/"
        )
        return response

    async def _auth_status(self, request: web.Request) -> web.Response:
        return self._json(True, {"authenticated": await self._authenticated(request)})

    async def _logout(self, request: web.Request) -> web.Response:
        raw = request.cookies.get(SESSION_COOKIE, "")
        if raw:
            async with self.auth_lock:
                self.sessions.pop(hashlib.sha256(raw.encode()).hexdigest(), None)
        response = self._json(True, {"authenticated": False})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return {}
        return body if isinstance(body, dict) else {}

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.plugin.config.proactive.timezone))

    def _public_user(self, user_id: str, user: dict[str, Any], include_recent: bool = False) -> dict[str, Any]:
        result = {
            "user_id": user_id,
            "is_target": self.plugin._is_target(user_id),
            "nickname": str(user.get("nickname") or ""),
            "mood": str(user.get("mood") or ""),
            "summary": str(user.get("summary") or ""),
            "notes": list(user.get("notes") or []),
            "message_count": int(user.get("message_count", 0)),
            "recent_count": len(user.get("recent") or []),
            "last_interaction": str(user.get("last_interaction") or ""),
            "last_proactive": str(user.get("last_proactive") or ""),
            "proactive_count": int(user.get("proactive_count", 0)),
        }
        if include_recent:
            result["recent"] = list(user.get("recent") or [])[-40:]
        return result

    async def _overview(self, request: web.Request) -> web.Response:
        del request
        users = self.plugin._data.get("users", {})
        now = self._now()
        payload = {
            "now": now.isoformat(),
            "timezone": self.plugin.config.proactive.timezone,
            "relationship": self.plugin.config.persona.relationship,
            "plugin_enabled": self.plugin.config.plugin.enabled,
            "proactive_enabled": self.plugin.config.proactive.enabled,
            "quiet_now": self.plugin._quiet(now),
            "target_count": len(self.plugin._target_users()),
            "target_mode": self.plugin._target_mode(),
            "memory_users": len(users),
            "message_count": sum(int(u.get("message_count", 0)) for u in users.values()),
            "note_count": sum(len(u.get("notes") or []) for u in users.values()),
            "users": [self._public_user(uid, user) for uid, user in sorted(users.items())],
        }
        return self._json(True, payload)

    async def _users(self, request: web.Request) -> web.Response:
        del request
        users = self.plugin._data.get("users", {})
        return self._json(True, [self._public_user(uid, user) for uid, user in sorted(users.items())])

    async def _user_detail(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"].strip()
        user = self.plugin._data.get("users", {}).get(user_id)
        if not isinstance(user, dict):
            return self._json(False, error="用户不存在", status=404)
        return self._json(True, self._public_user(user_id, user, include_recent=True))

    async def _update_user(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"].strip()
        if not user_id or len(user_id) > 80:
            return self._json(False, error="用户 ID 无效", status=400)
        body = await self._body(request)
        user = self.plugin._user(user_id)
        for key, limit in (("nickname", 40), ("mood", 160), ("summary", 1200)):
            if key in body:
                user[key] = str(body[key] or "").strip()[:limit]
        await self.plugin._save_data()
        return self._json(True, self._public_user(user_id, user, include_recent=True))

    async def _delete_user(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"].strip()
        body = await self._body(request)
        if body.get("confirm") != user_id:
            return self._json(False, error="确认值不匹配", status=400)
        removed = self.plugin._data.get("users", {}).pop(user_id, None)
        if removed is None:
            return self._json(False, error="用户不存在", status=404)
        await self.plugin._save_data()
        return self._json(True, {"deleted": user_id})

    async def _add_note(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"].strip()
        body = await self._body(request)
        text = str(body.get("text") or "").strip()[:400]
        if not text:
            return self._json(False, error="备注不能为空", status=400)
        user = self.plugin._user(user_id)
        notes = user.setdefault("notes", [])
        notes.append(text)
        del notes[: max(0, len(notes) - int(self.plugin.config.memory.max_notes))]
        await self.plugin._save_data()
        return self._json(True, {"notes": notes})

    async def _delete_note(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"].strip()
        try:
            index = int(request.match_info["index"])
            notes = self.plugin._user(user_id).setdefault("notes", [])
            removed = notes.pop(index)
        except (ValueError, IndexError):
            return self._json(False, error="备注序号无效", status=400)
        await self.plugin._save_data()
        return self._json(True, {"removed": removed, "notes": notes})

    async def _generate_summary(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"].strip()
        if user_id not in self.plugin._data.get("users", {}):
            return self._json(False, error="用户不存在", status=404)
        self.plugin._schedule_summary(user_id)
        return self._json(True, {"scheduled": True})

    async def _proactive_test(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"].strip()
        if not self.plugin._is_target(user_id):
            return self._json(False, error="该用户不在陪伴目标列表中", status=400)
        result = await self.plugin._proactive_tick(force=True, target_user=user_id)
        return self._json(True, result)

    def _settings_payload(self) -> dict[str, Any]:
        c = self.plugin.config
        return {
            "plugin": {
                "enabled": c.plugin.enabled,
                "admin_qqs": list(c.plugin.admin_qqs),
                "target_qqs": list(c.plugin.target_qqs),
                "target_mode": c.plugin.target_mode,
            },
            "persona": {
                "user_nickname": c.persona.user_nickname,
                "relationship": c.persona.relationship,
                "shared_background": c.persona.shared_background,
                "companion_style": c.persona.companion_style,
                "inject_in_groups": c.persona.inject_in_groups,
            },
            "proactive": {
                "enabled": c.proactive.enabled,
                "check_interval_minutes": c.proactive.check_interval_minutes,
                "min_silence_hours": c.proactive.min_silence_hours,
                "min_gap_hours": c.proactive.min_gap_hours,
                "max_per_day": c.proactive.max_per_day,
                "chance_percent": c.proactive.chance_percent,
                "quiet_start_hour": c.proactive.quiet_start_hour,
                "quiet_end_hour": c.proactive.quiet_end_hour,
                "timezone": c.proactive.timezone,
            },
            "memory": {
                "enabled": c.memory.enabled,
                "recent_message_count": c.memory.recent_message_count,
                "max_notes": c.memory.max_notes,
                "auto_summary_every": c.memory.auto_summary_every,
                "llm_model_task": c.memory.llm_model_task,
            },
            "webui": {
                "enabled": c.webui.enabled,
                "host": c.webui.host,
                "port": c.webui.port,
                "session_ttl_hours": c.webui.session_ttl_hours,
                "access_token_configured": bool(str(c.webui.access_token or "").strip()),
            },
        }

    async def _get_settings(self, request: web.Request) -> web.Response:
        del request
        return self._json(True, self._settings_payload())

    async def _save_settings(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        try:
            self._apply_settings(body)
        except (TypeError, ValueError) as exc:
            return self._json(False, error=str(exc), status=400)
        await self._write_config()
        await self.plugin._restart_proactive_runtime()
        for user_id in self.plugin._target_users():
            self.plugin._user(user_id)
        await self.plugin._save_data()
        return self._json(True, self._settings_payload())

    def _apply_settings(self, body: dict[str, Any]) -> None:
        c = self.plugin.config
        plugin = body.get("plugin", {})
        persona = body.get("persona", {})
        proactive = body.get("proactive", {})
        memory = body.get("memory", {})
        if isinstance(plugin, dict):
            if "enabled" in plugin:
                c.plugin.enabled = bool(plugin["enabled"])
            if "target_mode" in plugin:
                mode = str(plugin["target_mode"] or "").strip().lower()
                if mode not in {"whitelist", "blacklist"}:
                    raise ValueError("target_mode 必须是 whitelist 或 blacklist")
                c.plugin.target_mode = mode
            for key in ("admin_qqs", "target_qqs"):
                if key in plugin:
                    values = plugin[key]
                    if not isinstance(values, list):
                        raise TypeError(f"{key} 必须是数组")
                    setattr(c.plugin, key, [str(x).strip() for x in values if str(x).strip()][:50])
        if isinstance(persona, dict):
            limits = {"user_nickname": 40, "relationship": 160, "shared_background": 2000, "companion_style": 1000}
            for key, limit in limits.items():
                if key in persona:
                    setattr(c.persona, key, str(persona[key] or "").strip()[:limit])
            if "inject_in_groups" in persona:
                c.persona.inject_in_groups = bool(persona["inject_in_groups"])
        if isinstance(proactive, dict):
            if "enabled" in proactive:
                c.proactive.enabled = bool(proactive["enabled"])
            bounds = {
                "check_interval_minutes": (5, 1440, int), "min_silence_hours": (0.5, 168, float),
                "min_gap_hours": (1, 168, float), "max_per_day": (0, 12, int),
                "chance_percent": (0, 100, int), "quiet_start_hour": (0, 23, int),
                "quiet_end_hour": (0, 23, int),
            }
            for key, (low, high, caster) in bounds.items():
                if key in proactive:
                    value = caster(proactive[key])
                    if value < low or value > high:
                        raise ValueError(f"{key} 超出范围 {low} - {high}")
                    setattr(c.proactive, key, value)
            if "timezone" in proactive:
                zone = str(proactive["timezone"] or "").strip()
                ZoneInfo(zone)
                c.proactive.timezone = zone
        if isinstance(memory, dict):
            if "enabled" in memory:
                c.memory.enabled = bool(memory["enabled"])
            bounds = {"recent_message_count": (0, 40), "max_notes": (5, 200), "auto_summary_every": (0, 100)}
            for key, (low, high) in bounds.items():
                if key in memory:
                    value = int(memory[key])
                    if value < low or value > high:
                        raise ValueError(f"{key} 超出范围 {low} - {high}")
                    setattr(c.memory, key, value)
            if "llm_model_task" in memory:
                c.memory.llm_model_task = str(memory["llm_model_task"] or "planner").strip()[:80]

    @staticmethod
    def _q(value: str) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    @staticmethod
    def _array(values: list[str]) -> str:
        return "[" + ", ".join(json.dumps(str(v), ensure_ascii=False) for v in values) + "]"

    async def _write_config(self) -> None:
        c = self.plugin.config
        b = lambda value: "true" if value else "false"
        text = f"""[plugin]
enabled = {b(c.plugin.enabled)}
config_version = {self._q(c.plugin.config_version)}
admin_qqs = {self._array(list(c.plugin.admin_qqs))}
target_qqs = {self._array(list(c.plugin.target_qqs))}
target_mode = {self._q(c.plugin.target_mode)}

[persona]
user_nickname = {self._q(c.persona.user_nickname)}
relationship = {self._q(c.persona.relationship)}
shared_background = {self._q(c.persona.shared_background)}
companion_style = {self._q(c.persona.companion_style)}
inject_in_groups = {b(c.persona.inject_in_groups)}

[proactive]
enabled = {b(c.proactive.enabled)}
check_interval_minutes = {c.proactive.check_interval_minutes}
min_silence_hours = {c.proactive.min_silence_hours}
min_gap_hours = {c.proactive.min_gap_hours}
max_per_day = {c.proactive.max_per_day}
chance_percent = {c.proactive.chance_percent}
quiet_start_hour = {c.proactive.quiet_start_hour}
quiet_end_hour = {c.proactive.quiet_end_hour}
timezone = {self._q(c.proactive.timezone)}

[memory]
enabled = {b(c.memory.enabled)}
recent_message_count = {c.memory.recent_message_count}
max_notes = {c.memory.max_notes}
auto_summary_every = {c.memory.auto_summary_every}
llm_model_task = {self._q(c.memory.llm_model_task)}

[webui]
enabled = {b(c.webui.enabled)}
host = {self._q(c.webui.host)}
port = {c.webui.port}
access_token = {self._q(c.webui.access_token)}
session_ttl_hours = {c.webui.session_ttl_hours}
"""
        temp = CONFIG_PATH.with_suffix(".toml.tmp")
        await asyncio.to_thread(temp.write_text, text, encoding="utf-8")
        await asyncio.to_thread(temp.replace, CONFIG_PATH)

    async def _diagnostics(self, request: web.Request) -> web.Response:
        del request
        state_size = self.plugin._data_path.stat().st_size if self.plugin._data_path and self.plugin._data_path.exists() else 0
        scheduler = self.plugin._scheduler
        payload = {
            "plugin_version": "1.2.0",
            "webui_running": self.is_running,
            "webui_uptime_seconds": int(time.time() - self.started_at),
            "scheduler_running": bool(scheduler and not scheduler.done()),
            "summary_jobs": sum(1 for task in self.plugin._summary_tasks.values() if not task.done()),
            "indexed_sessions": len(self.plugin._session_user),
            "state_file": str(self.plugin._data_path or ""),
            "state_bytes": state_size,
            "config_file": str(CONFIG_PATH),
            "target_mode": self.plugin._target_mode(),
            "target_users": sorted(self.plugin._target_users()),
            "quiet_now": self.plugin._quiet(self._now()),
        }
        return self._json(True, payload)
