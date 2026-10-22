import asyncio
import os
import signal
import subprocess
from typing import Any

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger("bettergi")

try:
    import psutil
except ImportError:  # pragma: no cover - requirements.txt 已声明
    psutil = None


EXE_NAMES = ("BetterGI.exe", "BetterGI.bat", "BetterGI.cmd")
STOP_GRACE_TIMEOUT = 5.0


def build_command(template_key: str, config_name: str) -> list[str]:
    """根据模板类型和配置名组装 BetterGI 命令参数。

    Args:
        template_key: 模板类型 ("dragon" 或 "group")
        config_name: 配置名称

    Returns:
        命令参数列表（不含 exe 路径），如 ["--startOneDragon", "每日自动"]
    """
    if template_key == "dragon":
        args = ["--startOneDragon"]
        if config_name:
            args.append(config_name)
        return args
    elif template_key == "group":
        args = ["--startGroups"]
        if config_name:
            args.append(config_name)
        return args
    else:
        return [config_name] if config_name else []


# --------------------------------------------------------------------------
# 进程工具
# --------------------------------------------------------------------------


def _norm_case(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)) if path else ""


def _pid_alive(pid: int | None) -> bool:
    if not pid or psutil is None:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


def find_bettergi_pids(bettergi_dir: str = "") -> list[int]:
    """在系统中查找正在运行的 BetterGI 进程。

    为什么不能只记子进程 PID：BetterGI 本身是单实例程序，
    命令行启动时新进程可能把参数转交给已在运行的实例后自行退出，
    此时子进程已经结束但任务仍在跑；插件热重载后旧 PID 也会丢失。
    因此停止时必须能按可执行文件名兜底查找。
    """
    if psutil is None:
        return []

    exe_names = {name.lower() for name in EXE_NAMES}
    target_dir = _norm_case(bettergi_dir)
    pids: list[int] = []

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in exe_names:
                continue
            exe = proc.info.get("exe") or ""
            # 配置了目录且能取到 exe 路径时，只处理该目录下的进程，避免误杀其他副本
            if target_dir and exe and not _norm_case(exe).startswith(target_dir):
                continue
            pids.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

    return pids


def _kill_tree(proc, graceful: bool = True) -> bool:
    """结束进程及其子进程。返回进程是否已经不在了。

    先 terminate 让 BetterGI 自己收尾（它会关闭截图/输入注入等资源），
    超时后再 kill，避免留下孤儿进程继续占用游戏窗口。
    """
    try:
        children = proc.children(recursive=True)
    except psutil.NoSuchProcess:
        return True
    except Exception:
        children = []

    targets = [proc] + children

    if graceful:
        for p in targets:
            try:
                p.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            except Exception:
                pass
        _, alive = psutil.wait_procs(targets, timeout=STOP_GRACE_TIMEOUT)
        if not alive:
            return True
        targets = alive

    for p in targets:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        except Exception:
            pass
    _, alive = psutil.wait_procs(targets, timeout=3)
    return not alive


def _taskkill(pid: int) -> bool:
    """不依赖 psutil 的兜底强杀（Windows 同时结束整棵进程树）。"""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
            )
            return result.returncode == 0
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception as e:
        logger.warning("[BetterGI-Runner] 结束进程 %s 失败: %s", pid, e)
        return False


def stop_process_tree(pid: int, graceful: bool = True) -> bool:
    """结束指定 PID 及其子进程。返回 True 表示进程已不存在。"""
    if not pid:
        return False

    if psutil is not None:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        except Exception:
            proc = None
        if proc is not None:
            return _kill_tree(proc, graceful=graceful)

    return _taskkill(pid)


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


class LocalRunner:
    """本地模式命令执行器，直接通过子进程执行 BetterGI.exe。"""

    def __init__(self, bettergi_dir: str):
        self._bettergi_dir = bettergi_dir
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._current_command: str = ""
        self.last_error: str = ""
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """插件启动的任务是否还在跑（不探测外部进程，避免误判常驻的 BetterGI）。"""
        if self._process is not None and self._process.returncode is None:
            return True
        return _pid_alive(self._pid)

    @property
    def process_running(self) -> bool:
        """系统中是否存在 BetterGI 进程。"""
        return bool(find_bettergi_pids(self._bettergi_dir)) or self.is_running

    @property
    def current_command(self) -> str:
        return self._current_command

    def mark_idle(self) -> None:
        """收到任务结束事件后清空当前命令显示。"""
        self._current_command = ""

    async def run(self, args: list[str]) -> bool:
        """启动 BetterGI 执行命令（非阻塞，立即返回）。

        任务完成通过 Webhook 事件通知，不依赖进程返回码。
        """
        async with self._lock:
            if self.is_running:
                self.last_error = "已有任务正在运行，请先停止"
                logger.warning("[BetterGI-Runner] 已有任务正在运行，请先停止")
                return False

            exe_path = self._find_executable()
            if not exe_path:
                self.last_error = f"未找到 BetterGI.exe: {self._bettergi_dir}"
                logger.error(
                    f"[BetterGI-Runner] 未找到 BetterGI.exe: {self._bettergi_dir}"
                )
                return False

            cmd = [exe_path] + args
            self._current_command = " ".join(args)
            logger.info(f"[BetterGI-Runner] 执行命令: {' '.join(cmd)}")

            try:
                env = os.environ.copy()
                env.pop("PYTHONPATH", None)
                env.pop("PYTHONHOME", None)

                # DEVNULL 而非 PIPE：无人读取管道，PIPE 会因缓冲区填满卡死子进程
                self._process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=self._bettergi_dir,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                )
                self._pid = self._process.pid
                self.last_error = ""
                logger.info(f"[BetterGI-Runner] 进程已启动 (PID: {self._pid})")
                return True
            except FileNotFoundError:
                self._current_command = ""
                self.last_error = f"可执行文件不存在: {exe_path}"
                logger.error(f"[BetterGI-Runner] 可执行文件不存在: {exe_path}")
                return False
            except PermissionError:
                self._current_command = ""
                self.last_error = "权限不足，可能需要管理员权限"
                logger.error("[BetterGI-Runner] 权限不足，可能需要管理员权限")
                return False
            except Exception as e:
                self._current_command = ""
                self.last_error = f"启动进程失败: {e}"
                logger.error(f"[BetterGI-Runner] 启动进程失败: {e}", exc_info=True)
                return False

    async def stop(self, force: bool = False) -> tuple[bool, str]:
        """停止 BetterGI 及其子进程。

        依次处理三类目标：
        1. 插件自己启动的进程（精确 PID）；
        2. 单实例转发 / 热重载 / 手动启动留下的进程（按可执行文件名查找）；
        3. 都失败时用 taskkill /T 强杀进程树。

        Returns:
            (是否已停止, 说明)。返回 (False, "") 表示本来就没有在运行。
        """
        async with self._lock:
            self.last_error = ""

            managed_pid = self._pid
            proc = self._process
            if managed_pid is None and proc is not None:
                managed_pid = proc.pid

            was_running = self.is_running

            # 先解除引用，避免停止过程中被重复操作
            self._process = None
            self._pid = None

            stopped = 0
            failed: list[int] = []

            if managed_pid and _pid_alive(managed_pid):
                logger.info(f"[BetterGI-Runner] 正在停止进程 PID: {managed_pid}")
                ok = await asyncio.to_thread(
                    stop_process_tree, managed_pid, not force
                )
                if ok:
                    stopped += 1
                else:
                    failed.append(managed_pid)

            # 兜底：清理残留的 BetterGI 进程
            leftovers = await asyncio.to_thread(
                find_bettergi_pids, self._bettergi_dir
            )
            for pid in leftovers:
                if pid == managed_pid:
                    continue
                logger.info(f"[BetterGI-Runner] 清理残留进程 PID: {pid}")
                ok = await asyncio.to_thread(stop_process_tree, pid, not force)
                if ok:
                    stopped += 1
                else:
                    failed.append(pid)

            self._current_command = ""

            if stopped:
                logger.info(f"[BetterGI-Runner] 已停止 {stopped} 个进程")
                if stopped == 1:
                    return True, "BetterGI 已停止"
                return True, f"已停止 {stopped} 个 BetterGI 进程"

            if failed:
                self.last_error = (
                    f"无法结束进程 {','.join(str(p) for p in failed)}，"
                    "可能需要管理员权限"
                )
                logger.error(f"[BetterGI-Runner] {self.last_error}")
                return False, self.last_error

            if was_running:
                # 引用还在但进程已消失，视为已经停止
                return True, "BetterGI 进程已退出"

            logger.info("[BetterGI-Runner] 未发现正在运行的 BetterGI 进程")
            return False, ""

    async def get_status(self) -> dict[str, Any]:
        """获取当前运行状态。"""
        pids = await asyncio.to_thread(find_bettergi_pids, self._bettergi_dir)
        return {
            "is_running": self.is_running,
            "process_running": bool(pids) or self.is_running,
            "current_command": self._current_command,
            "pid": self._pid or (pids[0] if pids else None),
            "pids": pids,
            "mode": "local",
        }

    async def cleanup(self) -> None:
        """清理资源（插件卸载时只杀自己启动的进程，不影响用户手动打开的 BetterGI）。"""
        async with self._lock:
            pid = self._pid
            proc = self._process
            if pid is None and proc is not None:
                pid = proc.pid
            self._process = None
            self._pid = None
            if pid and _pid_alive(pid):
                await asyncio.to_thread(stop_process_tree, pid, False)
        self._current_command = ""

    def _find_executable(self) -> str | None:
        """查找 BetterGI 可执行文件。"""
        for name in EXE_NAMES:
            path = os.path.join(self._bettergi_dir, name)
            if os.path.exists(path):
                return path
        return None

    async def check_env(self) -> tuple[bool, str]:
        """检查本地环境是否可用。"""
        if not self._bettergi_dir:
            return False, "BetterGI 安装目录未配置"
        if not os.path.exists(self._bettergi_dir):
            return False, f"目录不存在: {self._bettergi_dir}"
        exe = self._find_executable()
        if not exe:
            return False, f"未找到 BetterGI.exe: {self._bettergi_dir}"
        return True, "OK"


class RemoteRunner:
    """远程模式命令执行器，通过 SSE 连接的辅助程序执行命令。

    辅助程序主动连接到 AstrBot 主端口的 SSE 端点，
    插件通过 SSE 下发命令，辅助程序执行后通过 POST /result 上报结果。
    """

    def __init__(self, manager):
        self._manager = manager  # RemoteConnectionManager
        self._current_command: str = ""
        self.last_error: str = ""

    @property
    def is_running(self) -> bool:
        # 简化：有当前命令就认为在运行（实际状态通过 Webhook 事件更新）
        return bool(self._current_command)

    @property
    def current_command(self) -> str:
        return self._current_command

    def mark_idle(self) -> None:
        """收到任务结束事件后清空运行标记。"""
        self._current_command = ""

    async def run(self, args: list[str]) -> bool:
        """通过辅助程序执行 BetterGI 命令。"""
        if not self._manager.is_connected:
            self.last_error = "辅助程序未连接，请确认辅助程序已启动"
            logger.error("[BetterGI-Remote] 辅助程序未连接")
            return False

        command_str = " ".join(args)
        logger.info("[BetterGI-Remote] 下发命令: %s", command_str)

        # submit_task 只等待启动确认（辅助程序立即返回），默认超时足够
        success, message = await self._manager.submit_task(args)
        if success:
            self._current_command = command_str
            self.last_error = ""
            logger.info("[BetterGI-Remote] 命令已执行: %s (%s)", command_str, message)
        else:
            self._current_command = ""
            self.last_error = message
            logger.error("[BetterGI-Remote] 命令执行失败: %s", message)
        return success

    async def stop(self, force: bool = False) -> tuple[bool, str]:
        """发送停止指令。

        无论是否收到确认都要清空运行标记，否则状态会一直卡在"运行中"，
        用户再次发停止也只是继续超时。
        """
        if not self._manager.is_connected:
            self._current_command = ""
            self.last_error = "辅助程序未连接，无法下发停止指令"
            logger.error("[BetterGI-Remote] 辅助程序未连接")
            return False, self.last_error

        success, message = await self._manager.submit_stop()
        self._current_command = ""

        if success:
            self.last_error = ""
            logger.info("[BetterGI-Remote] 已停止: %s", message)
            return True, "已下发停止指令，辅助程序已执行"

        self.last_error = message
        logger.error("[BetterGI-Remote] 停止失败: %s", message)
        return False, message

    async def get_status(self) -> dict[str, Any]:
        """获取运行状态。"""
        return {
            "is_running": self.is_running,
            "process_running": self.is_running,
            "current_command": self._current_command,
            "pid": None,
            "pids": [],
            "mode": "remote",
            "connected": self._manager.is_connected,
        }

    async def cleanup(self) -> None:
        pass

    async def check_env(self) -> tuple[bool, str]:
        """检查辅助程序是否已连接。"""
        if self._manager.is_connected:
            return True, "辅助程序已连接"
        return False, "辅助程序未连接，请确认 BetterGI 电脑上的辅助程序已启动"


def create_runner(config: dict, remote_manager=None) -> LocalRunner | RemoteRunner:
    """根据配置创建对应的命令执行器。

    Args:
        config: 插件配置
        remote_manager: 远程连接管理器（远程模式时必须提供）
    """
    mode = config.get("mode", "local")

    if mode == "remote":
        logger.debug("[BetterGI-Runner] 创建 RemoteRunner（SSE模式）")
        if remote_manager is None:
            raise ValueError("远程模式需要提供 remote_manager")
        return RemoteRunner(manager=remote_manager)
    else:
        bettergi_dir = config.get("bettergi_dir", "")
        logger.debug(
            "[BetterGI-Runner] 创建 LocalRunner: bettergi_dir=%s",
            bettergi_dir,
        )
        return LocalRunner(bettergi_dir=bettergi_dir)
