from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .webui import CompanionWebUI


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "heart"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="启用私人陪伴插件")
    config_version: str = Field(default="1.3.0", description="配置版本")
    admin_qqs: list[str] = Field(default_factory=list, description="管理员 QQ")
    target_qqs: list[str] = Field(default_factory=list, description="启用私人陪伴的 QQ")
    target_mode: str = Field(default="whitelist", description="陪伴目标模式：whitelist 白名单，blacklist 黑名单")


class PersonaSection(PluginConfigBase):
    __ui_label__ = "陪伴设定"
    __ui_icon__ = "sparkles"
    __ui_order__ = 1

    user_nickname: str = Field(default="", description="Bot 对主要用户的固定称呼，留空则自然称呼")
    relationship: str = Field(default="重要而亲近的长期陪伴对象", description="关系定位")
    shared_background: str = Field(default="", description="双方共同经历或关系背景")
    companion_style: str = Field(
        default="自然、熟悉、有分寸；会主动关心，但不机械问候，也不声称不存在的共同经历",
        description="陪伴表达风格",
    )
    inject_in_groups: bool = Field(default=False, description="目标用户在群聊发言时也注入陪伴上下文")


class ProactiveSection(PluginConfigBase):
    __ui_label__ = "主动关怀"
    __ui_icon__ = "message-circle"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="启用主动关怀")
    check_interval_minutes: int = Field(default=20, description="调度检查间隔", ge=5, le=1440)
    min_silence_hours: float = Field(default=4.0, description="用户多久没说话后才考虑主动联系", ge=0.5, le=168)
    min_gap_hours: float = Field(default=8.0, description="两次主动消息最小间隔", ge=1, le=168)
    max_per_day: int = Field(default=2, description="每位用户每天最多主动消息数", ge=0, le=12)
    chance_percent: int = Field(default=28, description="满足条件后每次检查的触发概率", ge=0, le=100)
    quiet_start_hour: int = Field(default=23, description="免打扰开始小时", ge=0, le=23)
    quiet_end_hour: int = Field(default=8, description="免打扰结束小时", ge=0, le=23)
    timezone: str = Field(default="Asia/Shanghai", description="IANA 时区")


class MemorySection(PluginConfigBase):
    __ui_label__ = "连续记忆"
    __ui_icon__ = "brain"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="记录近期互动并维护陪伴摘要")
    recent_message_count: int = Field(default=12, description="注入的近期互动条数", ge=0, le=40)
    max_notes: int = Field(default=40, description="每位用户最多保留的长期备注", ge=5, le=200)
    auto_summary_every: int = Field(default=16, description="每收到多少条消息更新一次摘要，0=关闭", ge=0, le=100)
    llm_model_task: str = Field(default="planner", description="摘要使用的模型任务")


class WebUISection(PluginConfigBase):
    __ui_label__ = "独立面板"
    __ui_icon__ = "panel-top"
    __ui_order__ = 4

    enabled: bool = Field(default=False, description="启用独立陪伴面板")
    host: str = Field(default="0.0.0.0", description="监听地址")
    port: int = Field(default=6190, description="监听端口", ge=1024, le=65535)
    access_token: str = Field(default="", description="面板登录令牌")
    session_ttl_hours: int = Field(default=24, description="登录会话有效小时数", ge=1, le=720)


class PrivateCompanionConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    persona: PersonaSection = Field(default_factory=PersonaSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    webui: WebUISection = Field(default_factory=WebUISection)


class PrivateCompanionPlugin(MaiBotPlugin):
    config_model = PrivateCompanionConfig
    _MSG_ID_RE = re.compile(r'msg_id="([^"]+)"')

    def __init__(self) -> None:
        super().__init__()
        self._data_path: Path | None = None
        self._data: dict[str, Any] = {"users": {}, "reminders": []}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._scheduler: asyncio.Task[None] | None = None
        self._reminder_scheduler: asyncio.Task[None] | None = None
        self._summary_tasks: dict[str, asyncio.Task[None]] = {}
        self._message_index: dict[str, deque[tuple[str, str]]] = {}
        self._session_user: dict[str, str] = {}
        self._session_private: dict[str, bool] = {}
        self._webui: CompanionWebUI | None = None

    async def on_load(self) -> None:
        self._data_path = Path(self.ctx.paths.data_dir) / "companion_state.json"
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        await self._load_data()
        if self.config.plugin.enabled and self.config.proactive.enabled:
            self._stop.clear()
            self._scheduler = asyncio.create_task(self._scheduler_loop(), name="private-companion.scheduler")
        self._reminder_scheduler = asyncio.create_task(
            self._reminder_scheduler_loop(), name="private-companion.reminders"
        )
        if self.config.webui.enabled:
            self._webui = CompanionWebUI(self)
            await self._webui.start()
        self.ctx.logger.info(
            "[private-companion] 已加载：targets=%s proactive=%s",
            self.config.plugin.target_qqs,
            self.config.proactive.enabled,
        )

    async def on_unload(self) -> None:
        self._stop.set()
        if self._scheduler and not self._scheduler.done():
            self._scheduler.cancel()
        if self._reminder_scheduler and not self._reminder_scheduler.done():
            self._reminder_scheduler.cancel()
        for task in self._summary_tasks.values():
            if not task.done():
                task.cancel()
        if self._webui is not None:
            await self._webui.stop()
            self._webui = None
        await self._save_data()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version
        await self._restart_proactive_runtime()
        if self.config.webui.enabled and self._webui is None:
            self._webui = CompanionWebUI(self)
            await self._webui.start()
        elif not self.config.webui.enabled and self._webui is not None:
            await self._webui.stop()
            self._webui = None

    async def _restart_proactive_runtime(self) -> None:
        self._stop.set()
        if self._scheduler and not self._scheduler.done():
            self._scheduler.cancel()
            try:
                await self._scheduler
            except (asyncio.CancelledError, Exception):
                pass
        self._stop = asyncio.Event()
        self._scheduler = None
        if self.config.plugin.enabled and self.config.proactive.enabled:
            self._scheduler = asyncio.create_task(
                self._scheduler_loop(), name="private-companion.scheduler"
            )

    def _targets(self) -> set[str]:
        return {str(x).strip() for x in self.config.plugin.target_qqs if str(x).strip()}

    def _target_mode(self) -> str:
        return self.config.plugin.target_mode if self.config.plugin.target_mode in {"whitelist", "blacklist"} else "whitelist"

    def _is_target(self, user_id: str) -> bool:
        user_id = str(user_id or "").strip()
        if not user_id:
            return False
        listed = self._targets()
        return user_id not in listed if self._target_mode() == "blacklist" else user_id in listed

    def _target_users(self) -> set[str]:
        if self._target_mode() == "whitelist":
            return self._targets()
        users = self._data.get("users", {})
        known = set(users) if isinstance(users, dict) else set()
        known.update(self._session_user.values())
        return {user_id for user_id in known if self._is_target(user_id)}

    def _admins(self) -> set[str]:
        return {str(x).strip() for x in self.config.plugin.admin_qqs if str(x).strip()}

    def _reminders(self) -> list[dict[str, Any]]:
        reminders = self._data.setdefault("reminders", [])
        if not isinstance(reminders, list):
            reminders = []
            self._data["reminders"] = reminders
        return reminders

    def _resolve_tool_target(self, requested: str, kwargs: dict[str, Any]) -> tuple[str, str]:
        """Resolve a private QQ target and prevent cross-user scheduling by ordinary users."""
        caller = self._resolve_tool_caller(kwargs)
        requested = str(requested or "").strip()
        if not caller:
            return "", "无法确认工具调用者，暂不创建提醒。"
        if requested and requested != caller and caller not in self._admins():
            return "", "只有管理员可以为其他 QQ 创建提醒。"
        target = requested or caller
        if not self._is_target(target):
            return "", f"QQ {target} 不在私人陪伴目标范围内。"
        return target, ""

    def _resolve_tool_caller(self, kwargs: dict[str, Any]) -> str:
        """从工具上下文、消息对象或会话映射中解析调用者 QQ。"""

        caller = str(kwargs.get("user_id") or "").strip()
        if caller:
            return caller

        message = kwargs.get("message")
        if isinstance(message, dict):
            info = message.get("message_info")
            if isinstance(info, dict):
                user_info = info.get("user_info")
                if isinstance(user_info, dict):
                    caller = str(user_info.get("user_id") or "").strip()
            if not caller:
                caller = str(message.get("user_id") or "").strip()
        if caller:
            return caller

        for key in ("stream_id", "session_id", "chat_id"):
            session_id = str(kwargs.get(key) or "").strip()
            if session_id:
                caller = str(self._session_user.get(session_id) or "").strip()
                if caller:
                    return caller
        return ""

    def _parse_reminder_time(
        self, run_at: str, delay_minutes: int, now: datetime
    ) -> tuple[datetime | None, str]:
        if run_at and delay_minutes:
            return None, "run_at 和 delay_minutes 二选一。"
        try:
            if run_at:
                value = run_at.strip().replace("Z", "+00:00")
                scheduled = datetime.fromisoformat(value)
                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(tzinfo=now.tzinfo)
                else:
                    scheduled = scheduled.astimezone(now.tzinfo)
            else:
                if delay_minutes < 1 or delay_minutes > 43200:
                    return None, "delay_minutes 必须在 1 到 43200 分钟之间。"
                scheduled = now + timedelta(minutes=delay_minutes)
        except ValueError:
            return None, "run_at 格式应为 YYYY-MM-DD HH:MM，或 ISO 8601 时间。"
        if scheduled <= now + timedelta(seconds=20):
            return None, "提醒时间至少需要比现在晚 20 秒。"
        if scheduled > now + timedelta(days=30):
            return None, "提醒时间不能超过 30 天。"
        return scheduled, ""

    def _reminder_visible_to(self, item: dict[str, Any], caller: str) -> bool:
        return caller in self._admins() or item.get("user_id") == caller or item.get("created_by") == caller

    @staticmethod
    def _reminder_summary(item: dict[str, Any]) -> str:
        destination = f"群 {item.get('group_id')} @QQ {item.get('user_id')}" if item.get("group_id") else f"QQ {item.get('user_id')}"
        return f"{item.get('id')}: {item.get('status')} | {item.get('run_at')} | {destination} | {item.get('message')}"

    @Tool(
        "manage_companion_reminders",
        brief_description="增删改查私人陪伴定时任务",
        detailed_description=(
            "统一管理私人或群聊 QQ 定时文字提醒。operation 使用 create（创建）、list（列表查询）、"
            "get（按 ID 查询）、update（修改未发送任务）或 delete（删除/取消未发送任务）。"
            "普通用户只能管理自己的提醒；群聊提醒的创建和修改只有管理员可用。"
            "群聊提醒中，target_user 必须填写要被 @ 的群友 QQ，不是机器人自己的 QQ；"
            "如果只是让机器人发文字而不 @ 人，请不要创建群聊提醒，或先确认目标 QQ。"
            "创建或修改时间使用 delay_minutes（延迟分钟数）或 run_at（北京时间 YYYY-MM-DD HH:MM）其中一个。"
        ),
        parameters=[
            ToolParameterInfo(
                name="operation",
                param_type=ToolParamType.STRING,
                description="操作类型",
                required=True,
                enum_values=["create", "list", "get", "update", "delete"],
            ),
            ToolParameterInfo(
                name="task_id",
                param_type=ToolParamType.STRING,
                description="任务 ID；get、update、delete 必填",
                required=False,
            ),
            ToolParameterInfo(
                name="message",
                param_type=ToolParamType.STRING,
                description="提醒内容；create 必填，update 可选，最多 500 字",
                required=False,
            ),
            ToolParameterInfo(
                name="delay_minutes",
                param_type=ToolParamType.INTEGER,
                description="从现在起延迟多少分钟；create/update 与 run_at 二选一",
                required=False,
            ),
            ToolParameterInfo(
                name="run_at",
                param_type=ToolParamType.STRING,
                description="执行时间，格式 YYYY-MM-DD HH:MM（Asia/Shanghai）；create/update 与 delay_minutes 二选一",
                required=False,
            ),
            ToolParameterInfo(
                name="target_user",
                param_type=ToolParamType.STRING,
                description="被提醒对象的 QQ；私聊留空表示当前用户；群聊必填要被 @ 的群友 QQ，禁止填写机器人自己的 QQ",
                required=False,
            ),
            ToolParameterInfo(
                name="group_id",
                param_type=ToolParamType.STRING,
                description="群号；创建群聊提醒时填写，只有管理员可用。与 target_user 一起使用，target_user 是被 @ 的群友",
                required=False,
            ),
            ToolParameterInfo(
                name="status",
                param_type=ToolParamType.STRING,
                description="list 查询的状态，默认 pending；可选 pending、sent、cancelled、all",
                required=False,
                enum_values=["pending", "sent", "cancelled", "all"],
                default="pending",
            ),
        ],
    )
    async def manage_companion_reminders(
        self,
        operation: str = "",
        task_id: str = "",
        message: str = "",
        delay_minutes: int = 0,
        run_at: str = "",
        target_user: str = "",
        group_id: str = "",
        status: str = "pending",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "私人陪伴插件未启用。"}
        operation = str(operation or "").strip().lower()
        if operation not in {"create", "list", "get", "update", "delete"}:
            return {"success": False, "content": "operation 必须是 create、list、get、update 或 delete。"}
        caller = self._resolve_tool_caller(kwargs)
        if not caller:
            return {"success": False, "content": "无法确认工具调用者。"}
        reminders = self._reminders()
        task_id = str(task_id or "").strip()

        if operation in {"get", "update", "delete"}:
            if not task_id:
                return {"success": False, "content": f"{operation} 操作必须填写 task_id。"}
            item = next((entry for entry in reminders if str(entry.get("id")) == task_id), None)
            if item is None:
                return {"success": False, "content": f"未找到提醒 {task_id}。"}
            if not self._reminder_visible_to(item, caller):
                return {"success": False, "content": "你没有权限管理这条提醒。"}
            if operation == "get":
                return {"success": True, "content": self._reminder_summary(item), "task": item}
            if item.get("status") != "pending":
                return {"success": False, "content": "只能修改或删除尚未发送的提醒。"}
            if operation == "delete":
                item["status"] = "cancelled"
                item["cancelled_at"] = datetime.now(ZoneInfo(self.config.proactive.timezone)).isoformat()
                await self._save_data()
                return {"success": True, "content": f"已删除提醒 {task_id}。", "task_id": task_id, "task": item}

            changed = False
            text = str(message or "").strip()
            if text:
                if len(text) > 500:
                    return {"success": False, "content": "提醒内容不能超过 500 字。"}
                item["message"] = text
                changed = True
            if run_at or int(delay_minutes or 0):
                now = datetime.now(ZoneInfo(self.config.proactive.timezone))
                scheduled, error = self._parse_reminder_time(str(run_at or ""), int(delay_minutes or 0), now)
                if error or scheduled is None:
                    return {"success": False, "content": error}
                item["run_at"] = scheduled.isoformat()
                changed = True
            if not changed:
                return {"success": False, "content": "update 至少要提供 message、run_at 或 delay_minutes。"}
            item["updated_at"] = datetime.now(ZoneInfo(self.config.proactive.timezone)).isoformat()
            await self._save_data()
            return {"success": True, "content": f"已更新提醒 {task_id}。", "task_id": task_id, "task": item}

        if operation == "list":
            status = str(status or "pending").strip().lower()
            if status not in {"pending", "sent", "cancelled", "all"}:
                return {"success": False, "content": "status 必须是 pending、sent、cancelled 或 all。"}
            items = [
                item
                for item in reminders
                if self._reminder_visible_to(item, caller)
                and (status == "all" or item.get("status") == status)
            ]
            items.sort(key=lambda item: str(item.get("run_at") or ""))
            return {
                "success": True,
                "content": "\n".join(self._reminder_summary(item) for item in items[:100]) or "没有符合条件的定时任务。",
                "tasks": items[:100],
            }

        text = str(message or "").strip()
        if not text:
            return {"success": False, "content": "创建提醒必须填写 message。"}
        if len(text) > 500:
            return {"success": False, "content": "提醒内容不能超过 500 字。"}
        requested_group = str(group_id or "").strip()
        if requested_group:
            if caller not in self._admins():
                return {"success": False, "content": "只有管理员可以创建群聊提醒。"}
            target = str(target_user or "").strip()
            if not target or not target.isdigit():
                return {"success": False, "content": "群聊提醒必须填写数字 QQ 号作为 @ 目标。"}
            bot_user_id = str(getattr(self.ctx, "bot_user_id", "") or "").strip()
            if not bot_user_id:
                bot_user_id = str(getattr(self.ctx, "self_id", "") or "").strip()
            if bot_user_id and target == bot_user_id:
                return {"success": False, "content": "target_user 不能填写机器人自己的 QQ，请填写要被提醒的群友 QQ。"}
        else:
            target, error = self._resolve_tool_target(target_user, kwargs)
            if error:
                return {"success": False, "content": error}
        now = datetime.now(ZoneInfo(self.config.proactive.timezone))
        scheduled, error = self._parse_reminder_time(str(run_at or ""), int(delay_minutes or 0), now)
        if error or scheduled is None:
            return {"success": False, "content": error}
        own_count = sum(1 for item in reminders if item.get("status") == "pending" and item.get("user_id") == target)
        if own_count >= 50:
            return {"success": False, "content": "该用户待执行提醒已达到 50 条上限。"}
        task = {
            "id": f"rem-{uuid.uuid4().hex[:12]}",
            "user_id": target,
            "created_by": caller,
            "stream_id": str(kwargs.get("stream_id") or ""),
            "message": text,
            "run_at": scheduled.isoformat(),
            "created_at": now.isoformat(),
            "status": "pending",
        }
        if requested_group:
            task["group_id"] = requested_group
        reminders.append(task)
        await self._save_data()
        return {"success": True, "content": f"已创建提醒 {task['id']}。", "task_id": task["id"], "task": task}

    def _user(self, user_id: str) -> dict[str, Any]:
        users = self._data.setdefault("users", {})
        user = users.setdefault(
            user_id,
            {
                "nickname": "",
                "summary": "",
                "notes": [],
                "recent": [],
                "mood": "平静、愿意交流",
                "message_count": 0,
                "last_interaction": "",
                "last_proactive": "",
                "proactive_day": "",
                "proactive_count": 0,
            },
        )
        return user

    async def _load_data(self) -> None:
        if not self._data_path or not self._data_path.exists():
            return
        try:
            raw = await asyncio.to_thread(self._data_path.read_text, encoding="utf-8")
            loaded = json.loads(raw)
            if isinstance(loaded, dict) and isinstance(loaded.get("users", {}), dict):
                self._data = loaded
        except Exception as exc:
            self.ctx.logger.warning("[private-companion] 状态读取失败：%s", exc)

    async def _save_data(self) -> None:
        if not self._data_path:
            return
        async with self._lock:
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            temp = self._data_path.with_suffix(".tmp")
            await asyncio.to_thread(temp.write_text, payload, encoding="utf-8")
            await asyncio.to_thread(temp.replace, self._data_path)

    @staticmethod
    def _extract_sender(message: dict[str, Any]) -> str:
        info = message.get("message_info")
        if isinstance(info, dict):
            user = info.get("user_info")
            if isinstance(user, dict):
                return str(user.get("user_id") or "").strip()
        return str(message.get("user_id") or "").strip()

    @staticmethod
    def _is_private_message(message: dict[str, Any]) -> bool:
        info = message.get("message_info")
        if not isinstance(info, dict):
            return not bool(message.get("group_id"))
        group = info.get("group_info")
        if isinstance(group, dict) and str(group.get("group_id") or "").strip():
            return False
        return not bool(info.get("group_id") or message.get("group_id"))

    @staticmethod
    def _extract_text(message: dict[str, Any]) -> str:
        for key in ("processed_plain_text", "plain_text", "message", "content"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        return ""

    @HookHandler(
        "chat.receive.after_process",
        name="private_companion_capture",
        description="记录目标用户的互动与会话归属",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=2500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def capture_message(self, message: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if not self.config.plugin.enabled or not isinstance(message, dict):
            return {"action": "continue"}
        user_id = self._extract_sender(message)
        session_id = str(message.get("session_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        if not user_id or not self._is_target(user_id):
            return {"action": "continue"}
        is_private = self._is_private_message(message)
        if session_id:
            self._session_user[session_id] = user_id
            self._session_private[session_id] = is_private
            if message_id:
                dq = self._message_index.setdefault(session_id, deque(maxlen=120))
                dq.append((message_id, user_id))
        if not is_private and not self.config.persona.inject_in_groups:
            return {"action": "continue"}
        text = self._extract_text(message)
        if text and self.config.memory.enabled and not text.startswith(("/陪伴", "陪伴 ")):
            user = self._user(user_id)
            now = datetime.now(ZoneInfo(self.config.proactive.timezone))
            user["recent"] = (user.get("recent", []) + [{"time": now.isoformat(), "text": text}])[-40:]
            user["last_interaction"] = now.isoformat()
            user["message_count"] = int(user.get("message_count", 0)) + 1
            self._update_mood(user, text)
            await self._save_data()
            every = self.config.memory.auto_summary_every
            if every and user["message_count"] % every == 0:
                self._schedule_summary(user_id)
        return {"action": "continue"}

    @staticmethod
    def _update_mood(user: dict[str, Any], text: str) -> None:
        if any(word in text for word in ("难过", "不开心", "累", "烦", "失眠", "压力")):
            user["mood"] = "有些担心对方，想温和陪着，不急于说教"
        elif any(word in text for word in ("开心", "好耶", "哈哈", "成功了", "搞定")):
            user["mood"] = "被对方的好心情感染，轻松而亲近"
        elif any(word in text for word in ("晚安", "睡了", "困了")):
            user["mood"] = "安静柔和，尊重对方休息"
        else:
            user["mood"] = "平静、熟悉、愿意继续交流"

    def _schedule_summary(self, user_id: str) -> None:
        old = self._summary_tasks.get(user_id)
        if old and not old.done():
            return
        task = asyncio.create_task(self._summarize_user(user_id), name=f"private-companion.summary.{user_id}")
        self._summary_tasks[user_id] = task
        task.add_done_callback(lambda _task: self._summary_tasks.pop(user_id, None))

    async def _summarize_user(self, user_id: str) -> None:
        user = self._user(user_id)
        recent = user.get("recent", [])[-20:]
        if not recent:
            return
        lines = "\n".join(f"- {item.get('text', '')[:240]}" for item in recent)
        prompt = (
            "请把以下聊天片段整理为供陪伴型角色内部使用的可靠用户摘要。"
            "只记录用户明确表达的近况、偏好、计划、困扰和未完话题；不要臆测，不写回复，不超过180字。\n"
            f"旧摘要：{user.get('summary') or '无'}\n近期片段：\n{lines}"
        )
        try:
            result = await self.ctx.llm.generate(prompt=prompt, model=self.config.memory.llm_model_task)
            if isinstance(result, dict):
                summary = str(result.get("response") or result.get("content") or result.get("text") or "").strip()
            else:
                summary = str(result or "").strip()
            if summary:
                user["summary"] = summary[:800]
                await self._save_data()
        except Exception as exc:
            self.ctx.logger.warning("[private-companion] 用户摘要失败(%s)：%s", user_id, exc)

    def _resolve_prompt_user(self, session_id: str, messages: list[dict[str, Any]] | None) -> str:
        if messages:
            for msg in reversed(messages):
                if not isinstance(msg, dict) or msg.get("role") != "user" or msg.get("tool_call_id"):
                    continue
                match = self._MSG_ID_RE.search(str(msg.get("content") or ""))
                if match:
                    for indexed_id, user_id in reversed(self._message_index.get(session_id, ())):
                        if indexed_id == match.group(1):
                            return user_id
                    break
        return self._session_user.get(session_id, "")

    def _life_context(self, user_id: str) -> str:
        user = self._user(user_id)
        now = datetime.now(ZoneInfo(self.config.proactive.timezone))
        hour = now.hour
        if 0 <= hour < 7:
            rhythm = "深夜休息时段；表达应更轻、更短，不主动制造紧迫感"
        elif hour < 11:
            rhythm = "上午，正在开始今天的生活节奏"
        elif hour < 14:
            rhythm = "中午，适合自然聊吃饭、休息和上午发生的事"
        elif hour < 18:
            rhythm = "下午，保持自己的生活感，不必句句围着用户转"
        elif hour < 23:
            rhythm = "晚上，适合回顾一天、分享感受或陪对方放松"
        else:
            rhythm = "临近休息，语气放缓并尊重免打扰"
        notes = [str(x) for x in user.get("notes", [])[-self.config.memory.max_notes :] if str(x).strip()]
        recent = user.get("recent", [])[-self.config.memory.recent_message_count :]
        recent_text = "；".join(str(x.get("text") or "")[:100] for x in recent if isinstance(x, dict))
        nickname = str(user.get("nickname") or self.config.persona.user_nickname or "").strip()
        parts = [
            "【私人陪伴上下文】",
            f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}；生活节奏：{rhythm}",
            f"关系定位：{self.config.persona.relationship}",
            f"当前内在状态：{user.get('mood') or '平静、愿意交流'}",
            f"表达原则：{self.config.persona.companion_style}",
        ]
        if nickname:
            parts.append(f"对用户的称呼：{nickname}（自然使用，不要每句话都叫）")
        if self.config.persona.shared_background.strip():
            parts.append(f"共同背景：{self.config.persona.shared_background.strip()}")
        if user.get("summary"):
            parts.append(f"可靠用户摘要：{user['summary']}")
        if notes:
            parts.append("长期备注：" + "；".join(notes[-12:]))
        if recent_text:
            parts.append("近期互动线索：" + recent_text[:1000])
        parts.append("把这些信息内化后自然回应，不要复述本上下文，不要声称自己真的有身体、设备或未发生过的经历。")
        return "\n".join(parts)

    def _eligible_session(self, session_id: str, user_id: str) -> bool:
        if not user_id or not self._is_target(user_id):
            return False
        return self._session_private.get(session_id, True) or self.config.persona.inject_in_groups

    @HookHandler(
        "maisaka.planner.before_request",
        name="private_companion_planner_context",
        description="向目标用户会话注入持续陪伴上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_planner(self, messages: list[dict[str, Any]] | None = None, session_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if not self.config.plugin.enabled or not isinstance(messages, list):
            return {"action": "continue"}
        user_id = self._resolve_prompt_user(session_id, messages)
        if not self._eligible_session(session_id, user_id):
            return {"action": "continue"}
        hint = {"role": "system", "content": self._life_context(user_id)}
        modified = list(messages)
        insert_at = 0
        for i, msg in enumerate(modified):
            if isinstance(msg, dict) and msg.get("role") == "system":
                insert_at = i + 1
        modified.insert(insert_at, hint)
        return {"action": "continue", "modified_kwargs": {"messages": modified}}

    @HookHandler(
        "maisaka.planner.after_response",
        name="private_companion_reply_context",
        description="把陪伴上下文传给回复模型",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_reply(self, tool_calls: list[dict[str, Any]] | None = None, session_id: str = "", **kwargs: Any) -> dict[str, Any]:
        del kwargs
        if not self.config.plugin.enabled or not isinstance(tool_calls, list):
            return {"action": "continue"}
        user_id = self._session_user.get(session_id, "")
        if not self._eligible_session(session_id, user_id):
            return {"action": "continue"}
        modified = [dict(item) for item in tool_calls]
        changed = False
        for item in modified:
            function = item.get("function")
            if not isinstance(function, dict) or function.get("name") != "reply":
                continue
            function = dict(function)
            args = dict(function.get("arguments") or {})
            context = self._life_context(user_id)
            old = str(args.get("reference_info") or "").strip()
            args["reference_info"] = f"{old}\n\n{context}".strip()
            function["arguments"] = args
            item["function"] = function
            changed = True
            break
        if not changed:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": {"tool_calls": modified}}

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._proactive_tick(force=False)
            except Exception as exc:
                self.ctx.logger.warning("[private-companion] 主动调度失败：%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(300, self.config.proactive.check_interval_minutes * 60))
            except asyncio.TimeoutError:
                pass

    async def _reminder_scheduler_loop(self) -> None:
        """Dispatch persisted reminders independently of the probabilistic care loop."""
        while not self._stop.is_set():
            try:
                await self._dispatch_reminders()
            except Exception as exc:
                self.ctx.logger.warning("[private-companion] 定时提醒调度失败：%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _dispatch_reminders(self) -> None:
        now = datetime.now(ZoneInfo(self.config.proactive.timezone))
        changed = False
        for item in list(self._reminders()):
            if item.get("status") != "pending":
                continue
            due_at = self._parse_time(item.get("run_at"), now)
            if due_at is None or due_at > now:
                continue
            user_id = str(item.get("user_id") or "").strip()
            group_id = str(item.get("group_id") or "").strip()
            stream_id = await self._find_group_stream(group_id) if group_id else await self._find_private_stream(user_id)
            if not stream_id:
                stream_id = str(item.get("stream_id") or "").strip()
            if not stream_id:
                destination = f"群 {group_id}" if group_id else f"用户 {user_id} 的私聊"
                self.ctx.logger.info("[private-companion] 暂无%s，提醒稍后重试", destination)
                continue
            try:
                if group_id and user_id:
                    await self.ctx.send.hybrid(
                        [
                            {"type": "at", "data": {"target_user_id": user_id}},
                            {"type": "text", "data": " " + str(item.get("message") or "")},
                        ],
                        stream_id,
                    )
                else:
                    await self.ctx.send.text(str(item.get("message") or ""), stream_id)
            except Exception as exc:
                self.ctx.logger.warning(
                    "[private-companion] 发送提醒失败(%s)：%s", item.get("id"), exc
                )
                continue
            item["status"] = "sent"
            item["sent_at"] = now.isoformat()
            changed = True
            destination = f"群 {group_id} @ {user_id}" if group_id else user_id
            self.ctx.logger.info("[private-companion] 定时提醒已发送：%s -> %s", item.get("id"), destination)
        if changed:
            await self._save_data()

    def _quiet(self, now: datetime) -> bool:
        start, end, hour = self.config.proactive.quiet_start_hour, self.config.proactive.quiet_end_hour, now.hour
        if start == end:
            return False
        return start <= hour < end if start < end else hour >= start or hour < end

    async def _find_private_stream(self, user_id: str) -> str:
        try:
            stream = await self.ctx.chat.get_stream_by_user_id(user_id=user_id, platform="qq")
            candidates = stream.get("streams", []) if isinstance(stream, dict) and "streams" in stream else [stream]
            for item in candidates:
                if isinstance(item, dict):
                    sid = str(item.get("stream_id") or item.get("session_id") or item.get("id") or "").strip()
                    if sid:
                        return sid
        except Exception:
            pass
        try:
            streams = await self.ctx.chat.get_private_streams(platform="qq")
            if isinstance(streams, dict):
                streams = streams.get("streams", [])
            for item in streams or []:
                if isinstance(item, dict) and str(item.get("user_id") or "").strip() == user_id:
                    return str(item.get("stream_id") or item.get("session_id") or item.get("id") or "").strip()
        except Exception as exc:
            self.ctx.logger.debug("[private-companion] 私聊流查询失败：%s", exc)
        return ""

    async def _find_group_stream(self, group_id: str) -> str:
        if not group_id:
            return ""
        try:
            stream = await self.ctx.chat.get_stream_by_group_id(group_id=group_id, platform="qq")
            candidates = stream.get("streams", []) if isinstance(stream, dict) and "streams" in stream else [stream]
            for item in candidates:
                if isinstance(item, dict):
                    sid = str(item.get("stream_id") or item.get("session_id") or item.get("id") or "").strip()
                    if sid:
                        return sid
        except Exception:
            pass
        try:
            streams = await self.ctx.chat.get_group_streams(platform="qq")
            if isinstance(streams, dict):
                streams = streams.get("streams", [])
            for item in streams or []:
                if isinstance(item, dict) and str(item.get("group_id") or "").strip() == group_id:
                    return str(item.get("stream_id") or item.get("session_id") or item.get("id") or "").strip()
        except Exception as exc:
            self.ctx.logger.debug("[private-companion] 群聊流查询失败：%s", exc)
        return ""

    async def _proactive_tick(self, force: bool, target_user: str = "") -> dict[str, Any]:
        now = datetime.now(ZoneInfo(self.config.proactive.timezone))
        if not force and self._quiet(now):
            return {"skipped": "quiet_hours"}
        sent = 0
        targets = [target_user] if target_user else sorted(self._target_users())
        for user_id in targets:
            user = self._user(user_id)
            day = now.strftime("%Y-%m-%d")
            if user.get("proactive_day") != day:
                user["proactive_day"] = day
                user["proactive_count"] = 0
            if not force:
                if int(user.get("proactive_count", 0)) >= self.config.proactive.max_per_day:
                    continue
                last_interaction = self._parse_time(user.get("last_interaction"), now)
                last_proactive = self._parse_time(user.get("last_proactive"), now)
                if last_interaction and now - last_interaction < timedelta(hours=self.config.proactive.min_silence_hours):
                    continue
                if last_proactive and now - last_proactive < timedelta(hours=self.config.proactive.min_gap_hours):
                    continue
                if random.randint(1, 100) > self.config.proactive.chance_percent:
                    continue
            stream_id = await self._find_private_stream(user_id)
            if not stream_id:
                self.ctx.logger.info("[private-companion] 未找到目标 %s 的私聊流", user_id)
                continue
            intent = (
                "以长期陪伴者的身份，自然地主动联系这个用户。根据当前时间、关系和近期互动选择一个真实的小话题或关心点。"
                "不要说这是定时任务，不要机械问候，不要虚构刚发生的事件；一两句话即可。\n"
                + self._life_context(user_id)
            )
            await self.ctx.maisaka.proactive.trigger(stream_id=stream_id, intent=intent, reason="plugin.private-companion")
            user["last_proactive"] = now.isoformat()
            user["proactive_count"] = int(user.get("proactive_count", 0)) + 1
            sent += 1
        await self._save_data()
        return {"sent": sent}

    @staticmethod
    def _parse_time(value: Any, now: datetime) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed.replace(tzinfo=now.tzinfo) if parsed.tzinfo is None else parsed.astimezone(now.tzinfo)
        except ValueError:
            return None

    def _command_user(self, kwargs: dict[str, Any]) -> str:
        return str(kwargs.get("user_id") or "").strip()

    async def _reply(self, stream_id: str, text: str) -> tuple[bool, str, int]:
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    @Command("companion", description="私人陪伴管理", pattern=r"^/?(?:陪伴|companion)(?:\s+(?P<action>状态|帮助|记忆|摘要|昵称|备注|忘记|心情|主动测试|status|help|memory|summary|nickname|note|forget|mood|test)(?:\s+(?P<arg>.*))?)?\s*$")
    async def companion_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        user_id = self._command_user(kwargs)
        groups = kwargs.get("matched_groups") or {}
        action = str(groups.get("action") or "状态").strip()
        arg = str(groups.get("arg") or "").strip()
        if not self._is_target(user_id) and user_id not in self._admins():
            return await self._reply(stream_id, "这个陪伴空间没有向你开放。")
        target_users = self._target_users()
        target = user_id if self._is_target(user_id) else (sorted(target_users)[0] if target_users else user_id)
        user = self._user(target)
        if action in {"帮助", "help"}:
            return await self._reply(stream_id, "陪伴 状态｜陪伴 记忆｜陪伴 摘要｜陪伴 昵称 <称呼>｜陪伴 备注 <内容>｜陪伴 忘记 <序号>｜陪伴 心情 <描述>｜陪伴 主动测试")
        if action in {"状态", "status"}:
            now = datetime.now(ZoneInfo(self.config.proactive.timezone))
            text = (
                f"私人陪伴状态\n对象：{target}\n关系：{self.config.persona.relationship}\n"
                f"称呼：{user.get('nickname') or self.config.persona.user_nickname or '自然称呼'}\n"
                f"当前状态：{user.get('mood')}\n摘要：{user.get('summary') or '尚未形成'}\n"
                f"长期备注：{len(user.get('notes', []))} 条\n近期互动：{len(user.get('recent', []))} 条\n"
                f"今日主动：{user.get('proactive_count', 0)}/{self.config.proactive.max_per_day}\n现在：{now.strftime('%Y-%m-%d %H:%M')}"
            )
            return await self._reply(stream_id, text)
        if action in {"记忆", "memory"}:
            notes = user.get("notes", [])
            recent = user.get("recent", [])[-6:]
            lines = [f"摘要：{user.get('summary') or '暂无'}", "长期备注："]
            lines.extend(f"{i}. {note}" for i, note in enumerate(notes, 1))
            lines.append("近期互动：")
            lines.extend(f"- {item.get('text', '')[:120]}" for item in recent if isinstance(item, dict))
            return await self._reply(stream_id, "\n".join(lines)[:3500])
        if action in {"摘要", "summary"}:
            self._schedule_summary(target)
            return await self._reply(stream_id, "已开始整理近期互动摘要，完成后会写入连续记忆。")
        if action in {"昵称", "nickname"}:
            if not arg:
                return await self._reply(stream_id, f"当前称呼：{user.get('nickname') or '未单独设置'}")
            user["nickname"] = arg[:40]
            await self._save_data()
            return await self._reply(stream_id, f"记住了，以后自然地称呼你为“{user['nickname']}”。")
        if action in {"备注", "note"}:
            if not arg:
                return await self._reply(stream_id, "用法：陪伴 备注 <要长期记住的内容>")
            notes = user.setdefault("notes", [])
            notes.append(arg[:400])
            del notes[: max(0, len(notes) - self.config.memory.max_notes)]
            await self._save_data()
            return await self._reply(stream_id, f"已加入长期备注（共 {len(notes)} 条）。")
        if action in {"忘记", "forget"}:
            try:
                index = int(arg) - 1
                removed = user.setdefault("notes", []).pop(index)
            except (ValueError, IndexError):
                return await self._reply(stream_id, "请提供“陪伴 记忆”中有效的备注序号。")
            await self._save_data()
            return await self._reply(stream_id, f"已删除备注：{removed}")
        if action in {"心情", "mood"}:
            if not arg:
                return await self._reply(stream_id, f"当前内在状态：{user.get('mood')}")
            user["mood"] = arg[:120]
            await self._save_data()
            return await self._reply(stream_id, "当前内在状态已更新。")
        if action in {"主动测试", "test"}:
            if user_id not in self._admins():
                return await self._reply(stream_id, "只有管理员能触发主动消息测试。")
            result = await self._proactive_tick(force=True, target_user=target)
            return await self._reply(stream_id, f"主动关怀测试完成：{result}")
        return await self._reply(stream_id, "未知子命令，发送“陪伴 帮助”查看用法。")


def create_plugin() -> PrivateCompanionPlugin:
    return PrivateCompanionPlugin()
