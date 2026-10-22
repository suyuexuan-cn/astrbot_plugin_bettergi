"""BetterGI 远程辅助程序（客户端模式）。

辅助程序主动连接到 AstrBot 主端口的 SSE 端点，
接收指令执行 BetterGI，执行完成后上报结果。
无需开放任何端口。
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

import httpx
import psutil
import yaml


def setup_logging(log_level: str = "INFO") -> None:
    """配置日志输出到文件和控制台。"""
    log_dir = os.path.dirname(os.path.abspath(__file__))
    log_file = os.path.join(log_dir, "assistant.log")

    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config(config_path: str) -> dict[str, Any]:
    """加载配置文件。"""
    if not os.path.exists(config_path):
        print(f"配置文件不存在: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_bettergi_exe(bettergi_dir: str) -> str | None:
    """查找 BetterGI 可执行文件。"""
    for name in ("BetterGI.exe", "BetterGI.bat", "BetterGI.cmd"):
        path = os.path.join(bettergi_dir, name)
        if os.path.exists(path):
            return path
    return None


GRACE_TIMEOUT = 5.0


def find_bettergi_pids(bettergi_dir: str) -> list[int]:
    """查找正在运行的 BetterGI 进程 PID 列表。

    不只认启动时拿到的 PID：BetterGI 单实例运行时，命令行启动的新进程
    可能把参数转交给已运行实例后自行退出，任务仍在跑。
    """
    exe_path = find_bettergi_exe(bettergi_dir)
    if not exe_path:
        return []

    exe_name = os.path.basename(exe_path).lower()
    target_dir = os.path.normcase(os.path.abspath(bettergi_dir))
    pids: list[int] = []

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if (proc.info["name"] or "").lower() != exe_name:
                continue
            exe = proc.info.get("exe") or ""
            if exe and not os.path.normcase(exe).startswith(target_dir):
                continue
            pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

    return pids


def is_bettergi_running(bettergi_dir: str) -> tuple[bool, int | None]:
    """检查 BetterGI 是否正在运行。"""
    pids = find_bettergi_pids(bettergi_dir)
    if not pids:
        return False, None
    return True, pids[0]


def kill_process_tree(proc: psutil.Process, graceful: bool = True) -> bool:
    """结束进程及其子进程，返回进程是否已退出。"""
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
        _, alive = psutil.wait_procs(targets, timeout=GRACE_TIMEOUT)
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


def stop_bettergi(bettergi_dir: str) -> tuple[bool, str]:
    """停止 BetterGI 进程（含子进程树）。"""
    pids = find_bettergi_pids(bettergi_dir)
    if not pids:
        return True, "未运行"

    stopped = 0
    failed: list[str] = []

    for pid in pids:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            stopped += 1
            continue
        except Exception as e:
            failed.append(f"{pid}({e})")
            continue

        try:
            if kill_process_tree(proc):
                stopped += 1
            else:
                failed.append(str(pid))
        except Exception as e:
            failed.append(f"{pid}({e})")

    if failed:
        logging.error("停止失败: %s", ", ".join(failed))
        return False, f"部分进程停止失败: {', '.join(failed)}"

    logging.info("BetterGI 已停止 %d 个进程", stopped)
    return True, f"已停止 {stopped} 个进程"


def run_bettergi(bettergi_dir: str, args: list[str]) -> tuple[bool, str]:
    """执行 BetterGI 命令。"""
    exe_path = find_bettergi_exe(bettergi_dir)
    if not exe_path:
        return False, f"未找到 BetterGI 可执行文件: {bettergi_dir}"

    # 安全检查：命令必须以 --startOneDragon 或 --startGroups 开头
    if not args or args[0] not in ("--startOneDragon", "--startGroups"):
        return False, "命令必须以 --startOneDragon 或 --startGroups 开头"

    cmd = [exe_path] + args
    logging.info("执行命令: %s", " ".join(cmd))

    try:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)

        # 使用 Popen 非阻塞执行；DEVNULL 避免管道缓冲区填满卡死
        proc = subprocess.Popen(
            cmd,
            cwd=bettergi_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        logging.info("BetterGI 已启动，PID: %d", proc.pid)
        return True, f"已启动 (PID: {proc.pid})"
    except FileNotFoundError:
        return False, f"可执行文件不存在: {exe_path}"
    except PermissionError:
        return False, "权限不足，可能需要管理员权限"
    except Exception as e:
        return False, f"启动失败: {e}"


def report_result(
    astrbot_url: str, api_key: str, task_id: str, success: bool, message: str
) -> bool:
    """上报执行结果到 AstrBot。"""
    url = f"{astrbot_url.rstrip('/')}/api/v1/plugins/extensions/bettergi/remote/result"
    payload = {
        "task_id": task_id,
        "success": success,
        "message": message,
    }
    headers = {}
    if api_key:
        # 只走 Authorization 头，避免密钥出现在 URL 中被访问日志记录
        headers["Authorization"] = f"ApiKey {api_key}"

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                logging.debug("结果上报成功: task_id=%s", task_id)
                return True
            logging.warning("结果上报失败: status=%d, body=%s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logging.error("结果上报异常: %s", e)
        return False


def sse_connect(astrbot_url: str, api_key: str, bettergi_dir: str) -> None:
    """连接 SSE 端点并处理指令。"""
    url = f"{astrbot_url.rstrip('/')}/api/v1/plugins/extensions/bettergi/remote/connect"
    headers = {}
    if api_key:
        # 只走 Authorization 头，避免密钥出现在 URL 中被访问日志记录
        headers["Authorization"] = f"ApiKey {api_key}"

    logging.info("连接 SSE 端点: %s", url)

    # 连接超时 10 秒；读不设超时（SSE 长连接，服务端每 30 秒发心跳）
    timeout = httpx.Timeout(10.0, read=None)

    while True:
        try:
            with httpx.stream("GET", url, headers=headers, timeout=timeout) as resp:
                if resp.status_code != 200:
                    logging.error("连接失败: HTTP %d", resp.status_code)
                    if resp.status_code == 401:
                        logging.error(
                            "认证失败，请检查 config.yaml 中的 api_key"
                            "（在 AstrBot 设置→OpenAPI 创建，只勾选 plugin 权限）"
                        )
                else:
                    logging.info("已连接到 AstrBot，等待指令...")

                    event_type = ""
                    data_buffer = ""

                    for line in resp.iter_lines():
                        if not line:
                            # 空行表示事件结束
                            if event_type and data_buffer:
                                _handle_event(
                                    event_type, data_buffer, bettergi_dir,
                                    astrbot_url, api_key,
                                )
                            event_type = ""
                            data_buffer = ""
                            continue

                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_buffer += line[5:].strip()

                    logging.warning("SSE 连接已断开，5秒后重连...")

        except httpx.ConnectError as e:
            logging.error("连接失败: %s，5秒后重试...", e)
        except Exception as e:
            logging.error("连接异常: %s，5秒后重连...", e)

        time.sleep(5)


def _handle_event(
    event_type: str,
    data: str,
    bettergi_dir: str,
    astrbot_url: str,
    api_key: str,
) -> None:
    """处理 SSE 事件。"""
    if event_type == "connected":
        logging.info("连接确认成功")
        return

    if event_type == "error":
        logging.error("收到错误事件: %s", data)
        return

    if event_type != "command":
        logging.debug("忽略未知事件: %s", event_type)
        return

    try:
        cmd_data = json.loads(data)
    except json.JSONDecodeError:
        logging.error("命令解析失败: %s", data)
        return

    cmd_type = cmd_data.get("type", "")
    task_id = cmd_data.get("task_id", "")

    logging.info("收到指令: type=%s, task_id=%s", cmd_type, task_id)

    if cmd_type == "run":
        args = cmd_data.get("args", [])
        success, message = run_bettergi(bettergi_dir, args)
        report_result(astrbot_url, api_key, task_id, success, message)

    elif cmd_type == "stop":
        success, message = stop_bettergi(bettergi_dir)
        report_result(astrbot_url, api_key, task_id, success, message)

    else:
        logging.warning("未知命令类型: %s", cmd_type)
        report_result(astrbot_url, api_key, task_id, False, f"未知命令: {cmd_type}")


def main() -> None:
    parser = argparse.ArgumentParser(description="BetterGI 远程辅助程序（客户端模式）")
    parser.add_argument(
        "-c", "--config",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
        help="配置文件路径",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_level", "INFO"))

    astrbot_url = config.get("astrbot_url", "")
    bettergi_dir = config.get("bettergi_dir", "")
    api_key = config.get("api_key", "")

    if not astrbot_url:
        logging.error("配置错误: astrbot_url 不能为空")
        sys.exit(1)

    if not bettergi_dir:
        logging.error("配置错误: bettergi_dir 不能为空")
        sys.exit(1)

    if config.get("token", ""):
        logging.warning("检测到旧版 token 配置已废弃，请改用 api_key")
    if not api_key:
        logging.warning(
            "api_key 未配置，新版 AstrBot 会拒绝连接（401）。"
            "请在 AstrBot 设置→OpenAPI 创建密钥（只勾选 plugin 权限，有效期选永久）"
        )

    if not os.path.exists(bettergi_dir):
        logging.error("BetterGI 目录不存在: %s", bettergi_dir)
        sys.exit(1)

    exe = find_bettergi_exe(bettergi_dir)
    if not exe:
        logging.error("未找到 BetterGI 可执行文件: %s", bettergi_dir)
        sys.exit(1)

    logging.info("=" * 50)
    logging.info("BetterGI 远程辅助程序启动")
    logging.info("模式: 客户端（主动连接 AstrBot）")
    logging.info("AstrBot 地址: %s", astrbot_url)
    logging.info("BetterGI 目录: %s", bettergi_dir)
    logging.info("可执行文件: %s", exe)
    logging.info("API 密钥: %s", "已设置" if api_key else "未设置")
    logging.info("=" * 50)

    try:
        sse_connect(astrbot_url, api_key, bettergi_dir)
    except KeyboardInterrupt:
        logging.info("用户中断，正在退出...")


if __name__ == "__main__":
    main()
