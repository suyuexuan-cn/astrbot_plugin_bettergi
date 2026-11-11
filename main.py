import base64
import os
import tempfile
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

from .service import (
    EventStore,
    RemoteConnectionManager,
    Scheduler,
    WebhookServer,
    build_command,
    create_runner,
)


@register("bettergi", "BetterGI", "BetterGI 远程控制插件", "2.1.8")
class BetterGIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._init_config()

        logger.info("[BetterGI] 插件初始化中...")
        logger.debug("[BetterGI-Init] 前缀: %s", self._prefix)
        logger.debug(
            "[BetterGI-Init] 命令别名: run=%s, status=%s, stop=%s, log=%s, bind=%s, help=%s",
            self._cmd_run, self._cmd_status, self._cmd_stop,
            self._cmd_log, self._cmd_bind, self._cmd_help,
        )

        self._event_store = EventStore(self._get_data_dir())
        self._scheduler = Scheduler()

        # 远程模式连接管理器（认证由 AstrBot API 密钥统一处理）
        mode = config.get("mode", "local")
        self._remote_manager: RemoteConnectionManager | None = None
        if mode == "remote":
            self._remote_manager = RemoteConnectionManager()
            logger.debug("[BetterGI-Init] 已创建 RemoteConnectionManager")

        self._runner = create_runner(config, remote_manager=self._remote_manager)

        webhook_cfg = config.get("webhook", {})
        logger.debug(
            "[BetterGI-Init] Webhook: enable=%s, path=%s",
            webhook_cfg.get("enable", True),
            webhook_cfg.get("path", "/webhook"),
        )

        self._webhook_server = WebhookServer(
            path=webhook_cfg.get("path", "/webhook"),
        )
        self._webhook_server.set_handler(self._on_webhook_event)

        raw_umo = config.get("notify", {}).get("umo", [])
        if isinstance(raw_umo, str):
            self._notify_umo: list[str] = [raw_umo] if raw_umo else []
        else:
            self._notify_umo = [str(u) for u in raw_umo] if raw_umo else []
        logger.debug("[BetterGI-Init] 通知绑定会话: %s", self._notify_umo or "无")
        logger.debug("[BetterGI-Init] 数据目录: %s", self._get_data_dir())

        self._event_map: dict[str, str] = {
            "notify.test": "notify_test",
            "dragon.start": "dragon_start",
            "dragon.end": "dragon_end",
            "group.start": "group_start",
            "group.end": "group_end",
            "task.cancel": "task_cancel",
            "task.error": "task_error",
            "domain.start": "domain_start",
            "domain.end": "domain_end",
            "domain.reward": "domain_reward",
            "domain.retry": "domain_retry",
            "tcg.start": "tcg_start",
            "tcg.end": "tcg_end",
            "album.start": "album_start",
            "album.end": "album_end",
            "album.error": "album_error",
            "daily.reward": "daily_reward",
            "autoeat.start": "autoeat_start",
            "autoeat.end": "autoeat_end",
            "autoeat.info": "autoeat_info",
            "js.custom": "js_custom",
            "js.error": "js_error",
        }

        # 任务结束类事件：收到后清空“运行中”标记，避免状态一直卡在运行中
        self._end_events: set[str] = {
            "dragon.end",
            "group.end",
            "task.cancel",
            "task.error",
            "domain.end",
            "tcg.end",
            "album.end",
            "autoeat.end",
        }

    def _init_config(self) -> None:
        self._cmd_names = self.config.get("command_names", {})
        self._prefix = self._cmd_names.get("prefix", "better")

        def _get_aliases(key: str, default: str) -> list[str]:
            val = self._cmd_names.get(key, [default])
            if isinstance(val, str):
                return [val]
            return [str(v) for v in val] if val else [default]

        self._cmd_run = _get_aliases("run", "运行")
        self._cmd_status = _get_aliases("status", "状态")
        self._cmd_stop = _get_aliases("stop", "停止")
        self._cmd_log = _get_aliases("log", "日志")
        self._cmd_bind = _get_aliases("bind", "绑定")
        self._cmd_help = _get_aliases("help", "帮助")

    def _get_data_dir(self) -> str:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return os.path.join(get_astrbot_data_path(), "plugin_data", "bettergi")
        except Exception:
            return ""

    def _check_permission(self, event: AstrMessageEvent) -> bool:
        sender_id = str(event.get_sender_id())

        try:
            bot_admins = self.context.get_bot_config().get("admins_id", [])
        except Exception:
            bot_admins = []

        if sender_id in [str(a) for a in bot_admins]:
            logger.debug("[BetterGI-Perm] 用户 %s 是管理员，放行", sender_id)
            return True

        masters = self.config.get("better_master", [])
        if sender_id in [str(m) for m in masters]:
            logger.debug("[BetterGI-Perm] 用户 %s 在 better_master 列表中，放行", sender_id)
            return True

        logger.warning(
            "[BetterGI-Perm] 用户 %s 无权限（非管理员且不在 better_master 列表）",
            sender_id,
        )
        return False

    def _get_commands(self) -> list[dict[str, Any]]:
        """获取配置的命令列表。"""
        return self.config.get("commands", [])

    def _match_cmd(self, sub: str, aliases: list[str]) -> str | None:
        """匹配命令别名，返回匹配到的别名（长别名优先），未匹配返回 None。"""
        for alias in sorted(aliases, key=len, reverse=True):
            if sub.startswith(alias):
                return alias
        return None

    def _get_command_at(self, index: int) -> list[str] | None:
        """根据序号获取命令参数。"""
        commands = self._get_commands()
        if not commands or index < 1 or index > len(commands):
            return None
        entry = commands[index - 1]
        template_key = entry.get("__template_key", "dragon")
        config_name = entry.get("config_name", "")
        return build_command(template_key, config_name)

    async def initialize(self):
        """插件加载和热重载时启动服务。"""
        webhook_cfg = self.config.get("webhook", {})
        if webhook_cfg.get("enable", True):
            ok = self._webhook_server.register(self.context)
            if not ok:
                logger.warning("[BetterGI] Webhook 路由注册失败")

        # 注册远程模式 SSE 和结果上报路由
        if self._remote_manager is not None:
            self._register_remote_routes()

        scheduled = self.config.get("scheduled_task", {})
        if scheduled.get("enable", False):
            self._scheduler.start(
                self._run_scheduled_task,
                scheduled.get("run_hour", 4),
                scheduled.get("run_minute", 0),
            )

        logger.info("[BetterGI] 插件已加载")

    def _register_remote_routes(self) -> None:
        """注册远程模式的 Web API 路由（认证由 AstrBot API 密钥统一处理）。"""
        try:
            from astrbot.api.web import json_response, request, stream_response

            # SSE 连接端点：辅助程序连接上来等待指令
            async def sse_handler():
                return stream_response(
                    self._remote_manager.sse_stream(),
                    content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

            self.context.register_web_api(
                "/bettergi/remote/connect",
                sse_handler,
                ["GET"],
                "BetterGI 远程辅助程序 SSE 连接端点",
            )

            # 结果上报端点：辅助程序执行完后上报结果
            async def result_handler():
                data = await request.json(default={})
                ok = await self._remote_manager.handle_result(data)
                return json_response({"status": "ok" if ok else "not_found"})

            self.context.register_web_api(
                "/bettergi/remote/result",
                result_handler,
                ["POST"],
                "BetterGI 远程辅助程序结果上报",
            )

            # 健康检查
            async def health_handler():
                return json_response({
                    "status": "ok",
                    "connected": self._remote_manager.is_connected,
                })

            self.context.register_web_api(
                "/bettergi/remote/health",
                health_handler,
                ["GET"],
                "BetterGI 远程模式健康检查",
            )

            logger.info("[BetterGI] 远程模式路由已注册")
            logger.info(
                "[BetterGI] SSE地址: http://<AstrBot地址>:<端口>/api/v1/plugins/extensions/bettergi/remote/connect"
            )
            logger.info(
                "[BetterGI] 辅助程序需在 config.yaml 中配置 api_key（AstrBot 设置→OpenAPI 创建，只勾选 plugin 权限）"
            )
        except Exception as e:
            logger.error("[BetterGI] 注册远程模式路由失败: %s", e, exc_info=True)

    @filter.regex('.*', priority=1)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，手动解析自定义命令。"""
        msg = event.message_str.strip()
        prefix = self._prefix

        if not msg.startswith(prefix):
            return

        sub = msg[len(prefix) :].strip()
        if not sub:
            return

        if not self._check_permission(event):
            return

        logger.debug("[BetterGI-Msg] 收到命令: msg='%s', prefix='%s', sub='%s'", msg, prefix, sub)

        matched = self._match_cmd(sub, self._cmd_run)
        if matched is not None:
            event.stop_event()
            cmd_arg = sub[len(matched) :].strip()
            logger.debug("[BetterGI-Msg] 匹配到 run: alias='%s', arg='%s'", matched, cmd_arg)
            async for result in self._handle_run(event, cmd_arg):
                yield result
            return

        matched = self._match_cmd(sub, self._cmd_status)
        if matched is not None:
            event.stop_event()
            logger.debug("[BetterGI-Msg] 匹配到 status: alias='%s'", matched)
            async for result in self._handle_status(event):
                yield result
            return

        matched = self._match_cmd(sub, self._cmd_stop)
        if matched is not None:
            event.stop_event()
            cmd_arg = sub[len(matched) :].strip()
            logger.debug("[BetterGI-Msg] 匹配到 stop: alias='%s', arg='%s'", matched, cmd_arg)
            async for result in self._handle_stop(event, cmd_arg):
                yield result
            return

        matched = self._match_cmd(sub, self._cmd_log)
        if matched is not None:
            event.stop_event()
            cmd_arg = sub[len(matched) :].strip()
            logger.debug("[BetterGI-Msg] 匹配到 log: alias='%s', arg='%s'", matched, cmd_arg)
            yield await self._handle_log(event, cmd_arg)
            return

        matched = self._match_cmd(sub, self._cmd_bind)
        if matched is not None:
            event.stop_event()
            logger.debug("[BetterGI-Msg] 匹配到 bind: alias='%s'", matched)
            yield await self._handle_bind(event)
            return

        matched = self._match_cmd(sub, self._cmd_help)
        if matched is not None:
            event.stop_event()
            logger.debug("[BetterGI-Msg] 匹配到 help: alias='%s'", matched)
            yield event.plain_result(self._build_help_text())

    async def _handle_run(self, event: AstrMessageEvent, arg: str):
        commands = self._get_commands()
        logger.debug("[BetterGI-Run] arg='%s', commands_count=%d", arg, len(commands) if commands else 0)

        if not commands:
            yield event.plain_result("❌ 未配置任何命令，请先在配置中添加")
            return

        if arg in ("", "默认", "default"):
            args = self._get_command_at(1)
            logger.debug("[BetterGI-Run] 默认执行: args=%s", args)
            if args:
                result = await self._execute_command(args)
                logger.debug("[BetterGI-Run] 执行结果: %s", result)
                if result and not result[0]:
                    yield event.plain_result(f"❌ {result[1]}")
                else:
                    yield event.plain_result(f"✅ 正在执行第1个命令: {' '.join(args)}")
            else:
                yield event.plain_result("❌ 无法解析第1个命令配置")
            return

        if arg in ("选择", "select", "列表", "list"):
            msg_lines = ["📋 可用命令列表："]
            for i, cmd in enumerate(commands, 1):
                key = cmd.get("__template_key", "unknown")
                name = cmd.get("config_name", "未命名")
                type_name = "一条龙" if key == "dragon" else "调度器"
                msg_lines.append(f"{i}. [{type_name}] {name}")
            msg_lines.append(f"\n发送 {self._prefix}{self._cmd_run[0]} 序号 来执行")
            yield event.plain_result("\n".join(msg_lines))
            return

        if arg.isdigit():
            index = int(arg)
            args = self._get_command_at(index)
            logger.debug("[BetterGI-Run] 序号 %d: args=%s", index, args)
            if args:
                result = await self._execute_command(args)
                logger.debug("[BetterGI-Run] 执行结果: %s", result)
                if result and not result[0]:
                    yield event.plain_result(f"❌ {result[1]}")
                else:
                    yield event.plain_result(
                        f"✅ 正在执行第{index}个命令: {' '.join(args)}"
                    )
            else:
                yield event.plain_result(
                    f"❌ 无效的序号，请输入 1-{len(commands)} 之间的数字"
                )
            return

        if arg.startswith("--"):
            yield event.plain_result("❌ 请使用命令配置列表，无需手动填写命令参数")
            return

        yield event.plain_result(
            f"发送 {self._prefix}{self._cmd_run[0]} 选择 查看可用命令"
        )

    async def _execute_command(self, args: list[str]) -> tuple[bool, str] | None:
        """执行命令的实际逻辑。返回 (成功, 消息) 或 None。"""
        logger.debug("[BetterGI-Exec] 开始执行: args=%s", args)
        ok, msg = await self._runner.check_env()
        logger.debug("[BetterGI-Exec] 环境检查: ok=%s, msg=%s", ok, msg)
        if not ok:
            logger.error("[BetterGI] 环境检查失败: %s", msg)
            return False, f"环境检查失败: {msg}"

        success = await self._runner.run(args)
        logger.debug("[BetterGI-Exec] runner.run 返回: %s", success)
        if not success:
            detail = getattr(self._runner, "last_error", "") or "命令执行失败，请查看日志"
            logger.error("[BetterGI] 命令执行失败: %s (%s)", " ".join(args), detail)
            return False, detail
        return True, "OK"

    async def _handle_stop(self, event: AstrMessageEvent, arg: str = ""):
        force = arg in ("强制", "强停", "强制停止", "force", "f", "-f")
        logger.debug("[BetterGI-Stop] 尝试停止任务 (force=%s, arg='%s')", force, arg)

        stopped, message = await self._runner.stop(force=force)
        logger.debug("[BetterGI-Stop] 停止结果: stopped=%s, message=%s", stopped, message)

        if stopped:
            yield event.plain_result(f"✅ {message}")
            return

        detail = message or getattr(self._runner, "last_error", "")
        if detail:
            yield event.plain_result(f"❌ 停止失败: {detail}")
        else:
            yield event.plain_result("❌ 当前没有正在运行的任务")

    async def _handle_status(self, event: AstrMessageEvent):
        status = await self._runner.get_status()
        sched_status = self._scheduler.get_status()
        logger.debug("[BetterGI-Status] runner=%s, scheduler=%s", status, sched_status)

        lines = ["📊 BetterGI 状态：", ""]

        mode = status.get("mode", "local")
        mode_name = "本地" if mode == "local" else "远程"
        lines.append(f"🔹 运行模式: {mode_name}")

        if mode == "remote":
            connected = status.get("connected", False)
            lines.append(f"🔹 辅助程序: {'已连接' if connected else '未连接'}")

        is_running = status.get("is_running", False)
        lines.append(f"🔹 运行状态: {'运行中' if is_running else '空闲'}")

        current = status.get("current_command", "")
        if current:
            lines.append(f"🔹 当前命令: {current}")

        pids = status.get("pids") or []
        if pids:
            pid_text = ", ".join(str(p) for p in pids)
            lines.append(f"🔹 BetterGI 进程: 运行中 (PID: {pid_text})")
        elif status.get("pid"):
            lines.append(f"🔹 进程PID: {status['pid']}")
        elif mode == "local" and not status.get("process_running"):
            lines.append("🔹 BetterGI 进程: 未运行")

        webhook_running = self._webhook_server.is_running
        lines.append(f"🔹 Webhook: {'已启动' if webhook_running else '未启动'}")
        if webhook_running:
            lines.append(f"  地址: {self._webhook_server.webhook_url}")

        lines.append(
            f"🔹 定时任务: {'已启用' if sched_status.get('is_running') else '未启用'}"
        )
        if sched_status.get("last_run_date"):
            lines.append(f"  上次执行: {sched_status['last_run_date']}")

        if self._notify_umo:
            lines.append(f"🔹 通知绑定: 已绑定 {len(self._notify_umo)} 个会话")

        yield event.plain_result("\n".join(lines))

    async def _handle_log(
        self, event: AstrMessageEvent, arg: str
    ) -> MessageEventResult:
        logger.debug("[BetterGI-Log] arg='%s'", arg)
        if arg in ("清除", "清空", "clear"):
            count = await self._event_store.clear()
            logger.debug("[BetterGI-Log] 已清除 %d 条事件", count)
            return event.plain_result(f"✅ 已清除 {count} 条事件记录")

        count = 10
        if arg.isdigit():
            count = int(arg)

        events = await self._event_store.get_recent(count)
        logger.debug("[BetterGI-Log] 获取到 %d 条事件", len(events))
        text = EventStore.format_events(events)
        return event.plain_result(text)

    async def _handle_bind(self, event: AstrMessageEvent) -> MessageEventResult:
        umo = event.unified_msg_origin
        logger.debug("[BetterGI-Bind] 尝试绑定会话: %s", umo)

        if umo in self._notify_umo:
            logger.debug("[BetterGI-Bind] 会话已存在，无需重复绑定")
            return event.plain_result("✅ 当前会话已绑定，无需重复绑定")

        self._notify_umo.append(umo)
        logger.debug("[BetterGI-Bind] 已添加，当前绑定列表: %s", self._notify_umo)

        try:
            self.config["notify"]["umo"] = self._notify_umo
            self.config.save_config()
            logger.debug("[BetterGI-Bind] 配置已保存")
        except Exception as e:
            logger.warning("[BetterGI] 保存绑定配置失败: %s", e)

        return event.plain_result(
            f"✅ 已绑定当前会话为通知接收方\n"
            f"BetterGI 事件将自动转发到此处\n"
            f"当前共绑定 {len(self._notify_umo)} 个会话"
        )

    async def _on_webhook_event(self, event_data: dict) -> None:
        """处理 BetterGI Webhook 事件。"""
        logger.debug("[BetterGI-Webhook] 收到事件数据: keys=%s", list(event_data.keys()))

        await self._event_store.add(event_data)

        event_type = event_data.get("event", "")
        message = event_data.get("message", "")
        result = event_data.get("result", "")
        timestamp = event_data.get("timestamp", "")
        screenshot = event_data.get("screenshot", "")

        if event_type in self._end_events:
            mark_idle = getattr(self._runner, "mark_idle", None)
            if callable(mark_idle):
                mark_idle()
            logger.debug("[BetterGI-Webhook] 结束事件 %s，已清空运行标记", event_type)

        logger.info("[BetterGI] Webhook 事件: %s | %s | %s", event_type, result, message)
        logger.debug("[BetterGI-Webhook] screenshot: %s", f"有({len(screenshot)}字符)" if screenshot else "无")
        logger.debug("[BetterGI-Webhook] 全部字段: send_to=%s, timestamp=%s", event_data.get("send_to", ""), timestamp)

        if not self._notify_umo:
            logger.warning("[BetterGI-Webhook] 无绑定会话，不转发通知")
            return

        if not self._should_notify(event_type):
            logger.debug("[BetterGI-Webhook] 事件 %s 未启用通知，跳过", event_type)
            return

        logger.info("[BetterGI] 开始转发事件 %s", event_type)
        await self._send_notify(
            event_type, result, message, timestamp, screenshot
        )

    def _should_notify(self, event_type: str) -> bool:
        events_cfg = self.config.get("notify", {}).get("events", {})
        config_key = self._event_map.get(event_type)
        if not config_key:
            logger.debug("[BetterGI-Notify] 事件 %s 不在事件映射表中", event_type)
            return False
        enabled = events_cfg.get(config_key, False)
        logger.debug("[BetterGI-Notify] 事件 %s -> config_key=%s, enabled=%s", event_type, config_key, enabled)
        return enabled

    async def _send_notify(
        self,
        event_type: str,
        result: str,
        message: str,
        timestamp: str,
        screenshot: str = "",
    ) -> None:
        """发送事件通知到所有绑定的会话。"""
        logger.debug(
            "[BetterGI-Send] 开始: event=%s, umo_count=%d, has_screenshot=%s",
            event_type, len(self._notify_umo), bool(screenshot),
        )

        text = (
            f"📢 BetterGI 事件通知\n"
            f"事件: {event_type}\n"
            f"结果: {result}\n"
            f"时间: {timestamp}\n"
        )
        if message:
            text += f"消息: {message}"

        temp_path = ""
        if screenshot:
            try:
                image_data = base64.b64decode(screenshot)
                data_dir = self._get_data_dir()
                logger.debug("[BetterGI-Send] data_dir=%s, 图片数据=%d 字节", data_dir, len(image_data))
                os.makedirs(data_dir, exist_ok=True)
                fd, temp_path = tempfile.mkstemp(
                    suffix=".jpg", prefix="bettergi_", dir=data_dir
                )
                with os.fdopen(fd, "wb") as f:
                    f.write(image_data)
                logger.debug("[BetterGI-Send] 截图已保存: %s (%d 字节)", temp_path, len(image_data))
                logger.debug("[BetterGI-Send] 文件存在: %s", os.path.exists(temp_path))
            except Exception as e:
                logger.error("[BetterGI-Send] 保存截图失败: %s", e, exc_info=True)
                temp_path = ""
        else:
            logger.debug("[BetterGI-Send] 无截图数据，仅发送文本")

        for umo in self._notify_umo:
            try:
                chain = MessageChain().message(text)
                if temp_path:
                    chain = chain.file_image(temp_path)
                    logger.debug("[BetterGI-Send] 构建 MessageChain: text + file_image(%s)", temp_path)
                else:
                    logger.debug("[BetterGI-Send] 构建 MessageChain: text only")
                await self.context.send_message(umo, chain)
                logger.info("[BetterGI] 通知已发送到 %s (含图片: %s)", umo, bool(temp_path))
            except Exception as e:
                logger.error("[BetterGI-Send] 发送通知到 %s 失败: %s", umo, e, exc_info=True)

        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
                logger.debug("[BetterGI-Send] 临时文件已删除: %s", temp_path)
            except Exception:
                pass

    async def _run_scheduled_task(self) -> None:
        """定时任务执行函数。"""
        scheduled = self.config.get("scheduled_task", {})
        index = scheduled.get("command_index", 1)
        logger.info("[BetterGI] 定时任务触发: index=%d", index)

        args = self._get_command_at(index)
        if not args:
            logger.error("[BetterGI-Sched] 无法找到第%d个命令配置", index)
            return

        logger.info("[BetterGI] 定时任务执行: %s", " ".join(args))
        result = await self._execute_command(args)
        logger.debug("[BetterGI-Sched] 定时任务结果: %s", result)
        if result and not result[0]:
            logger.error("[BetterGI-Sched] 定时任务执行失败: %s", result[1])

    def _build_help_text(self) -> str:
        p = self._prefix

        def _alias_str(aliases: list[str]) -> str:
            return "/".join(aliases) if aliases else ""

        run = _alias_str(self._cmd_run)
        status = _alias_str(self._cmd_status)
        stop = _alias_str(self._cmd_stop)
        log = _alias_str(self._cmd_log)
        bind = _alias_str(self._cmd_bind)
        help_cmd = _alias_str(self._cmd_help)
        return (
            f"BetterGI 远程控制插件 使用帮助\n\n"
            f"📌 命令列表（前缀: {p}）：\n"
            f"  {p}{run}        - 运行第1个命令\n"
            f"  {p}{run} 选择   - 查看可用命令列表\n"
            f"  {p}{run} 序号   - 运行指定序号的命令\n"
            f"  {p}{status}      - 查看运行状态\n"
            f"  {p}{stop}        - 停止当前任务（含残留进程）\n"
            f"  {p}{stop} 强制   - 不等待优雅退出，直接强杀进程\n"
            f"  {p}{log}        - 查看最近事件日志\n"
            f"  {p}{log} 清除   - 清除事件记录\n"
            f"  {p}{bind}        - 绑定通知会话\n"
            f"  {p}{help_cmd}        - 显示此帮助\n\n"
            f"📌 Webhook 配置：\n"
            f"  在 BetterGI 设置中配置 Webhook 地址为：\n"
            f"  {self._webhook_server.webhook_url}\n"
            f"  API 密钥在 AstrBot 设置→OpenAPI 创建（只勾选 plugin 权限，有效期选永久）\n\n"
            f"📌 事件类型参考：\n"
            f"  dragon.start/end - 一条龙启动/结束\n"
            f"  group.start/end  - 配置组启动/结束\n"
            f"  task.cancel/error - 任务取消/错误\n"
            f"  domain.start/end/reward/retry - 秘境相关\n"
            f"  tcg.start/end - 七圣召唤启动/结束\n"
            f"  album.start/end/error - 音游相关\n"
            f"  autoeat.start/end/info - 自动吃药\n"
            f"  daily.reward - 每日奖励状态\n"
            f"  js.custom/error - JS自定义/错误\n"
            f"  notify.test - 测试通知"
        )

    async def terminate(self):
        """插件卸载时清理资源。"""
        logger.info("[BetterGI] 插件正在卸载...")
        await self._scheduler.stop()
        await self._webhook_server.stop()
        await self._runner.cleanup()
        logger.info("[BetterGI] 插件已卸载")
