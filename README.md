# MaiBot 私人陪伴

这是 `menglimi/astrbot_plugin_private_companion` 的 MaiBot 原生核心移植版。它不是直接运行 AstrBot 代码，而是用 MaiBot SDK 重建持续陪伴主链。

> 原 AstrBot 插件版权归原作者所有，本项目保留移植说明与 GPL-3.0-or-later 许可。

已实现：目标私聊白名单、双层回复上下文注入、连续状态、近期互动、LLM 用户摘要、长期备注、专属称呼、免打扰、每日配额和主动关怀。

命令：

- `陪伴 状态`
- `陪伴 记忆`
- `陪伴 摘要`
- `陪伴 昵称 <称呼>`
- `陪伴 备注 <内容>`
- `陪伴 忘记 <序号>`
- `陪伴 心情 <描述>`
- `陪伴 主动测试`（管理员）
- `陪伴 帮助`

配置重点：

- `plugin.admin_qqs`：允许管理陪伴状态、主动测试和群聊提醒的 QQ。
- `plugin.target_mode`：`whitelist` 只陪伴名单内用户，`blacklist` 陪伴名单外用户。
- `plugin.target_qqs`：按 `target_mode` 解释的 QQ 列表。
- `webui.enabled`：是否启用独立 WebUI，公开部署前请设置强令牌。

原插件的设备感知、天气、梦境、日记、图片创作、TTS 和群聊关系网尚未移植。

独立 WebUI 默认监听 `0.0.0.0:6190`。访问令牌只保存在 `config.toml`，不会通过面板 API 回显。
