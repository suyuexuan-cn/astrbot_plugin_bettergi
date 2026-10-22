import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger("bettergi")

HEARTBEAT_INTERVAL = 30.0


class PendingTask:
    """待执行任务。"""

    def __init__(self, task_id: str, args: list[str]):
        self.task_id = task_id
        self.args = args
        self.event = asyncio.Event()
        self.result: dict | None = None
        self.error: str | None = None


class RemoteConnectionManager:
    """管理远程辅助程序的 SSE 连接和任务调度。

    辅助程序通过 SSE 连接上来，插件通过 SSE 下发任务指令，
    辅助程序执行后通过 POST /result 上报结果。
    """

    def __init__(self):
        self._connected = False
        self._pending_tasks: dict[str, PendingTask] = {}
        self._connect_event = asyncio.Event()
        self._command_queue: asyncio.Queue[dict] = asyncio.Queue()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def wait_for_connection(self, timeout: float = 30) -> bool:
        """等待辅助程序连接。"""
        if self._connected:
            return True
        try:
            await asyncio.wait_for(self._connect_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def sse_stream(self) -> AsyncGenerator[str, None]:
        """SSE 事件流，供辅助程序连接。"""
        if self._connected:
            logger.warning("[BetterGI-Remote] 已有辅助程序连接，拒绝新连接")
            yield 'event: error\ndata: {"message": "already connected"}\n\n'
            return

        self._connected = True
        self._connect_event.set()
        logger.info("[BetterGI-Remote] 辅助程序已连接")

        try:
            yield 'event: connected\ndata: {"status": "ok"}\n\n'

            while True:
                try:
                    task = await asyncio.wait_for(
                        self._command_queue.get(), timeout=HEARTBEAT_INTERVAL
                    )
                except asyncio.TimeoutError:
                    # 心跳注释行，维持连接活跃，客户端会忽略
                    yield ": ping\n\n"
                    continue

                data = json.dumps(task, ensure_ascii=False)
                logger.debug("[BetterGI-Remote] 下发任务: %s", task.get("type"))
                yield f"event: command\ndata: {data}\n\n"
        except asyncio.CancelledError:
            logger.info("[BetterGI-Remote] SSE 连接已取消")
        except Exception as e:
            logger.error("[BetterGI-Remote] SSE 连接异常: %s", e)
        finally:
            # 同步清理（不 await，避免生成器关闭过程中的问题）
            self._connected = False
            self._connect_event.clear()
            for task in self._pending_tasks.values():
                task.error = "连接断开"
                task.event.set()
            self._pending_tasks.clear()
            # 丢弃未下发的过期命令，防止重连后执行
            while not self._command_queue.empty():
                try:
                    self._command_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            logger.info("[BetterGI-Remote] 辅助程序已断开")

    async def submit_task(self, args: list[str], timeout: float = 20) -> tuple[bool, str]:
        """提交任务并等待结果。返回 (成功, 消息)。

        辅助程序启动 BetterGI 后立即上报，通常几秒内返回，
        timeout 只是兜底（如辅助程序卡死）。
        """
        if not self._connected:
            return False, "辅助程序未连接"

        task_id = str(uuid.uuid4())
        pending = PendingTask(task_id, args)
        self._pending_tasks[task_id] = pending

        try:
            await self._command_queue.put({
                "type": "run",
                "task_id": task_id,
                "args": args,
                "timestamp": time.time(),
            })

            await asyncio.wait_for(pending.event.wait(), timeout=timeout)

            if pending.error:
                return False, pending.error
            if pending.result:
                success = pending.result.get("success", False)
                message = pending.result.get("message", "")
                return success, message
            return False, "未知结果"

        except asyncio.TimeoutError:
            self._pending_tasks.pop(task_id, None)
            return False, "等待辅助程序响应超时"
        except Exception as e:
            self._pending_tasks.pop(task_id, None)
            return False, f"任务执行失败: {e}"

    async def handle_result(self, result_data: dict) -> bool:
        """处理辅助程序上报的结果。"""
        task_id = result_data.get("task_id", "")
        success = result_data.get("success", False)

        logger.debug(
            "[BetterGI-Remote] 收到任务结果: task_id=%s, success=%s",
            task_id, success,
        )

        pending = self._pending_tasks.pop(task_id, None)

        if not pending:
            logger.warning("[BetterGI-Remote] 收到未知任务的结果: %s", task_id)
            return False

        pending.result = result_data
        pending.event.set()
        return True

    async def submit_stop(self, timeout: float = 15) -> tuple[bool, str]:
        """发送停止指令并等待辅助程序确认。

        必须保证 pending 一定被清理：否则超时后的结果会挂在表里，
        下次停止又会等待到超时，表现为"怎么发都关不掉"。
        """
        if not self._connected:
            return False, "辅助程序未连接"

        task_id = f"stop_{uuid.uuid4()}"
        pending = PendingTask(task_id, [])
        self._pending_tasks[task_id] = pending

        try:
            await self._command_queue.put({
                "type": "stop",
                "task_id": task_id,
                "timestamp": time.time(),
            })

            await asyncio.wait_for(pending.event.wait(), timeout=timeout)

            if pending.error:
                return False, pending.error
            if pending.result is not None and not pending.result.get("success", False):
                return False, pending.result.get("message", "") or "辅助程序停止失败"
            return True, "已停止"
        except asyncio.TimeoutError:
            logger.warning("[BetterGI-Remote] 停止指令未收到确认，task_id=%s", task_id)
            return (
                False,
                "已下发停止指令，但辅助程序 15 秒内未确认"
                "（可能已离线或连接假死，请检查辅助程序）",
            )
        except Exception as e:
            logger.error("[BetterGI-Remote] 停止指令下发失败: %s", e)
            return False, f"停止指令下发失败: {e}"
        finally:
            self._pending_tasks.pop(task_id, None)
